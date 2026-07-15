# -*- coding: utf-8 -*-
"""Wizard for importing Shopify orders by date range.

Opened from Settings → Shopify or from the Shopify Sync menu.
User picks *date_from* and *date_to*, clicks "Start Import", and the
wizard pages through **all** Shopify orders created in that window.
Orders that already exist in Odoo (matched by ``x_shopify_id``) are
skipped; missing ones are created via ``_process_single_order``.
"""
import logging

import requests

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Commit every N created orders to avoid long-running transactions
COMMIT_INTERVAL = 50


class ShopifyOrderImportWizard(models.TransientModel):
    _name = 'shopify.order.import.wizard'
    _description = 'Import Shopify Orders by Date Range'

    # -- input fields -------------------------------------------------------
    date_from = fields.Date(
        string='From Date',
        required=True,
        help="Fetch orders created on or after this date.",
    )
    date_to = fields.Date(
        string='To Date',
        required=True,
        default=fields.Date.today,
        help="Fetch orders created on or before this date.",
    )

    # -- progress fields (read-only) ----------------------------------------
    state = fields.Selection(
        selection=[
            ('draft', 'Ready'),
            ('running', 'Importing...'),
            ('done', 'Complete'),
        ],
        string='Status', default='draft', readonly=True,
    )
    orders_fetched = fields.Integer(
        string='Orders Fetched from API', readonly=True,
    )
    orders_created = fields.Integer(
        string='Orders Created in Odoo', readonly=True,
    )
    orders_skipped = fields.Integer(
        string='Orders Skipped (Already Exist)', readonly=True,
    )
    orders_failed = fields.Integer(
        string='Orders Failed', readonly=True,
    )

    # ------------------------------------------------------------------
    # Main import action
    # ------------------------------------------------------------------

    def action_import_orders(self):
        """Fetch ALL Shopify orders in the selected date range and create
        sale orders for any that don't already exist in Odoo.

        Uses cursor pagination (``rel="next"`` Link header) to page
        through every order.  Each order is checked against
        ``sale.order.x_shopify_id`` before creation (idempotent — safe
        to re-run for the same date range).
        """
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()
        shop = ICP.get_param('shopify.shop_url')
        token = ICP.get_param('shopify.access_token')

        if not shop or not token:
            return self._notify(
                'Configuration Missing',
                'Configure Shop URL and Access Token in Settings → Shopify first.',
                'danger',
            )

        api_version = ICP.get_param('shopify.api_version') or '2024-10'
        base_url = f"https://{shop}/admin/api/{api_version}"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        # ISO-8601 datetime strings (store timezone implied)
        date_from_str = f"{self.date_from.isoformat()}T00:00:00"
        date_to_str = f"{self.date_to.isoformat()}T23:59:59"

        page_url = (
            f"{base_url}/orders.json"
            f"?status=any"
            f"&created_at_min={date_from_str}"
            f"&created_at_max={date_to_str}"
            f"&limit=250"
        )

        self.write({
            'state': 'running',
            'orders_fetched': 0, 'orders_created': 0,
            'orders_skipped': 0, 'orders_failed': 0,
        })

        # -- ensure a sync helper record exists -------------------------
        Sync = self.env['shopify.sync'].sudo()
        sync = Sync.search([], limit=1)
        if not sync:
            sync = Sync.create({'name': 'Shopify Sync'})

        sync_ctx = sync.with_context(
            mail_create_nosubscribe=True,
            mail_notrack=True,
            tracking_disable=True,
        )

        total_fetched = 0
        total_created = 0
        total_skipped = 0
        total_failed = 0
        created_since_commit = 0

        _logger.info(
            "Shopify import: date range %s → %s [STARTING]",
            date_from_str, date_to_str,
        )

        # ── Page through ALL orders in the date range ──────────────────
        while page_url:
            try:
                resp = requests.get(page_url, headers=headers, timeout=30)
                sync._check_rate_limit(resp.headers)
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.RequestException as exc:
                _logger.error("Shopify import: API error — %s", exc)
                break

            orders = data.get('orders', [])
            if not orders:
                break

            for order_data in orders:
                total_fetched += 1
                shopify_id = str(order_data.get('id', '?'))
                order_number = order_data.get('order_number', shopify_id)

                try:
                    result = sync_ctx._process_single_order(order_data)
                    if result and result.get('status') == 'created':
                        total_created += 1
                        created_since_commit += 1
                        _logger.info(
                            "Shopify import: ✅ #%s → SO #%s",
                            order_number, result.get('sale_order_id'),
                        )
                    else:
                        total_skipped += 1
                except Exception as exc:
                    total_failed += 1
                    _logger.warning(
                        "Shopify import: ❌ #%s failed — %s",
                        order_number, exc,
                    )

                # Commit every COMMIT_INTERVAL created orders
                if created_since_commit >= COMMIT_INTERVAL:
                    self.env.cr.commit()
                    self.invalidate_recordset()
                    created_since_commit = 0
                    _logger.info(
                        "Shopify import: committed at %d created",
                        total_created,
                    )

            # ── Follow Link header for next page ───────────────────────
            page_url = None
            link_header = resp.headers.get('Link', '')
            for part in link_header.split(','):
                if 'rel="next"' in part:
                    page_url = part.split(';')[0].strip(' <>')
                    break

            # Update progress (visible in the wizard while running)
            self.write({
                'orders_fetched': total_fetched,
                'orders_created': total_created,
                'orders_skipped': total_skipped,
                'orders_failed': total_failed,
            })
            _logger.info(
                "Shopify import: fetched=%d created=%d skipped=%d failed=%d",
                total_fetched, total_created, total_skipped, total_failed,
            )

        # ── Final commit & status ──────────────────────────────────────
        if created_since_commit > 0:
            self.env.cr.commit()
            self.invalidate_recordset()

        self.write({
            'state': 'done',
            'orders_fetched': total_fetched,
            'orders_created': total_created,
            'orders_skipped': total_skipped,
            'orders_failed': total_failed,
        })

        _logger.info(
            "Shopify import: date range %s → %s [DONE] — "
            "fetched=%d created=%d skipped=%d failed=%d",
            date_from_str, date_to_str,
            total_fetched, total_created, total_skipped, total_failed,
        )

        return self._notify(
            'Import Complete',
            (
                f"Date Range: {self.date_from} → {self.date_to}\n"
                f"Orders Fetched from Shopify: {total_fetched}\n"
                f"Created in Odoo: {total_created}\n"
                f"Skipped (already exist): {total_skipped}\n"
                f"Failed: {total_failed}"
            ),
            'success' if total_failed == 0 else 'warning',
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _notify(title, message, msg_type):
        """Return a client-action notification dict."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': msg_type,
                'sticky': True,
            },
        }
