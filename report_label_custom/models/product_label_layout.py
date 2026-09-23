# -*- coding: utf-8 -*-
import logging
import base64
import io
from odoo import _, api, fields, models
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
    partner_id = fields.Many2one(
        'res.partner',
        string='Partner',
        compute='_compute_partner_id',
        readonly=True,
        help='Partner for the label',
    )

    @api.depends('move_ids.picking_id.partner_id')
    def _compute_partner_id(self):
        for record in self:
            record.partner_id = record.move_ids[:1].picking_id.partner_id if record.move_ids else False

    def _prepare_report_data(self):
        if self.print_format != 'custom':
            return super()._prepare_report_data()

        if self.custom_quantity <= 0:
            raise UserError('You need to set a positive quantity.')

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
        x_vendor_code = self.partner_id.x_vendor_code or '' if self.partner_id else ''

        for move in self.move_ids:
            product = move.product_id
            for _ in range(int(move.quantity)):
                products_data.append({
                    'id': product.id,
                    'name': product.product_tmpl_id.name,
                    'barcode': product.default_code or '',
                    'list_price': product.list_price,
                    'currency_symbol': product.currency_id.symbol or '',
                    'x_size': product.x_size or '',
                    'x_vendor_code': x_vendor_code,
                })

        xml_id = 'report_label_custom.action_report_product_label_custom'
        data = {
            'x_vendor_code': self.partner_id.x_vendor_code,
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

        products_data = data.get('products_data', [])
        BATCH_SIZE = 80

        if len(products_data) <= BATCH_SIZE:
            report_action = self.env.ref(xml_id).report_action(None, data=data, config=False)
            report_action.update({'close_on_report_download': True})
            return report_action

        # Split into batches and merge PDFs to avoid wkhtmltopdf crash on large receipts
        report = self.env.ref(xml_id)
        batches = [products_data[i:i + BATCH_SIZE] for i in range(0, len(products_data), BATCH_SIZE)]
        pdf_parts = []

        for batch in batches:
            batch_data = dict(data)
            batch_data['products_data'] = batch
            pdf_content, _ = report._render_qweb_pdf(xml_id, data=batch_data)
            pdf_parts.append(pdf_content)

        # Merge all PDF parts
        from PyPDF2 import PdfMerger
        merger = PdfMerger()
        for pdf_bytes in pdf_parts:
            merger.append(io.BytesIO(pdf_bytes))

        output = io.BytesIO()
        merger.write(output)
        merger.close()
        merged_pdf = output.getvalue()

        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'product_labels.pdf',
            'datas': base64.b64encode(merged_pdf),
            'mimetype': 'application/pdf',
            'res_model': 'product.label.layout',
            'res_id': self.id,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%d?download=true' % attachment.id,
            'target': 'self',
            'close_on_report_download': True,
        }
