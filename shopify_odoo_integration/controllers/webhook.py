# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ShopifyWebhookController(http.Controller):
    """Receive Shopify webhook events and dispatch to sync logic."""

    @http.route(
        '/shopify/webhook/orders',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def receive_order(self, **kwargs):
        """Handle orders/create webhook from Shopify."""
        try:
            order_data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError) as e:
            _logger.error("Shopify webhook: invalid JSON — %s", e)
            return request.make_response(
                json.dumps({'status': 'error', 'message': 'Invalid JSON'}),
                headers={'Content-Type': 'application/json'},
                status=400,
            )

        order_number = order_data.get('order_number', '?')
        _logger.info("Shopify webhook: received order #%s", order_number)

        try:
            Sync = request.env['shopify.sync'].sudo()
            sync_record = Sync.search([], limit=1)
            if not sync_record:
                sync_record = Sync.create({'name': 'Shopify Sync'})
            sync_record._create_or_update_sale_order(order_data)
            _logger.info("Shopify webhook: order #%s processed OK", order_number)
        except Exception as e:
            _logger.error(
                "Shopify webhook: failed order #%s — %s", order_number, e
            )
            return request.make_response(
                json.dumps({'status': 'error', 'message': str(e)}),
                headers={'Content-Type': 'application/json'},
                status=500,
            )

        return request.make_response(
            json.dumps({'status': 'ok'}),
            headers={'Content-Type': 'application/json'},
        )
