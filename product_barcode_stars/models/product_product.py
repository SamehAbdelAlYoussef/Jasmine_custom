# -*- coding: utf-8 -*-
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arabic → Latin transliteration table
# Used to convert Arabic vendor names into ASCII barcode-safe initials.
# Multi-character mappings (like SH for ش) are handled in _arabic_to_latin().
# ---------------------------------------------------------------------------
_ARABIC_TO_LATIN = {
    # Single-letter mappings
    'ا': 'A', 'أ': 'A', 'إ': 'A', 'آ': 'A',
    'ب': 'B',
    'ت': 'T', 'ث': 'T',
    'ج': 'J',
    'ح': 'H', 'خ': 'K',
    'د': 'D', 'ذ': 'D',
    'ر': 'R',
    'ز': 'Z',
    'س': 'S', 'ش': 'S',
    'ص': 'S', 'ض': 'D',
    'ط': 'T', 'ظ': 'Z',
    'ع': 'A', 'غ': 'G',
    'ف': 'F', 'ق': 'Q',
    'ك': 'K',
    'ل': 'L',
    'م': 'M',
    'ن': 'N',
    'ه': 'H', 'ة': 'H',
    'و': 'W', 'ؤ': 'W',
    'ي': 'Y', 'ى': 'A', 'ئ': 'Y',
    'ء': 'A',
    # Digits (keep as-is)
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
    '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
}


