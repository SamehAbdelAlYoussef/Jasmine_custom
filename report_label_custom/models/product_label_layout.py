# -*- coding: utf-8 -*-
import logging
import base64
from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductLabelLayout(models.TransientModel):
    _inherit = 'product.label.layout'

    print_format = fields.Selection(
        [('custom', 'Custom Label (38x25mm)')],
        string="Format",
        default='custom',
        required=True,
    )

    def _generate_barcode_base64(self, barcode_value):
        """توليد barcode كـ base64 مباشرة بدون HTTP request."""
        try:
            from odoo.tools.image import image_process
            # استخدم Odoo built-in barcode generator
            barcode_obj = self.env['ir.actions.report'].barcode(
                'Code128', barcode_value, width=180, height=60, humanreadable=False
            )
            encoded = base64.b64encode(barcode_obj).decode('utf-8')
            return 'data:image/png;base64,%s' % encoded
        except Exception as e:
            _logger.warning("Barcode generation failed for %s: %s", barcode_value, e)
            return ''

    def _prepare_report_data(self):
        if self.print_format != 'custom':
            return super()._prepare_report_data()

        if self.custom_quantity <= 0:
            raise UserError(_('You need to set a positive quantity.'))

        if self.product_tmpl_ids:
            products = self.env['product.product'].sudo().search([
                ('product_tmpl_id', 'in', self.product_tmpl_ids.ids),
                ('active', '=', True),
            ])
            if not products:
                raise UserError(_("No active variants found."))
        elif self.product_ids:
            products = self.product_ids.sudo()
        else:
            raise UserError(_("No product selected."))

        qty = self.custom_quantity
        products_data = []

        for product in products:
            # ✅ توليد barcode base64 مرة واحدة لكل منتج
            barcode_src = ''
            if product.barcode:
                barcode_src = self._generate_barcode_base64(product.barcode)

            for _ in range(qty):
                products_data.append({
                    'id': product.id,
                    'name': product.name,
                    'barcode': product.barcode or '',
                    'barcode_src': barcode_src,  # ✅ base64 مباشرة
                    'list_price': product.list_price,
                    'currency_symbol': product.currency_id.symbol or '',
                })

        xml_id = 'report_label_custom.action_report_product_label_custom'
        data = {
            'active_model': 'product.product',
            'quantity_by_product': {p.id: qty for p in products},
            'layout_wizard': self.id,
            'price_included': True,
            'products_data': products_data,
        }
        return xml_id, data

    def process(self):
        self.ensure_one()
        if self.print_format != 'custom':
            return super().process()

        xml_id, data = self._prepare_report_data()
        if not xml_id:
            raise UserError(_(
                'Unable to find report template for %s format', self.print_format
            ))

        report_action = self.env.ref(xml_id).report_action(
            None, data=data, config=False
        )
        report_action.update({'close_on_report_download': True})
        return report_action