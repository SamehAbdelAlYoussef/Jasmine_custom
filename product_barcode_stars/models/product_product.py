# -*- coding: utf-8 -*-
import logging
import re
import io
import base64

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arabic → Latin transliteration (used for vendor initials in barcode prefix)
# ---------------------------------------------------------------------------
_ARABIC_TO_LATIN = {
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
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
    '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
}


def _arabic_to_latin(text):
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
                code128 = barcode.get('code128', product.barcode, writer=writer)
                buffer = io.BytesIO()
                code128.write(buffer)
                buffer.seek(0)
                product.barcode_image = base64.b64encode(buffer.read())
            except Exception as e:
                _logger.error("Barcode image failed for %s: %s", product.barcode, e)
                product.barcode_image = False

    def _build_barcode(self, product):
        """
        Build barcode: [CategoryNumber][VendorInitials][default_code]

        Format example: 01ANPROD001
          01   = category_number from product.category
          AN   = first 2 chars of vendor name (Arabic transliterated to Latin)
          PROD001 = product's Internal Reference (default_code)

        Returns False if default_code is not set.
        """
        if not product.default_code:
            return False

        # Category number (2-digit)
        cat_num = '00'
        if product.categ_id and product.categ_id.category_number:
            cat_num = product.categ_id.category_number

        # Vendor initials (2 Latin chars)
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

        return '%s%s%s' % (cat_num, vendor_initials, product.default_code)

    def action_generate_barcode(self):
        """Generate barcode from default_code — skips products that already have a barcode."""
        generated_count = 0
        for product in self:
            if product.barcode:
                continue
            barcode_val = self._build_barcode(product)
            if barcode_val:
                product.write({'barcode': barcode_val})
                generated_count += 1

        if generated_count:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('✅ باركود تم إنشاؤه'),
                    'message': _('تم إنشاء %(count)s باركود من الرقم المرجعي.') % {'count': generated_count},
                    'type': 'success',
                    'sticky': False,
                },
            }
        return True

    def action_regenerate_barcode(self):
        """Regenerate barcode for all selected products using current category + vendor + default_code."""
        updated_count = 0
        for product in self:
            barcode_val = self._build_barcode(product)
            if barcode_val and barcode_val != product.barcode:
                product.write({'barcode': barcode_val})
                updated_count += 1

        if updated_count:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('✅ تم تحديث الباركود'),
                    'message': _('تم تحديث %(count)s باركود.') % {'count': updated_count},
                    'type': 'success',
                    'sticky': False,
                },
            }
        return True

    def write(self, vals):
        res = super().write(vals)
        # Rebuild barcode when category or vendor changes
        if {'categ_id', 'seller_ids'} & set(vals.keys()):
            for product in self:
                if product.barcode:
                    new_barcode = self._build_barcode(product)
                    if new_barcode and new_barcode != product.barcode:
                        super(ProductProduct, product).write({'barcode': new_barcode})
        return res


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
        for template in self:
            variant = template.product_variant_ids[:1]
            if variant:
                variant.barcode = template.barcode

    def _search_barcode(self, operator, value):
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
        """Force recompute barcode images for all products."""
        all_products = self.env['product.product'].search([('barcode', '!=', False)])
        all_products._compute_barcode_image()
        templates = self.env['product.template'].search([('barcode', '!=', False)])
        templates._compute_barcode_image_template()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def write(self, vals):
        res = super().write(vals)
        # Rebuild barcode when category changes
        if 'categ_id' in vals:
            for template in self:
                for variant in template.product_variant_ids:
                    if variant.barcode:
                        new_barcode = variant._build_barcode(variant)
                        if new_barcode and new_barcode != variant.barcode:
                            variant.write({'barcode': new_barcode})
        return res

    def action_generate_barcode(self):
        """Generate barcodes for all variants of this template."""
        self.mapped('product_variant_ids').action_generate_barcode()
        self._compute_barcode_image_template()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def action_regenerate_barcode(self):
        """Regenerate barcodes for all variants of this template."""
        self.mapped('product_variant_ids').action_regenerate_barcode()
        self._compute_barcode_image_template()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }


class ProductSupplierinfo(models.Model):
    """Rebuild barcode when vendor list changes (prefix uses vendor initials)."""
    _inherit = 'product.supplierinfo'

    def _rebuild_template_barcodes(self, templates):
        for template in templates:
            for variant in template.product_variant_ids:
                if variant.barcode:
                    new_barcode = variant._build_barcode(variant)
                    if new_barcode and new_barcode != variant.barcode:
                        variant.write({'barcode': new_barcode})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._rebuild_template_barcodes(records.mapped('product_tmpl_id'))
        return records

    def write(self, vals):
        res = super().write(vals)
        self._rebuild_template_barcodes(self.mapped('product_tmpl_id'))
        return res

    def unlink(self):
        templates = self.mapped('product_tmpl_id')
        res = super().unlink()
        self._rebuild_template_barcodes(templates)
        return res