def _arabic_to_latin(text):
    """Convert Arabic characters to Latin, preserving Latin chars as-is.

    Returns a string of only ASCII letters (A-Z) — exactly 2 chars
    per input character for mapped Arabic letters, 1:1 for Latin.
    The result is safe for Code128 barcodes.
    """
    result = []
    for ch in text:
        if 'A' <= ch <= 'Z' or 'a' <= ch <= 'z':
            result.append(ch.upper())
        else:
            mapped = _ARABIC_TO_LATIN.get(ch)
            if mapped:
                result.append(mapped)
    return ''.join(result)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    barcode_image = fields.Binary(
        string='Barcode Image',
        compute='_compute_barcode_image',
        store=True,
        readonly=True,
    )

    @api.depends('barcode')
    def _compute_barcode_image(self):
        for product in self:
            if not product.barcode:
                product.barcode_image = False
                continue
            try:
                import base64, io
                import barcode
                from barcode.writer import ImageWriter

                writer = ImageWriter()
                writer.set_options({
                    'module_width': 0.2,
                    'module_height': 15.0,
                    'quiet_zone': 1.0,
                    'font_size': 8,
                    'text_distance': 2.0,
                    'background': 'white',
                    'foreground': 'black',
                })
                code128 = barcode.get(
                    'code128', product.barcode,
                    writer=writer,
                )
                buffer = io.BytesIO()
                code128.write(buffer)
                buffer.seek(0)
                product.barcode_image = base64.b64encode(buffer.read())
            except Exception as e:
                _logger.error("Barcode image failed for %s: %s", product.barcode, e)
                product.barcode_image = False

    def write(self, vals):
        res = super(ProductProduct, self).write(vals)
        # When category or vendor (seller_ids) changes, rebuild ONLY the prefix.
        # The sequence number stays fixed — never changes after initial generation.
        if {'categ_id', 'seller_ids'} & set(vals.keys()):
            for product in self:
                if product.barcode:
                    new_barcode = product._rebuild_barcode_prefix(product)
                    if new_barcode:
                        super(ProductProduct, product).write({'barcode': new_barcode})
        return res

    def action_generate_barcode(self):
        """Generate barcode for selected products (only if empty)"""
        generated_count = 0
        for product in self:
            if product.barcode:
                continue
            barcode = self._build_barcode(product)
            if barcode:
                product.write({'barcode': barcode})
                generated_count += 1

        if generated_count:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('✅ باركود تم إنشاؤه'),
                    'message': _(
                        'تم إنشاء %(count)s باركود بنجاح مع أول حرفين من اسم المورد (عربي → إنجليزي).'
                    ) % {'count': generated_count},
                    'type': 'success',
                    'sticky': False,
                },
            }
        return True

    def action_regenerate_barcode(self):
        """Regenerate barcode — rebuilds prefix (vendor/category) but keeps the SAME sequence number."""
        updated_count = 0
        for product in self:
            if not product.barcode:
                barcode = self._build_barcode(product)
            else:
                barcode = self._rebuild_barcode_prefix(product)
            if barcode and barcode != product.barcode:
                product.write({'barcode': barcode})
                updated_count += 1
        if updated_count:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('✅ تم تحديث الباركود'),
                    'message': _('تم تحديث %(count)s باركود — البادئة فقط، رقم التسلسل ثابت.') % {'count': updated_count},
                    'type': 'success',
                    'sticky': False,
                },
            }
        return True

    def _extract_seq_from_barcode(self, barcode_value):
        """Extract the sequence number from a barcode.
        Format: [2-digit category][2-char vendor][sequence]
        E.g.: '22AD15' → 15, '78SH123' → 123
        """
        if len(barcode_value) > 4:
            seq = barcode_value[4:]
            return int(seq) if seq.isdigit() else None
        return None

    def _rebuild_barcode_prefix(self, product):
        """Rebuild only the category+vendor prefix; keep the existing sequence number.
        E.g.: 22AD15 → vendor changed → 22MY15 (same sequence, new vendor initials)
        """
        if not product.barcode:
            return self._build_barcode(product)

        # Rebuild the prefix from current category + vendor
        cat_num = '00'
        if product.categ_id and product.categ_id.category_number:
            cat_num = product.categ_id.category_number

        # Vendor initials
        seller = None
        if product.product_tmpl_id and product.product_tmpl_id.seller_ids:
            seller = product.product_tmpl_id.seller_ids[0].partner_id
        elif product.seller_ids:
            seller = product.seller_ids[0].partner_id

        vendor_initials = 'XX'
        if seller and seller.name:
            name_clean = re.sub(r'[^a-zA-Z؀-ۿ]', '', seller.name).strip()
            if name_clean:
                raw_initials = name_clean[:2]
                vendor_initials = _arabic_to_latin(raw_initials)
                if len(vendor_initials) > 2:
                    vendor_initials = vendor_initials[:2]
                elif len(vendor_initials) < 2:
                    vendor_initials = vendor_initials.ljust(2, 'X')

        # Extract existing sequence number — never changes
        seq_num = self._extract_seq_from_barcode(product.barcode)
        if seq_num is None:
            # Fallback: if barcode has no digits, generate new sequence
            return self._build_barcode(product)

        prefix = '%s%s' % (cat_num, vendor_initials)
        return '%s%s' % (prefix, seq_num)

    def _build_barcode(self, product):
        """
        Build barcode: [CategoryNumber][VendorInitials][GLOBAL_Sequence]

        Barcode format:  22XX7, 78SH123
        - XX  = default when no vendor
        - SH  = Arabic ش → S + ر → H  mapped to Latin (Code128-safe)
        - SA  = Latin vendor "Sameh" → first 2 chars "SA"

        Global sequence NEVER resets — unique across all products.
        """
        cat_num = '00'
        if product.categ_id and product.categ_id.category_number:
            cat_num = product.categ_id.category_number

        # Vendor initials from template's seller_ids
        seller = None
        if product.product_tmpl_id and product.product_tmpl_id.seller_ids:
            seller = product.product_tmpl_id.seller_ids[0].partner_id
        elif product.seller_ids:
            seller = product.seller_ids[0].partner_id

        vendor_initials = 'XX'
        vendor_display_name = ''
        if seller and seller.name:
            # 1) Keep only Arabic + Latin letters (strip digits, special chars)
            name_clean = re.sub(r'[^a-zA-Z؀-ۿ]', '', seller.name)
            name_clean = name_clean.strip()
            if name_clean:
                # 2) Take the first 2 characters (may be Arabic or Latin)
                raw_initials = name_clean[:2]
                # 3) Transliterate Arabic → Latin (Latin chars stay as-is, uppercased)
                vendor_initials = _arabic_to_latin(raw_initials)
                if vendor_initials:
                    vendor_display_name = seller.name
                # 4) Ensure exactly 2 ASCII chars for the barcode
                if len(vendor_initials) > 2:
                    vendor_initials = vendor_initials[:2]
                elif len(vendor_initials) < 2:
                    vendor_initials = vendor_initials.ljust(2, 'X')

        prefix = '%s%s' % (cat_num, vendor_initials)

        # GLOBAL sequence — same for ALL products regardless of category/vendor
        IrSequence = self.env['ir.sequence'].sudo()
        seq_code = 'product.barcode.global'
        sequence = IrSequence.search([('code', '=', seq_code)], limit=1)
        if not sequence:
            last_barcode = self.env['product.product'].search(
                [('barcode', '!=', False)],
                order='id desc', limit=1,
            )
            next_num = 1
            if last_barcode and last_barcode.barcode:
                # Extract last number from any barcode
                num_part = ''.join(c for c in last_barcode.barcode if c.isdigit())
                if num_part:
                    next_num = int(num_part) + 1

            sequence = IrSequence.create({
                'name': 'Product Barcode Global',
                'code': seq_code,
                'prefix': '',
                'padding': 0,
                'number_next': next_num,
                'implementation': 'no_gap',
            })

        seq_num = sequence.next_by_id()
        barcode_value = '%s%s' % (prefix, seq_num)

        # Log when an Arabic vendor name was transliterated
        if vendor_display_name:
            _logger.info(
                "Barcode %s: vendor '%s' → initials '%s' (Arabic→Latin)",
                barcode_value, vendor_display_name, vendor_initials,
            )

        return barcode_value


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    x_size = fields.Char(
        string='Size',
        help="Product size (e.g. 100ml, XL, 50g, ...)",
    )
    x_brand = fields.Char(
        string='Brand',
        help="Product brand name",
    )

    barcode = fields.Char(
        string='Barcode',
        compute='_compute_barcode',
        inverse='_set_barcode',
        search='_search_barcode',
        store=True,
    )

    barcode_image = fields.Binary(
        string='Barcode Image',
        compute='_compute_barcode_image_template',
        store=True,
        readonly=True,
    )

    @api.depends('product_variant_ids.barcode')
    def _compute_barcode(self):
        self._compute_template_field_from_variant_field('barcode')

    def _set_barcode(self):
        """Write barcode to the first variant."""
        for template in self:
            variant = template.product_variant_ids[:1]
            if variant:
                variant.barcode = template.barcode

    def _search_barcode(self, operator, value):
        """Search barcode on variants."""
        variants = self.env['product.product'].search(
            [('barcode', operator, value)], limit=None
        )
        return [('id', 'in', variants.product_tmpl_id.ids)]

    @api.depends('product_variant_ids.barcode_image')
    def _compute_barcode_image_template(self):
        for template in self:
            variant = template.product_variant_ids[:1]
            template.barcode_image = variant.barcode_image if variant else False

    def action_recompute_barcode_images(self):
        """Force recompute barcode images for all products"""
        all_products = self.env['product.product'].search([('barcode', '!=', False)])
        all_products._compute_barcode_image()
        # Recompute template images
        templates = self.env['product.template'].search([('barcode', '!=', False)])
        templates._compute_barcode_image_template()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def _trigger_barcode_prefix_rebuild(self):
        """Rebuild barcode PREFIX for all variants (vendor/category changed).
        Sequence number stays fixed — only prefix is rebuilt.
        """
        for variant in self.product_variant_ids:
            if variant.barcode:
                barcode = variant._rebuild_barcode_prefix(variant)
                if barcode:
                    variant.write({'barcode': barcode})

    @api.model
    def create(self, vals):
        template = super(ProductTemplate, self).create(vals)
        # Auto-generate barcode for all variants after creation
        if template.product_variant_ids:
            for variant in template.product_variant_ids:
                if not variant.barcode:
                    barcode = variant._build_barcode(variant)
                    if barcode:
                        variant.write({'barcode': barcode})
            # Recompute barcode_image on template
            template._compute_barcode_image_template()
        return template

    def write(self, vals):
        # When category changes, rebuild PREFIX only — sequence stays fixed.
        prefix_changed = 'categ_id' in vals
        res = super(ProductTemplate, self).write(vals)
        for template in self:
            for variant in template.product_variant_ids:
                if not variant.barcode:
                    # No barcode yet → generate fresh one
                    barcode = variant._build_barcode(variant)
                    if barcode:
                        variant.write({'barcode': barcode})
                elif prefix_changed:
                    # Category changed → rebuild prefix, keep sequence
                    barcode = variant._rebuild_barcode_prefix(variant)
                    if barcode:
                        variant.write({'barcode': barcode})
        return res

    def action_generate_barcode(self):
        """Generate barcodes for all variants of this template"""
        self.mapped('product_variant_ids').action_generate_barcode()
        self._compute_barcode_image_template()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def action_regenerate_barcode(self):
        """Regenerate barcodes for all variants"""
        self.mapped('product_variant_ids').action_regenerate_barcode()
        self._compute_barcode_image_template()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }


class ProductSupplierinfo(models.Model):
    """Hook into vendor list changes to rebuild barcode PREFIX only.
    The sequence number always stays fixed.
    """
    _inherit = 'product.supplierinfo'

    @api.model
    def create(self, vals):
        record = super(ProductSupplierinfo, self).create(vals)
        if record.product_tmpl_id:
            record.product_tmpl_id._trigger_barcode_prefix_rebuild()
        return record

    def write(self, vals):
        res = super(ProductSupplierinfo, self).write(vals)
        for record in self:
            if record.product_tmpl_id:
                record.product_tmpl_id._trigger_barcode_prefix_rebuild()
        return res

    def unlink(self):
        templates = self.mapped('product_tmpl_id')
        res = super(ProductSupplierinfo, self).unlink()
        for template in templates:
            template._trigger_barcode_prefix_rebuild()
        return res
