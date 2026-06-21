# -*- coding: utf-8 -*-
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

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
        # Regenerate barcode when categ_id or seller_ids changes
        # (these can come via product.template write or be set directly)
        prefix_fields = {'categ_id', 'seller_ids'}
        if prefix_fields & set(vals.keys()):
            for product in self:
                barcode = product._build_barcode(product)
                if barcode:
                    super(ProductProduct, product).write({'barcode': barcode})
        return res

    def action_generate_barcode(self):
        """Generate barcode for selected products (only if empty)"""
        for product in self:
            if product.barcode:
                continue
            barcode = self._build_barcode(product)
            if barcode:
                product.write({'barcode': barcode})
        return True

    def action_regenerate_barcode(self):
        """Force regenerate barcode"""
        self.write({'barcode': False})
        return self.action_generate_barcode()

    def _build_barcode(self, product):
        """
        Build barcode: [CategoryNumber][VendorInitials][GLOBAL_Sequence]
        Example: 78MY1, 23AD2, 78MY3 (global sequence NEVER resets)
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
        if seller and seller.name:
            name_clean = re.sub(r'[^a-zA-Z؀-ۿ\s]', '', seller.name)
            name_clean = name_clean.strip()
            if name_clean:
                vendor_initials = name_clean[:2].upper()

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
        return '%s%s' % (prefix, seq_num)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

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

    def _trigger_barcode_regeneration(self):
        """Regenerate barcodes for all variants (called when vendor/category changes)."""
        for variant in self.product_variant_ids:
            barcode = variant._build_barcode(variant)
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
        # Detect if category changed — triggers barcode regeneration
        # Note: seller_ids is One2many (virtual), handled via
        # product.supplierinfo overrides below
        prefix_changed = 'categ_id' in vals
        res = super(ProductTemplate, self).write(vals)
        for template in self:
            for variant in template.product_variant_ids:
                if prefix_changed or not variant.barcode:
                    # Regenerate barcode when category changes
                    # or variant has no barcode yet
                    barcode = variant._build_barcode(variant)
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
    """Hook into vendor list changes to regenerate barcodes."""
    _inherit = 'product.supplierinfo'

    @api.model
    def create(self, vals):
        record = super(ProductSupplierinfo, self).create(vals)
        if record.product_tmpl_id:
            record.product_tmpl_id._trigger_barcode_regeneration()
        return record

    def write(self, vals):
        res = super(ProductSupplierinfo, self).write(vals)
        for record in self:
            if record.product_tmpl_id:
                record.product_tmpl_id._trigger_barcode_regeneration()
        return res

    def unlink(self):
        templates = self.mapped('product_tmpl_id')
        res = super(ProductSupplierinfo, self).unlink()
        for template in templates:
            template._trigger_barcode_regeneration()
        return res
