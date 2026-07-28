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

    # ------------------------------------------------------------
    # Direct Invoice Creation — auto-confirm + invoice + stay on SO
    # ------------------------------------------------------------
    def action_create_invoice_direct(self):
        """Auto-confirm the SO (if needed), validate delivery, create
        and post the invoice directly — no wizard, no redirect."""
        self.ensure_one()

        # 1 — Confirm the order if not yet done
        if self.state in ('draft', 'sent'):
            res = self.action_confirm()
            if isinstance(res, dict) and res.get('type') != 'ir.actions.act_window_close':
                return res
            self.invalidate_recordset(['state'])

        # 2 — Auto-validate any outstanding deliveries
        pickings_to_validate = self.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel')
        )
        for picking in pickings_to_validate:
            if picking.state in ('draft', 'waiting', 'confirmed'):
                picking.action_assign()
            if picking.state != 'assigned':
                continue
            for move in picking.move_ids:
                if move.product_uom_qty > 0 and not move.quantity:
                    move.quantity = move.product_uom_qty
            picking.with_context(skip_backorder=True).button_validate()
        # Flush + invalidate to pick up newly delivered quantities
        self.env.flush_all()
        self.invalidate_recordset()
        self.order_line.invalidate_recordset()

        # 3 — Ensure every line is invoiceable: for delivery-policy
        #     products whose qty_delivered may still be 0 (e.g. no
        #     stock or compute lag), force-set it to the ordered qty.
        for line in self.order_line:
            if line.display_type:
                continue
            if line.product_id.invoice_policy == 'delivery' \
                    and not line.qty_delivered:
                line.qty_delivered = line.product_uom_qty

        # 4 — Already invoiced?  Notify and stay
        if self.invoice_status == 'invoiced':
            self.message_post(
                body=_('Invoice already exists: %(invoices)s.',
                       invoices=', '.join(self.invoice_ids.mapped('name'))))
            return self._notify(
                _('Already invoiced.'), 'warning',
            )

        # 5 — Create & post the invoice
        invoices = self._create_invoices()
        if invoices:
            # Post (validate) the invoice immediately
            try:
                invoices.action_post()
            except Exception:
                pass  # stays draft if posting fails
            self.message_post(
                body=_('Invoice %(name)s created automatically.',
                       name=invoices[0].name))
            return self._notify(
                _('Invoice %(name)s created.', name=invoices[0].name),
                'success',
            )
        return self._notify(
            _('No invoiceable lines found.'), 'danger',
        )

    def _notify(self, message, msg_type):
        """Return a client action that shows a toast notification."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sales Visit Plan'),
                'message': message,
                'type': msg_type,
                'sticky': False,
            },
        }

    def action_confirm(self):
        """Confirm the SO and auto-validate the delivery picking.

        After the standard confirmation flow (which creates the stock
        picking via procurement rules), this method:

        1. Reserves / assigns the picking (``action_assign``) if not yet ready.
        2. Sets done quantities on moves to their full demand so the entire
           delivery ships immediately.
        3. Calls ``button_validate`` to complete the transfer without the
           backorder wizard (``skip_backorder=True``).

        Already-done and cancelled pickings are skipped.
        """
        res = super(SaleOrder, self).action_confirm()

        # Auto-validate delivery pickings linked to this sale order
        pickings_to_validate = self.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel')
        )
        for picking in pickings_to_validate:
            # Reserve stock if not already assigned
            if picking.state in ('draft', 'waiting', 'confirmed'):
                picking.action_assign()

            # If still not assigned (e.g. not enough stock), skip
            if picking.state != 'assigned':
                continue

            # Mark full demand as done on each move (the inverse `_set_quantity`
            # distributes the quantity across the move lines automatically)
            for move in picking.move_ids:
                if move.product_uom_qty > 0 and not move.quantity:
                    move.quantity = move.product_uom_qty

            # Validate without showing the backorder wizard
            picking.with_context(skip_backorder=True).button_validate()

        return res
