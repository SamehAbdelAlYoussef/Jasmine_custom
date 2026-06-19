# -*- coding: utf-8 -*-
"""Extend ``sale.order`` with Shopify idempotency fields.

Adds ``x_shopify_id`` so every imported order carries a unique,
immutable reference to its Shopify source.  The sync job uses this
field — rather than the human-readable ``client_order_ref`` — to
detect duplicates in O(1) indexed lookups.
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
