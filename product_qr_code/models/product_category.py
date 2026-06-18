# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ProductCategory(models.Model):
    _inherit = 'product.category'

    category_number = fields.Char(
        string='Category Number',
        help='Unique numeric code for this category (used in product QR codes)',
        copy=False,
    )

    @api.constrains('category_number')
    def _check_category_number_unique(self):
        for rec in self:
            if rec.category_number:
                dup = self.search([
                    ('category_number', '=', rec.category_number),
                    ('id', '!=', rec.id),
                ], limit=1)
                if dup:
                    raise ValidationError(
                        'Category Number "%s" is already used by category "%s"!' %
                        (rec.category_number, dup.name)
                    )

    @api.model
    def create(self, vals):
        if not vals.get('category_number'):
            vals['category_number'] = self._get_next_category_number()
        return super(ProductCategory, self).create(vals)

    @api.model
    def _get_next_category_number(self):
        """Auto-generate next category number as 2-digit string (01, 02, ...)"""
        last = self.search([], order='category_number desc', limit=1)
        if last and last.category_number:
            try:
                next_num = int(last.category_number) + 1
            except ValueError:
                next_num = 1
        else:
            next_num = 1
        return str(next_num).zfill(2)
