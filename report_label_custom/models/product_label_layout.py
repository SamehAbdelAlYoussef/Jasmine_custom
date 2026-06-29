# -*- coding: utf-8 -*-

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductLabelLayout(models.TransientModel):
    _inherit = 'product.label.layout'

    print_format = fields.Selection(selection_add=[
        ('custom', 'Custom Label (38x25mm)'),
    ], ondelete={'custom': 'set default'})

    def _prepare_report_data(self):
        xml_id, data = super()._prepare_report_data()
        if self.print_format == 'custom':
            xml_id = 'report_label_custom.action_report_product_template_label_custom'
            data['price_included'] = True
        return xml_id, data

    def _get_custom_label_products(self):
        """Resolve wizard input to product.product records."""
        self.ensure_one()
        Product = self.env['product.product']

        if self.product_tmpl_ids:
            variants = Product.search([
                ('product_tmpl_id', 'in', self.product_tmpl_ids.ids),
            ])
            if not variants:
                raise UserError(_("No product variants found for the selected templates."))
            _logger.info("Custom label: resolving %d template(s) → %d variant(s)",
                        len(self.product_tmpl_ids), len(variants))
            return variants

        elif self.product_ids:
            _logger.info("Custom label: using %d product.product record(s) directly",
                        len(self.product_ids))
            return self.product_ids

        else:
            raise UserError(_("No product selected to print labels for."))

    def process(self):
        self.ensure_one()
        if self.print_format != 'custom':
            return super().process()

        xml_id, data = self._prepare_report_data()
        if not xml_id:
            raise UserError(_('Unable to find report template for %s format', self.print_format))

        products = self._get_custom_label_products()

        # كرر حسب الكمية
        docids = []
        for product in products:
            docids.extend([product.id] * self.custom_quantity)

        _logger.info("Custom label: %d product(s) × %d copies = %d label(s)",
                    len(products), self.custom_quantity, len(docids))

        if not docids:
            raise UserError(_("No products to print."))

        return self.env.ref(xml_id).report_action(docids, data=data)