# -*- coding: utf-8 -*-
import logging

import requests

from odoo import models, fields

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    shopify_shop_url = fields.Char(
        string='Shopify Shop URL',
        config_parameter='shopify.shop_url',
        help="Your Shopify store domain (e.g. mystore.myshopify.com)",
    )
    shopify_access_token = fields.Char(
        string='Shopify Access Token',
        config_parameter='shopify.access_token',
        help="Shopify Admin API access token",
    )

    def action_test_shopify_connection(self):
        """Test Shopify API connection from settings."""
        ICP = self.env['ir.config_parameter'].sudo()
        shop = self.shopify_shop_url or ICP.get_param('shopify.shop_url')
        token = self.shopify_access_token or ICP.get_param('shopify.access_token')

        if not shop or not token:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': 'Shop URL or Access Token is missing.',
                    'type': 'danger',
                    'sticky': True,
                }
            }

        url = f"https://{shop}/admin/api/2024-01/shop.json"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            shop_data = response.json().get('shop', {})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Successful',
                    'message': f"Connected to: {shop_data.get('name', shop)}",
                    'type': 'success',
                    'sticky': False,
                }
            }
        except requests.exceptions.RequestException as e:
            _logger.error("Shopify test connection failed: %s", e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def action_sync_orders_from_settings(self):
        """Save settings, mark sync as pending, return immediately.

        The cron picks up 'fetching' syncs and processes them in the
        background using batched, independent database transactions.
        No queue_job dependency — works with native Odoo cron only.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('shopify.shop_url', self.shopify_shop_url or '')
        ICP.set_param('shopify.access_token', self.shopify_access_token or '')

        if not self.shopify_shop_url or not self.shopify_access_token:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Skipped',
                    'message': 'Configure both Shop URL and Access Token first.',
                    'type': 'warning',
                    'sticky': True,
                }
            }

        Sync = self.env['shopify.sync']
        sync_record = Sync.search([], limit=1)
        if not sync_record:
            sync_record = Sync.create({'name': 'Shopify Sync'})

        # If already fetching, just report progress
        if sync_record.sync_state == 'fetching':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Already Running',
                    'message': (
                        f"Already fetching orders from Shopify. "
                        f"{sync_record.orders_processed} orders processed so far."
                    ),
                    'type': 'warning',
                    'sticky': True,
                }
            }

        # Mark as fetching — the cron will pick it up within 5 minutes
        sync_record.write({
            'last_sync_id': None,
            'sync_state': 'fetching',
            'orders_processed': 0,
            'orders_total': 0,
            'orders_failed': 0,
            'last_error': False,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Sync Queued',
                'message': (
                    'Shopify sync will start in the background within '
                    '5 minutes. Check the Shopify Sync menu for progress.'
                ),
                'type': 'success',
                'sticky': False,
            }
        }
