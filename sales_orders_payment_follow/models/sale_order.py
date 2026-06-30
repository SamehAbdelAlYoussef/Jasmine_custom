# models/sale_order.py
from odoo import fields, models, _


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # per-order aggregated payments/refunds (filtered to this SO)
    so_payments = fields.Monetary(compute='_compute_so_payment_details', string='SO Payments', currency_field='currency_id')
    so_refunds = fields.Monetary(compute='_compute_so_payment_details', string='SO Refunds', currency_field='currency_id')
    so_remaining = fields.Monetary(compute='_compute_so_payment_details', string='SO Remaining', currency_field='currency_id')
    # store=True field MUST use separate compute method from non-stored fields (Odoo 19)
    amount_paid_percent = fields.Float(string='Paid (%)', compute='_compute_amount_paid_percent')
    confirm_on_percent = fields.Float(string='Confirm On (%)', default=100.0)

    # boolean to show alert on form if near threshold (>=49% and < confirm_on_percent)
    near_confirm_threshold = fields.Boolean(compute='_compute_near_confirm_threshold')

    def _compute_so_payment_details(self):
        """Compute payments, refunds, remaining (non-stored fields)."""
        for rec in self:
            APL = self.env['account.payment']
            payments = APL.search([
                ('state', 'in', ('in_process', 'paid')),
                ('payment_type', '=', 'inbound'),
                ('sale_order_id', '=', rec.id),
            ])
            refunds = APL.search([
                ('state', 'in', ('in_process', 'paid')),
                ('payment_type', '=', 'outbound'),
                ('sale_order_id', '=', rec.id),
            ])
            rec.so_payments = sum(payments.mapped('amount')) if payments else 0.0
            rec.so_refunds = sum(refunds.mapped('amount')) if refunds else 0.0
            rec.so_remaining = rec.amount_total - rec.so_payments + rec.so_refunds

    def _compute_amount_paid_percent(self):
        """Compute paid percent — always fresh, recomputed on every access."""
        for rec in self:
            rec.amount_paid_percent = (rec.so_payments / rec.amount_total * 100) if rec.amount_total else 0.0

    def _compute_near_confirm_threshold(self):
        for rec in self:
            confirm_on = rec.confirm_on_percent or 100.0
            rec.near_confirm_threshold = (rec.amount_paid_percent >= 49.0) and (rec.amount_paid_percent < confirm_on)

    # Actions to open payment lists filtered
    def action_open_so_payments(self):
        self.ensure_one()
        # ── Shopify integration: auto-sync payments before opening ───
        if hasattr(self, 'x_shopify_id') and self.x_shopify_id:
            import sys
            print(f">>> action_open_so_payments: syncing Shopify order {self.x_shopify_id}", file=sys.stderr, flush=True)
            sync_rec = self.env['shopify.sync'].search([], limit=1)
            if not sync_rec:
                sync_rec = self.env['shopify.sync'].create({'name': 'Shopify Sync'})
            sync_rec._fetch_and_sync_payments(
                self, shopify_order_id=self.x_shopify_id,
            )
        # ── /Shopify ──────────────────────────────────────────────────
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
        """Allow confirmation freely — payment gate removed per business requirement.

        Shopify orders are already paid on-platform; manual orders may
        receive payments after confirmation.  The ``confirm_on_percent``
        field is kept for reporting but no longer blocks confirmation.
        """
        return super(SaleOrder, self).action_confirm()
