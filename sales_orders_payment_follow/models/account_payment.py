# -*- coding: utf-8 -*-

from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    sale_order_id = fields.Many2one('sale.order', domain="[('partner_id','=?',partner_id)]", string='Sale Order')

