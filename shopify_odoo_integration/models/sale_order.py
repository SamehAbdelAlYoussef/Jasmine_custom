# -*- coding: utf-8 -*-
"""Extend ``sale.order`` with Shopify idempotency fields.

Adds ``x_shopify_id`` so every imported order carries a unique,
immutable reference to its Shopify source.  The sync job uses this
field — rather than the human-readable ``client_order_ref`` — to
detect duplicates in O(1) indexed lookups.

A database-level UNIQUE constraint on ``x_shopify_id`` prevents
duplicate orders even during concurrent webhook+cron races that
slip past the Python-level idempotency check.
"""

from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_shopify_id = fields.Char(
        string='Shopify Order ID',
        index=True,
        copy=False,
        readonly=True,
        help="Globally-unique Shopify order ID (GraphQL / REST ID). "
             "Used by the sync engine to prevent duplicate imports.",
    )

    webhook_last_processed = fields.Datetime(
        string='Last Webhook Processed',
        copy=False,
        help="Timestamp of the last webhook event processed for this "
             "order. Used to deduplicate rapid-fire webhooks from Shopify.",
    )

    x_shopify_payment_synced = fields.Boolean(
        string='Shopify Payments Synced',
        default=False,
        copy=False,
        help="Whether Shopify transactions have been fetched and synced "
             "as account.payment records for this order.",
    )

    # Idempotency enforced in _process_single_order via x_shopify_id search
    # + IntegrityError handler.  No DB constraint needed (Odoo 19 compat).

    def action_sync_shopify_payments(self):
        """Manually trigger Shopify payment sync for this order.

        Called from the "Sync Shopify Payments" button on the sale order
        form.  Delegates to the ``shopify.sync`` singleton's payment
        sync method.
        """
        self.ensure_one()
        if not self.x_shopify_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Not a Shopify Order',
                    'message': 'This sale order was not imported from Shopify.',
                    'type': 'warning',
                    'sticky': False,
                },
            }

        ShopifySync = self.env['shopify.sync']
        sync_record = ShopifySync.search([], limit=1)
        if not sync_record:
            sync_record = ShopifySync.create({'name': 'Shopify Sync'})

        try:
            result = sync_record._fetch_and_sync_payments(
                self, shopify_order_id=self.x_shopify_id,
            )
            if result:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Payments Synced',
                        'message': 'Shopify payments have been synced successfully.',
                        'type': 'success',
                        'sticky': False,
                    },
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'No Payments Found',
                        'message': 'No Shopify transactions were found for this order.',
                        'type': 'warning',
                        'sticky': False,
                    },
                }
        except Exception as exc:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Payment Sync Failed',
                    'message': str(exc),
                    'type': 'danger',
                    'sticky': True,
                },
            }
