# -*- coding: utf-8 -*-

from odoo import api, fields, models


class Partner(models.Model):
    _inherit = 'res.partner'

    t_amount_so = fields.Monetary(compute="compute_total_amount_so", currency_field='currency_id')
    t_count_so = fields.Integer(compute="compute_total_amount_so")
    t_payments_so = fields.Monetary(
        compute="compute_total_payments_report",
        string="T Payment SO",
        currency_field='currency_id',
        store=False
    )
    t_refunds_so = fields.Monetary(
        compute="compute_total_payments_report",
        string="T Refunds SO",
        currency_field='currency_id',
        store=False
    )
    t_remaining_so = fields.Monetary(
        compute="compute_total_payments_report",
        string="T Remining SO",
        currency_field='currency_id',
        store=False
    )
    t_paid_amount = fields.Monetary(
        compute="_compute_total_paid_receivable",
        string="Total Paid",
        currency_field='currency_id',
        store=False
    )
    t_refunded = fields.Monetary(compute="_compute_total_refunded_receivable")
    t_remaining_amount = fields.Monetary(compute="compute_total_remaining",currency_field='currency_id')
    r_balance = fields.Monetary(compute="compute_total_balance", currency_field='currency_id')


    # t_amount_so = fields.Integer(compute="compute_total_amount_so", store=True, string="Orders Count")
    # t_paid_count = fields.Integer(compute="compute_total_paid", store=True, string="Payments Count")
    # t_remaining_count = fields.Float(compute="compute_total_remaining", store=True, string="Remaining Amount")
    # r_balance = fields.Float(compute="compute_total_balance", store=True, string="Account Balance")
    # t_refunded_so = fields.Float(compute="compute_refunded", store=True, string="Refunded Amount")

    def refunded_balance(self):
        pass

    # def compute_refunded(self):
    #     for rec in self:
            # payment_ids = self.env['account.payment'].search(
            #     [('partner_id', '=', self.id), ('state', '=', 'posted'), ('payment_type', '=', 'outbound')])
            # refunded_amount = sum(payment_ids.mapped('amount'))
            # rec.t_refunded_so = refunded_amount
    def _compute_total_refunded_receivable(self):
        """Compute total payments as refunds from receivable account lines."""
        aml = self.env['account.move.line']
        for rec in self:
            if not rec.id or str(rec.id).startswith('NewId'):
                rec.t_refunded = 0.0
                continue
            total_debit = 0.0
            try:
                if rec.property_account_receivable_id:
                    move_lines = aml.search([
                        ('partner_id', '=', rec.id),
                        ('account_id', '=', rec.property_account_receivable_id.id),
                        ('parent_state', '=', 'posted'),
                        ('journal_id.type', 'in', ['bank', 'cash']),
                    ])
                    total_debit = sum(move_lines.mapped('debit'))
            except Exception:
                pass
            rec.t_refunded = total_debit


    def action_open_paid_invoice(self):
        pass

    def compute_total_amount_so(self):
        for rec in self:
            if not rec.id or str(rec.id).startswith('NewId'):
                rec.t_amount_so = 0.0
                rec.t_count_so = 0
                continue
            rec.t_amount_so = sum(total_so_list)
            rec.t_count_so = len(total_so_list)

    def compute_total_payments_report(self):
        for rec in self:
            if not rec.id or str(rec.id).startswith('NewId'):
                rec.t_payments_so = 0.0
                rec.t_refunds_so = 0.0
                rec.t_remaining_so = 0.0
                continue
            paid_inbound_amount_so = self.env['account.payment'].search(
                [('partner_id', '=', rec.id), ('state', 'in', ('in_process', 'paid')), ('payment_type', '=', 'inbound'),('sale_order_id', '!=', False),]).mapped(
                'amount')
            paid_outbound_amount_so = self.env['account.payment'].search(
                [('partner_id', '=', rec.id), ('state', 'in', ('in_process', 'paid')), ('payment_type', '=', 'outbound'),('sale_order_id', '!=', False),]).mapped(
                'amount')
            rec.t_payments_so = sum(paid_inbound_amount_so)
            rec.t_refunds_so = sum(paid_outbound_amount_so)
            rec.t_remaining_so = rec.t_amount_so - rec.t_payments_so + rec.t_refunds_so



    # @api.depends()
    # def compute_total_paid(self):
    #     for rec in self:
            # Fetch posted inbound payments linked to this partner
            # payments = self.env['account.payment'].search([
            #     ('partner_id', '=', rec.id),
            #     ('state', '=', 'posted'),
            #     ('payment_type', '=', 'inbound')
            # ])
            # rec.t_paid_amount = sum(payments.mapped('amount_company_currency_signed'))
            # rec.t_paid_amount = rec.credit * -1

    # @api.depends()
    # def _compute_total_paid_receivable(self):
    #     """Compute total payments from receivable account lines
    #     restricted to moves from cash/bank journals only.
    #     """
    #     aml = self.env['account.move.line']
    #     for rec in self:
    #         total_credit = 0.0
    #         if rec.property_account_receivable_id:
    #             # Fetch posted move lines from receivable account for this partner
    #             move_lines = aml.search([
    #                 ('partner_id', '=', rec.id),
    #                 ('account_id', '=', rec.property_account_receivable_id.id),
    #                 ('parent_state', '=', 'posted'),
    #                 ('journal_id.type', 'in', ['bank', 'cash']),
    #             ])
    #             # Credits represent money received (actual payments)
    #             total_credit = sum(move_lines.mapped('credit'))
    #         rec.t_paid_amount = total_credit

    def _compute_total_paid_receivable(self):
        aml = self.env['account.move.line']
        for rec in self:
            # Skip virtual records (NewId — not yet saved)
            if not rec.id or str(rec.id).startswith('NewId'):
                rec.t_paid_amount = 0.0
                continue
            try:
                if not rec.property_account_receivable_id:
                    rec.t_paid_amount = 0.0
                    continue
                domain = [
                    ('partner_id', '=', rec.id),
                    ('account_id', '=', rec.property_account_receivable_id.id),
                    ('parent_state', '=', 'posted'),
                    ('journal_id.type', 'in', ['bank', 'cash']),
                ]
                grouped = aml.read_group(domain, ['credit:sum'], [])
                credit = grouped[0]['credit'] if grouped and grouped[0].get('credit') else 0.0
                rec.t_paid_amount = credit
            except Exception:
                rec.t_paid_amount = 0.0


    def compute_total_remaining(self):
        for rec in self:
            try:
                rec.t_remaining_amount = rec.t_amount_so - rec.t_paid_amount + rec.t_refunded
            except Exception:
                rec.t_remaining_amount = 0.0
            # total_remaning_recieve_amount = self.env['account.payment'].search(
            #     [('partner_id', '=', rec.id), ('state', '=', 'posted'), ('payment_type', '=', 'inbound')]).mapped(
            #     'amount_company_currency_signed')
            # total_remaning_recieve_amount = sum(total_remaning_recieve_amount)
            # total_sale_order_amount = self.env['sale.order'].search([('partner_id', '=', rec.id)]).mapped(
            #     'amount_total')
            # total_sale_order_amount = sum(total_sale_order_amount)
            # remain_balance = total_sale_order_amount - total_remaning_recieve_amount + rec.t_refunded
            # rec.t_remaining_count = remain_balance


    @api.depends('credit', 'debit')
    def compute_total_balance(self):
        for rec in self:
            remain_balance = rec.credit - rec.debit
            rec.r_balance = remain_balance