# -*- coding: utf-8 -*-
import logging
import io
import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


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
        """Barcode = Internal Reference (default_code) only."""
        return product.default_code or False

    def _next_unique_default_code(self, reserved):
        """Return the next unique default_code not used by any product or in `reserved`.

        Finds the maximum numeric default_code across ALL products in the DB,
        then counts up from there until it finds an unused value.
        `reserved` is a set of codes already assigned in this batch (in-memory).
        """
        # Collect all numeric default_codes from the entire product table
        self.env.cr.execute(
            "SELECT default_code FROM product_product WHERE default_code ~ '^[0-9]+$'"
        )
        existing_nums = {int(r[0]) for r in self.env.cr.fetchall()}
        candidate = (max(existing_nums) if existing_nums else 0) + 1
        while str(candidate) in reserved or candidate in existing_nums:
            candidate += 1
        return str(candidate)

    def action_generate_barcode(self):
        """Generate barcode from default_code.

        - Products that already have a barcode are skipped.
        - Products without a default_code get an auto-generated unique reference
          (next integer after the highest numeric default_code in the system).
        - Raises UserError if two selected products share the same default_code.
        """
        # Uniqueness check: default_code must not be duplicated among selected products
        codes = [p.default_code for p in self if p.default_code and not p.barcode]
        if len(codes) != len(set(codes)):
            from collections import Counter
            duplicates = [c for c, n in Counter(codes).items() if n > 1]
            raise UserError(
                _('الرقم المرجعي (Reference) مكرر في المنتجات المختارة:\n%s\n\nيجب أن يكون كل رقم مرجعي فريداً.') % ', '.join(duplicates)
            )

        generated_count = 0
        auto_ref_count = 0
        # Track refs generated in this batch so they don't collide with each other
        reserved_codes = set()

        for product in self:
            if product.barcode:
                continue

            # Auto-generate a unique default_code if the product has none
            if not product.default_code:
                new_code = self._next_unique_default_code(reserved_codes)
                reserved_codes.add(new_code)
                product.write({'default_code': new_code})
                auto_ref_count += 1

            # Check barcode not already used by another product
            barcode_val = self._build_barcode(product)
            if not barcode_val:
                continue
            existing = self.env['product.product'].search([
                ('barcode', '=', barcode_val),
                ('id', '!=', product.id),
            ], limit=1)
            if existing:
                raise UserError(
                    _('الباركود "%s" مستخدم مسبقاً من المنتج "%s".\nتأكد أن الرقم المرجعي فريد.') % (
                        barcode_val, existing.display_name
                    )
                )
            product.write({'barcode': barcode_val})
            generated_count += 1

        msg_parts = []
        if generated_count:
            msg_parts.append(_('تم إنشاء %(count)s باركود.') % {'count': generated_count})
        if auto_ref_count:
            msg_parts.append(_('تم توليد %(count)s رقم مرجعي تلقائياً للمنتجات بدون رقم.') % {'count': auto_ref_count})

        if generated_count:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('✅ باركود تم إنشاؤه'),
                    'message': ' | '.join(msg_parts),
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
