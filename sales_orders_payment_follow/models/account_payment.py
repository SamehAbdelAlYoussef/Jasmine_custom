# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
import logging

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = "account.payment"

    sale_order_id = fields.Many2one('sale.order', domain="[('partner_id','=?',partner_id)]", string='Sale Order')

    @api.onchange('sale_order_id')
    def _onchange_sale_order_readonly_fields(self):
        """If the form opened from SO context, make fields readonly in UI."""
        ctx_so_id = self.env.context.get('default_sale_order_id')
        if ctx_so_id:
            # dynamically mark the fields as readonly
            for field_name in ['payment_type', 'sale_order_id']:
                self._fields[field_name].readonly = True

