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

        # Odoo 19 _read_group returns list of tuples: (groupby_val, ..., aggregate_val, ...)
        # Each groupby_val for m2o is a tuple: (id, display_name)

        # 1) SO aggregates: count and sum(amount_total) per partner
        so_grp = SaleOrder._read_group(
            [('partner_id', 'in', partners.ids)],
            ['partner_id'],
            ['amount_total:sum', '__count'],
        )
        # so_grp format: [( (partner_id, name), amount_total_sum, __count ), ...]
        so_map = {g[0][0]: g[1] for g in so_grp if g[0]}
        so_counts = {g[0][0]: g[2] for g in so_grp if g[0]}

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
        pay_map = {g[0][0]: g[1] for g in pay_grp if g[0]}

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
        ref_map = {g[0][0]: g[1] for g in ref_grp if g[0]}

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
        free_pay_map = {g[0][0]: g[1] for g in free_pay_grp if g[0]}

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
        free_ref_map = {g[0][0]: g[1] for g in free_ref_grp if g[0]}

        for rec in partners:
            pid = rec.id
            # count SOs for this partner from _read_group results
            rec.so_count = so_counts.get(pid, 0)
            rec.total_so_amount = so_map.get(pid, 0.0)
            rec.total_so_payments = pay_map.get(pid, 0.0)
            rec.total_so_refunds = ref_map.get(pid, 0.0)
            # remaining = total_so_amount - payments + refunds
            rec.total_so_remaining = rec.total_so_amount - rec.total_so_payments + rec.total_so_refunds
            rec.total_free_payments = free_pay_map.get(pid, 0.0)
            rec.total_free_refunds = free_ref_map.get(pid, 0.0)
