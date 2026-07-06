# -*- coding: utf-8 -*-
"""Shopify webhook controller — handles real-time order events.

Supported Shopify topics (configure these in Shopify Admin → Settings → Notifications → Webhooks):

    - orders/create    → create sale.order
    - orders/updated   → update existing sale.order lines / totals / state
    - orders/cancelled → cancel sale.order in Odoo
    - orders/delete    → delete sale.order from Odoo
    - orders/paid      → confirm sale.order (draft → sale) -- + mark invoiced
    - orders/fulfilled → post a note on the SO

All endpoints read the ``X-Shopify-Topic`` header to distinguish the
event type, so a single URL can receive every topic if desired, but
separate webhook registrations per topic are recommended.
"""

import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ShopifyWebhookController(http.Controller):
    """Receive Shopify webhook events and dispatch to sync logic."""

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _get_sync_record():
        """Return the singleton ``shopify.sync`` record (create if missing)."""
        Sync = request.env['shopify.sync'].sudo()
        rec = Sync.search([], limit=1)
        return rec or Sync.create({'name': 'Shopify Sync'})

    @staticmethod
    def _parse_json():
        """Parse and return the incoming JSON body, or None."""
        try:
            return json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError) as e:
            _logger.error("Shopify webhook: invalid JSON — %s", e)
            return None

    @staticmethod
    def _ok(body=None):
        return request.make_response(
            json.dumps(body or {'status': 'ok'}),
            headers={'Content-Type': 'application/json'},
        )

    @staticmethod
    def _error(msg, status=400):
        return request.make_response(
            json.dumps({'status': 'error', 'message': str(msg)}),
            headers={'Content-Type': 'application/json'},
            status=status,
        )

    # -- main entry point (single URL for all topics) -------------------
    @http.route(
        '/shopify/webhook/orders',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_event(self, **kwargs):
        """Dispatch based on ``X-Shopify-Topic`` header.

        If the header is missing, defaults to ``orders/create`` for
        backward compatibility.
        """
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        topic = request.httprequest.headers.get('X-Shopify-Topic', 'orders/create')
        order_ref = order_data.get('order_number', order_data.get('id', '?'))
        _logger.info("Shopify webhook: [%s] order #%s", topic, order_ref)

        sync = self._get_sync_record()

        try:
            if topic == 'orders/cancelled':
                result = sync._cancel_sale_order(order_data)
            elif topic == 'orders/delete':
                result = sync._delete_sale_order(order_data)
            elif topic in ('orders/updated', 'orders/edited'):
                result = sync._update_sale_order(order_data)
            elif topic == 'orders/paid':
                result = sync._mark_order_paid(order_data)
            elif topic == 'orders/fulfilled':
                result = sync._note_fulfillment(order_data)
            else:
                # orders/create or unknown → create / skip
                result = sync._process_single_order(order_data)

            _logger.info(
                "Shopify webhook: [%s] order #%s → %s",
                topic, order_ref, result.get('status', '?'),
            )
            return self._ok(result)

        except Exception as exc:
            _logger.error(
                "Shopify webhook: [%s] order #%s FAILED — %s",
                topic, order_ref, exc, exc_info=True,
            )
            return self._error(str(exc), 500)

    # -- dedicated route for orders/create (optional, backwards compat) -
    @http.route(
        '/shopify/webhook/orders/create',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_create(self, **kwargs):
        """Handle orders/create webhook — dedicated URL."""
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        order_ref = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: [orders/create] order #%s", order_ref)

        try:
            sync = self._get_sync_record()
            result = sync._process_single_order(order_data)
            return self._ok(result)
        except Exception as exc:
            _logger.error("Shopify webhook: create order #%s failed — %s", order_ref, exc)
            return self._error(str(exc), 500)

    # -- dedicated route for orders/updated -----------------------------
    @http.route(
        '/shopify/webhook/orders/updated',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_updated(self, **kwargs):
        """Handle orders/updated webhook."""
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        order_ref = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: [orders/updated] order #%s", order_ref)

        try:
            sync = self._get_sync_record()
            result = sync._update_sale_order(order_data)
            return self._ok(result)
        except Exception as exc:
            _logger.error("Shopify webhook: update order #%s failed — %s", order_ref, exc)
            return self._error(str(exc), 500)

    # -- dedicated route for orders/cancelled ---------------------------
    @http.route(
        '/shopify/webhook/orders/cancelled',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_cancelled(self, **kwargs):
        """Handle orders/cancelled webhook."""
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        order_ref = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: [orders/cancelled] order #%s", order_ref)

        try:
            sync = self._get_sync_record()
            result = sync._cancel_sale_order(order_data)
            return self._ok(result)
        except Exception as exc:
            _logger.error("Shopify webhook: cancel order #%s failed — %s", order_ref, exc)
            return self._error(str(exc), 500)

    # -- dedicated route for orders/delete ------------------------------
    @http.route(
        '/shopify/webhook/orders/delete',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_delete(self, **kwargs):
        """Handle orders/delete webhook."""
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        order_ref = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: [orders/delete] order #%s", order_ref)

        try:
            sync = self._get_sync_record()
            result = sync._delete_sale_order(order_data)
            return self._ok(result)
        except Exception as exc:
            _logger.error("Shopify webhook: delete order #%s failed — %s", order_ref, exc)
            return self._error(str(exc), 500)

    # -- dedicated route for orders/paid --------------------------------
    @http.route(
        '/shopify/webhook/orders/paid',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_paid(self, **kwargs):
        """Handle orders/paid webhook."""
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        order_ref = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: [orders/paid] order #%s", order_ref)

        try:
            sync = self._get_sync_record()
            result = sync._mark_order_paid(order_data)
            return self._ok(result)
        except Exception as exc:
            _logger.error("Shopify webhook: paid order #%s failed — %s", order_ref, exc)
            return self._error(str(exc), 500)

    # -- dedicated route for orders/fulfilled ---------------------------
    @http.route(
        '/shopify/webhook/orders/fulfilled',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order_fulfilled(self, **kwargs):
        """Handle orders/fulfilled webhook."""
        order_data = self._parse_json()
        if order_data is None:
            return self._error('Invalid JSON', 400)

        order_ref = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: [orders/fulfilled] order #%s", order_ref)

        try:
            sync = self._get_sync_record()
            result = sync._note_fulfillment(order_data)
            return self._ok(result)
        except Exception as exc:
            _logger.error("Shopify webhook: fulfilled order #%s failed — %s", order_ref, exc)
            return self._error(str(exc), 500)
