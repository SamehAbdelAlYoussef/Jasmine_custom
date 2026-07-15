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
    shopify_api_version = fields.Char(
        string='Shopify API Version',
        config_parameter='shopify.api_version',
        default='2024-10',
        help="Shopify REST Admin API version (e.g. 2024-10, 2025-01). "
             "Default: 2024-10",
    )

    # -- Inventory sync settings ----------------------------------------
    shopify_location_id = fields.Char(
        string='Shopify Location ID',
        config_parameter='shopify.location_id',
        help="The Shopify fulfilment location ID where inventory levels "
             "are synced.  Leave empty to use per-product location.  "
             "Find it in Shopify Admin → Settings → Locations, or "
             "via the API at /admin/api/{version}/locations.json.",
    )
    shopify_stock_sync_enabled = fields.Boolean(
        string='Enable Stock Sync',
        config_parameter='shopify.stock_sync_enabled',
        default=False,
        help="When enabled, Odoo stock-quantity changes are automatically "
             "pushed to Shopify via the inventory_levels/set.json API.",
    )
    shopify_default_requires_shipping = fields.Boolean(
        string='Default: Requires Shipping',
        config_parameter='shopify.default_requires_shipping',
        default=True,
        help="Default value for the 'Requires Shipping' field on new "
             "product bindings.  Synced to Shopify InventoryItem."
             "requires_shipping.",
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

        api_version = self.shopify_api_version or ICP.get_param('shopify.api_version') or '2024-10'
        base = f"https://{shop}/admin/api/{api_version}"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        msgs = []

        # 1. REST shop.json
        try:
            r = requests.get(f"{base}/shop.json", headers=headers, timeout=15)
            r.raise_for_status()
            msgs.append(f"✓ REST: {r.json().get('shop', {}).get('name', shop)}")
        except requests.exceptions.RequestException as e:
            msgs.append(f"✗ REST: {e}")

        # 2. Locations
        try:
            r = requests.get(f"{base}/locations.json", headers=headers, timeout=15)
            r.raise_for_status()
            locs = r.json().get('locations', [])
            active = [l for l in locs if l.get('active')]
            msgs.append(f"✓ Locations: {len(active)} active")
        except requests.exceptions.RequestException as e:
            msgs.append(f"✗ Locations: {e}")

        # 3. Inventory read + connect test
        if active:
            loc_id = active[0]['id']
            msgs.append(f"Using location_id={loc_id}")

            try:
                rv = requests.get(
                    f"{base}/variants.json?limit=3&fields=id,inventory_item_id,sku",
                    headers=headers, timeout=15,
                )
                rv.raise_for_status()
                variants = rv.json().get('variants', [])
                # Pick first variant with a real SKU
                test_var = None
                for v in variants:
                    if v.get('sku') and v.get('sku') != '0':
                        test_var = v
                        break
                if not test_var:
                    test_var = variants[0] if variants else None

                if test_var:
                    inv_id = test_var.get('inventory_item_id')
                    variant_id = test_var.get('id')
                    sku = test_var.get('sku', '?')

                    msgs.append(f"Test: SKU={sku}, inv_item_id={inv_id}, variant_id={variant_id}")

                    ri = requests.get(
                        f"{base}/inventory_levels.json"
                        f"?inventory_item_ids={inv_id}"
                        f"&location_ids={loc_id}",
                        headers=headers, timeout=15,
                    )
                    if ri.status_code == 200:
                        lvls = ri.json().get('inventory_levels', [])
                        if lvls:
                            cur_qty = lvls[0].get('available', 0)
                            msgs.append(f"✓ Connected qty={cur_qty}")
                            rv2 = requests.get(
                                f"{base}/variants/{variant_id}.json",
                                headers=headers, timeout=15,
                            )
                            if rv2.status_code == 200:
                                vd = rv2.json().get('variant', {})
                                msgs.append(f"inv_mgmt={vd.get('inventory_management')}")
                            msgs.append("──────────────────────────────")
                            msgs.append("Run this curl in terminal:")
                            msgs.append(f"curl -X POST \"{base}/inventory_levels/set.json\" \\")
                            msgs.append(f"  -H \"X-Shopify-Access-Token: {token[:15]}...\" \\")
                            msgs.append(f"  -H \"Content-Type: application/json\" \\")
                            msgs.append(f"  -d '{{\"location_id\":{loc_id},\"inventory_item_id\":{inv_id},\"available\":{cur_qty + 1}}}'")
                            msgs.append("Replace '...' with your FULL token")
                            msgs.append("──────────────────────────────")
                        else:
                            msgs.append("✗ NOT connected to this location!")
                    else:
                        msgs.append(f"✗ Inventory GET: HTTP {ri.status_code}")
            except Exception as ex:
                msgs.append(f"Inventory test error: {ex}")

        result = "\n".join(msgs)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Shopify Connection Test',
                'message': result,
                'type': 'success',
                'sticky': True,
            }
        }

    def action_fetch_shopify_locations(self):
        """Fetch locations from Shopify API and show them in a notification."""
        ICP = self.env['ir.config_parameter'].sudo()
        shop = self.shopify_shop_url or ICP.get_param('shopify.shop_url')
        token = self.shopify_access_token or ICP.get_param('shopify.access_token')

        if not shop or not token:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': 'Configure Shop URL and Access Token first.',
                    'type': 'danger',
                    'sticky': True,
                }
            }

        api_version = self.shopify_api_version or ICP.get_param('shopify.api_version') or '2024-10'
        url = f"https://{shop}/admin/api/{api_version}/locations.json"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            locations = response.json().get('locations', [])
            active = [l for l in locations if l.get('active')]

            if len(active) == 1:
                loc = active[0]
                loc_id = str(loc.get('id', ''))
                ICP.set_param('shopify.location_id', loc_id)
                self.shopify_location_id = loc_id
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Location Auto-Filled',
                        'message': (
                            f"Location '{loc.get('name')}' (ID: {loc_id}) "
                            "has been set automatically. Click Save to confirm."
                        ),
                        'type': 'success',
                        'sticky': False,
                    }
                }

            lines = []
            for loc in active:
                lines.append(
                    f"• {loc.get('name', 'Unknown')} — ID: {loc.get('id')}"
                )
            result = "\n".join(lines) if lines else "No active locations found."

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': f'Shopify Locations ({len(active)} active)',
                    'message': result,
                    'type': 'success',
                    'sticky': True,
                }
            }
        except requests.exceptions.RequestException as e:
            _logger.error("Shopify fetch locations failed: %s", e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Fetch Failed',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def action_full_inventory_sync(self):
        """One-click full sync: fetch ALL Shopify variants, match with Odoo
        by SKU, create bindings, then enqueue all bound products for
        background inventory sync via the existing cron job.

        Returns immediately — the cron processes the queue every 5 minutes.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        shop = self.shopify_shop_url or ICP.get_param('shopify.shop_url')
        token = self.shopify_access_token or ICP.get_param('shopify.access_token')
        location_id = self.shopify_location_id or ICP.get_param('shopify.location_id')

        if not shop or not token:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Failed',
                    'message': 'Configure Shop URL and Access Token first.',
                    'type': 'danger',
                    'sticky': True,
                }
            }
        if not location_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Failed',
                    'message': 'Set Shopify Location ID first.',
                    'type': 'danger',
                    'sticky': True,
                }
            }

        # Fetch Shopify domain from config
        myshopify = ICP.get_param('shopify.myshopify_domain', shop)

        api_version = self.shopify_api_version or ICP.get_param('shopify.api_version') or '2024-10'
        base_url = f"https://{myshopify}/admin/api/{api_version}"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        # Step 1: Fetch ALL Shopify variants & create bindings
        page_url = f"{base_url}/variants.json?limit=250&fields=id,sku,inventory_item_id,product_id"
        matched = 0
        errors = 0

        Binding = self.env['shopify.product.binding'].sudo()
        OdooProduct = self.env['product.product'].sudo()

        while page_url:
            try:
                resp = requests.get(page_url, headers=headers, timeout=30)
                limit_hdr = resp.headers.get('X-Shopify-Shop-Api-Call-Limit', '')
                if limit_hdr and '/' in limit_hdr:
                    used_str, limit_str = limit_hdr.split('/')
                    used, limit = (int(used_str), int(limit_str))
                    if limit > 0 and used / limit >= 0.85:
                        import time
                        time.sleep(1.0)
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.RequestException as e:
                _logger.error("Catalog sync: API error: %s", e)
                errors += 1
                break

            variants = data.get('variants', [])
            for v in variants:
                sku = v.get('sku')
                if not sku:
                    continue

                odoo_product = OdooProduct.search(
                    [('default_code', '=', sku)], limit=1,
                )
                if not odoo_product:
                    continue

                matched += 1
                binding = Binding.search(
                    [('product_id', '=', odoo_product.id)], limit=1,
                )
                binding_vals = {
                    'product_id': odoo_product.id,
                    'shopify_product_id': str(v.get('product_id', '')),
                    'shopify_variant_id': str(v.get('id', '')),
                    'shopify_inventory_item_id': str(v.get('inventory_item_id', '')),
                    'shopify_location_id': location_id,
                    'sync_inventory': True,
                }
                if binding:
                    binding.write(binding_vals)
                else:
                    Binding.create(binding_vals)

            # Next page
            page_url = None
            link_header = resp.headers.get('Link', '')
            for part in link_header.split(','):
                if 'rel="next"' in part:
                    page_url = part.split(';')[0].strip(' <>')
                    break

        _logger.info("Catalog sync: %d bindings created/updated, %d errors", matched, errors)

        # Step 2: Enqueue all bound products for background stock sync
        if matched > 0:
            bound_products = Binding.search([('sync_inventory', '=', True)]).mapped('product_id').ids
            if bound_products:
                self.env['shopify.stock.queue'].sudo()._enqueue_products(bound_products)
                _logger.info("Enqueued %d products for background stock sync", len(bound_products))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Catalog Sync Complete',
                'message': (
                    f"Bindings created: {matched} products\n"
                    f"Stock sync queued: {matched} products\n"
                    f"Errors: {errors}\n\n"
                    "Inventory will sync via the background cron job "
                    "(every 5 minutes). Activate 'Shopify: Sync Stock Queue' "
                    "cron if not already active."
                ),
                'type': 'success' if errors == 0 else 'warning',
                'sticky': True,
            }
        }

    def action_sync_orders_from_settings(self):
        """Save settings, mark sync as pending, return immediately."""
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
