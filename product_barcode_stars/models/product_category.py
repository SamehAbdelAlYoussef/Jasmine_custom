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

    @api.model_create_multi
    def create(self, vals_list):
        # Collect all existing numeric category numbers once, then assign
        # unique sequential numbers to every record in the batch.
        existing = self._get_existing_category_numbers()
        next_num = 1
        for vals in vals_list:
            if not vals.get('category_number'):
                # Skip numbers already used by existing records
                while str(next_num).zfill(2) in existing:
                    next_num += 1
                vals['category_number'] = str(next_num).zfill(2)
                existing.add(str(next_num).zfill(2))
                next_num += 1
        return super().create(vals_list)

    @api.model
    def _get_existing_category_numbers(self):
        """Return a set of all category_number values currently in the database."""
        cats = self.search([('category_number', '!=', False)])
        return {c.category_number for c in cats if c.category_number}

    @api.model
    def _get_next_category_number(self):
        """Auto-generate next available category number as 2-digit string (01, 02, ...).
        Scans all existing numeric category numbers and returns the lowest available one.
        """
        existing = self._get_existing_category_numbers()
        if not existing:
            return '01'
        num = 1
        while str(num).zfill(2) in existing:
            num += 1
        return str(num).zfill(2)
