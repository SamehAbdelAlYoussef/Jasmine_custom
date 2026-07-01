# -*- coding: utf-8 -*-
"""Extend ``account.payment`` with Shopify transaction tracking fields.

Adds ``x_shopify_txn_id`` for idempotent payment import and
``x_shopify_gateway`` for reporting by payment gateway.

When ``sales_orders_payment_follow`` is installed, its ``sale_order_id``
field links Shopify payments back to sale orders, enabling the computed
fields ``so_payments``, ``so_refunds``, and ``so_remaining``.
"""

from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    x_shopify_txn_id = fields.Char(
        string='Shopify Transaction ID',
        index=True,
        copy=False,
        readonly=True,
        help="Globally-unique Shopify transaction ID.  Used by the sync "
             "engine to prevent duplicate payment imports.",
    )

    x_shopify_gateway = fields.Char(
        string='Shopify Gateway',
        copy=False,
        readonly=True,
        help="Original Shopify payment gateway name (e.g. 'Geidea Pay', "
             "'Cash on Delivery (COD)', 'Paymob').",
    )

    # Idempotency is enforced at Python level in _fetch_and_sync_payments
    # via x_shopify_txn_id search — no DB constraint needed.
