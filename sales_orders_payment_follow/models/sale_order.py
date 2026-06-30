# models/sale_order.py
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # per-order aggregated payments/refunds (filtered to this SO)
    so_payments = fields.Monetary(compute='_compute_so_payment_details', string='SO Payments', currency_field='currency_id')
    so_refunds = fields.Monetary(compute='_compute_so_payment_details', string='SO Refunds', currency_field='currency_id')
    so_remaining = fields.Monetary(compute='_compute_so_payment_details', string='SO Remaining', currency_field='currency_id')
    # store=True field MUST use separate compute method from non-stored fields (Odoo 19)
    amount_paid_percent = fields.Float(string='Paid (%)', compute='_compute_amount_paid_percent', store=True)
    confirm_on_percent = fields.Float(string='Confirm On (%)', default=100.0)

    # boolean to show alert on form if near threshold (>=49% and < confirm_on_percent)
    near_confirm_threshold = fields.Boolean(compute='_compute_near_confirm_threshold')

    @api.depends('amount_total')
    def _compute_so_payment_details(self):
        """Compute payments, refunds, remaining (non-stored fields)."""
        for rec in self:
            APL = self.env['account.payment']
            payments = APL.search([
                ('state', '=', 'paid'),
                ('payment_type', '=', 'inbound'),
                ('sale_order_id', '=', rec.id),
            ])
            refunds = APL.search([
                ('state', '=', 'paid'),
                ('payment_type', '=', 'outbound'),
                ('sale_order_id', '=', rec.id),
            ])
            rec.so_payments = sum(payments.mapped('amount')) if payments else 0.0
            rec.so_refunds = sum(refunds.mapped('amount')) if refunds else 0.0
            rec.so_remaining = rec.amount_total - rec.so_payments + rec.so_refunds

    @api.depends('so_payments', 'amount_total')
    def _compute_amount_paid_percent(self):
        """Compute paid percent (stored field — separate from non-stored fields for Odoo 19)."""
        for rec in self:
            rec.amount_paid_percent = (rec.so_payments / rec.amount_total * 100) if rec.amount_total else 0.0

    @api.depends('amount_paid_percent', 'confirm_on_percent')
    def _compute_near_confirm_threshold(self):
        for rec in self:
            confirm_on = rec.confirm_on_percent or 100.0
            rec.near_confirm_threshold = (rec.amount_paid_percent >= 49.0) and (rec.amount_paid_percent < confirm_on)

    # Actions to open payment lists filtered
    def action_open_so_payments(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('SO Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id), ('payment_type', '=', 'inbound')],
            'context': {'default_partner_id': self.partner_id.id, 'default_sale_order_id': self.id},
        }
        return action

    def action_open_so_refunds(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('SO Refunds'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id), ('payment_type', '=', 'outbound')],
            'context': {'default_partner_id': self.partner_id.id, 'default_sale_order_id': self.id},
        }
        return action

    def action_open_so_remaining(self):
        pass

    # Open payment creation popup (advance payment)
    def action_open_create_payment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Advance Payment'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_payment_type': 'inbound',
                'default_sale_order_id': self.id,
            },
        }

    # Open refund creation popup
    def action_open_create_refund(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Refund'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_payment_type': 'outbound',
                'default_sale_order_id': self.id,
            },
        }

    def action_confirm(self):
        """Prevent confirming if paid percentage < required confirm_on_percent."""
        for rec in self:
            rec._compute_so_payment_details()

            # Fallback to 100% if user left field empty
            confirm_on = rec.confirm_on_percent or 100.0

            # Validation condition
            if rec.amount_paid_percent < confirm_on:
                raise ValidationError(_(
                    "You can't confirm this order.\n"
                    "Customer has paid only %.2f%% but %.2f%% is required."
                ) % (rec.amount_paid_percent, confirm_on))

        # If passes validation → confirm normally
        return super(SaleOrder, self).action_confirm()
