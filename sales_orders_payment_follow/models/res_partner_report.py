# models/res_partner.py
from odoo import api, fields, models

class Partner(models.Model):
    _inherit = 'res.partner'

    # report fields
    so_count = fields.Integer(string="Sales Orders Count", compute='_compute_so_aggregates', store=True)
    total_so_amount = fields.Monetary(string="Total SO Amount", compute='_compute_so_aggregates', store=True, currency_field='currency_id')
    total_so_payments = fields.Monetary(string="Total SO Payments", compute='_compute_so_aggregates', store=True, currency_field='currency_id')
    total_so_refunds = fields.Monetary(string="Total SO Refunds", compute='_compute_so_aggregates', store=True, currency_field='currency_id')
    total_so_remaining = fields.Monetary(string="Total SO Remaining", compute='_compute_so_aggregates', store=True, currency_field='currency_id')
    total_free_payments = fields.Monetary(string="Total Free Payments", compute='_compute_so_aggregates', store=True, currency_field='currency_id')
    total_free_refunds = fields.Monetary(string="Total Free Refunds", compute='_compute_so_aggregates', store=True, currency_field='currency_id')

    @api.depends()  # we compute from db via _read_group
    def _compute_so_aggregates(self):
        partners = self
        if not partners:
            return
        SaleOrder = self.env['sale.order']
        APL = self.env['account.payment']

        # 1) SO aggregates: count and sum(amount_total) per partner
        # Odoo 19: _read_group replaces deprecated read_group
        so_grp = SaleOrder._read_group(
            [('partner_id', 'in', partners.ids)],
            ['partner_id'],
            ['amount_total:sum'],
        )
        so_map = {g['partner_id'][0]: g['amount_total'] for g in so_grp if g.get('partner_id')}

        # 2) payments assigned to SOs (paid inbound)
        pay_grp = APL._read_group(
            [
                ('partner_id', 'in', partners.ids),
                ('sale_order_id', '!=', False),
                ('state', '=', 'paid'),
                ('payment_type', '=', 'inbound'),
            ],
            ['partner_id'],
            ['amount:sum'],
        )
        pay_map = {g['partner_id'][0]: g['amount'] for g in pay_grp if g.get('partner_id')}

        # 3) refunds assigned to SOs (paid outbound)
        ref_grp = APL._read_group(
            [
                ('partner_id', 'in', partners.ids),
                ('sale_order_id', '!=', False),
                ('state', '=', 'paid'),
                ('payment_type', '=', 'outbound'),
            ],
            ['partner_id'],
            ['amount:sum'],
        )
        ref_map = {g['partner_id'][0]: g['amount'] for g in ref_grp if g.get('partner_id')}

        # 4) free payments (no sale_order_id) inbound
        free_pay_grp = APL._read_group(
            [
                ('partner_id', 'in', partners.ids),
                ('sale_order_id', '=', False),
                ('state', '=', 'paid'),
                ('payment_type', '=', 'inbound'),
            ],
            ['partner_id'],
            ['amount:sum'],
        )
        free_pay_map = {g['partner_id'][0]: g['amount'] for g in free_pay_grp if g.get('partner_id')}

        # 5) free refunds (no sale_order_id) outbound
        free_ref_grp = APL._read_group(
            [
                ('partner_id', 'in', partners.ids),
                ('sale_order_id', '=', False),
                ('state', '=', 'paid'),
                ('payment_type', '=', 'outbound'),
            ],
            ['partner_id'],
            ['amount:sum'],
        )
        free_ref_map = {g['partner_id'][0]: g['amount'] for g in free_ref_grp if g.get('partner_id')}

        for rec in partners:
            pid = rec.id
            rec.so_count = sum(1 for so in so_grp if so.get('partner_id') and so['partner_id'][0] == pid)
            rec.total_so_amount = so_map.get(pid, 0.0)
            rec.total_so_payments = pay_map.get(pid, 0.0)
            rec.total_so_refunds = ref_map.get(pid, 0.0)
            # remaining = total_so_amount - payments + refunds
            rec.total_so_remaining = rec.total_so_amount - rec.total_so_payments + rec.total_so_refunds
            rec.total_free_payments = free_pay_map.get(pid, 0.0)
            rec.total_free_refunds = free_ref_map.get(pid, 0.0)
