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
        """Test Shopify API connection — REST + GraphQL."""
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
            msgs.append(f"✓ REST API: {r.json().get('shop', {}).get('name', shop)}")
        except requests.exceptions.RequestException as e:
            msgs.append(f"✗ REST API: {e}")

        # 2. GraphQL simple query
        try:
            r = requests.post(
                f"{base}/graphql.json",
                headers=headers,
                json={'query': '{ shop { name } }'},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if 'errors' in data:
                msgs.append(f"✗ GraphQL: {data['errors'][0]['message']}")
            else:
                name = data.get('data', {}).get('shop', {}).get('name', '?')
                msgs.append(f"✓ GraphQL: {name}")
        except requests.exceptions.RequestException as e:
            msgs.append(f"✗ GraphQL: {e}")

        # 3. Check inventory scope via REST locations
        try:
            r = requests.get(f"{base}/locations.json", headers=headers, timeout=15)
            r.raise_for_status()
            locs = r.json().get('locations', [])
            active = [l for l in locs if l.get('active')]
            msgs.append(f"✓ Locations: {len(active)} active")

            # 4. Test inventory REST endpoints (if we have a location)
            if active:
                loc_id = active[0]['id']
                # Get first variant to test with
                rv = requests.get(
                    f"{base}/variants.json?limit=1&fields=id,inventory_item_id",
                    headers=headers, timeout=15,
                )
                rv.raise_for_status()
                variants = rv.json().get('variants', [])
                if variants:
                    inv_item_id = variants[0].get('inventory_item_id')
                    # Try inventory_levels.json GET
                    ri = requests.get(
                        f"{base}/inventory_levels.json"
                        f"?inventory_item_ids={inv_item_id}"
                        f"&location_ids={loc_id}",
                        headers=headers, timeout=15,
                    )
                    if ri.status_code == 200:
                        lvls = ri.json().get('inventory_levels', [])
                        cur_qty = lvls[0].get('available', 0) if lvls else 0
                        msgs.append(f"✓ Inventory read: qty={cur_qty}")

                        # Test set.json
                        try:
                            rs = requests.post(
                                f"{base}/inventory_levels/set.json",
                                headers=headers,
                                json={
                                    'inventory_item_id': inv_item_id,
                                    'location_id': loc_id,
                                    'available': cur_qty,
                                },
                                timeout=15,
                            )
                            if rs.status_code == 200:
                                msgs.append("✓ Inventory SET works")
                            else:
                                msgs.append(f"✗ Inventory SET: HTTP {rs.status_code}")
                        except Exception as ex:
                            msgs.append(f"✗ Inventory SET: {ex}")

                        # Test adjust.json
                        try:
                            ra = requests.post(
                                f"{base}/inventory_levels/adjust.json",
                                headers=headers,
                                json={
                                    'inventory_item_id': inv_item_id,
                                    'location_id': loc_id,
                                    'available_adjustment': 0,
                                },
                                timeout=15,
                            )
                            if ra.status_code == 200:
                                msgs.append("✓ Inventory ADJUST works")
                            else:
                                msgs.append(f"✗ Inventory ADJUST: HTTP {ra.status_code}")
                        except Exception as ex:
                            msgs.append(f"✗ Inventory ADJUST: {ex}")

                        # Test POST to base /inventory_levels.json (no action)
                        try:
                            rb = requests.post(
                                f"{base}/inventory_levels.json",
                                headers=headers,
                                json={
                                    'inventory_item_id': inv_item_id,
                                    'location_id': loc_id,
                                    'available': cur_qty,
                                },
                                timeout=15,
                            )
                            if rb.status_code == 200:
                                msgs.append("✓ Inventory POST base works")
                            else:
                                msgs.append(f"✗ Inventory POST base: HTTP {rb.status_code}")
                        except Exception as ex:
                            msgs.append(f"✗ Inventory POST base: {ex}")

                        # Test variant PUT (only inventory_management, no quantity)
                        try:
                            variant_id = variants[0].get('id')
                            rv2 = requests.put(
                                f"{base}/variants/{variant_id}.json",
                                headers=headers,
                                json={
                                    'variant': {
                                        'id': variant_id,
                                        'inventory_management': 'shopify',
                                    },
                                },
                                timeout=15,
                            )
                            if rv2.status_code == 200:
                                msgs.append("✓ Variant PUT (no qty): OK")
                            else:
                                body = rv2.text[:100]
                                msgs.append(f"✗ Variant PUT: HTTP {rv2.status_code}")
                        except Exception as ex:
                            msgs.append(f"✗ Variant PUT: {ex}")

                        # Test if API version 2025-01 has set.json
                        try:
                            base2 = f"https://{shop}/admin/api/2025-01"
                            rs2 = requests.post(
                                f"{base2}/inventory_levels/set.json",
                                headers=headers,
                                json={
                                    'inventory_item_id': inv_item_id,
                                    'location_id': loc_id,
                                    'available': cur_qty,
                                },
                                timeout=15,
                            )
                            if rs2.status_code == 200:
                                msgs.append("✓ SET works on 2025-01")
                            else:
                                msgs.append(f"✗ SET on 2025-01: HTTP {rs2.status_code}")
                        except Exception as ex:
                            msgs.append(f"✗ SET on 2025-01: {ex}")

                        # Test connect.json
                        try:
                            rc = requests.post(
                                f"{base}/inventory_levels/connect.json",
                                headers=headers,
                                json={
                                    'location_id': loc_id,
                                    'inventory_item_id': inv_item_id,
                                },
                                timeout=15,
                            )
                            if rc.status_code in (200, 201):
                                msgs.append("✓ Inventory CONNECT works")
                            else:
                                msgs.append(f"✗ CONNECT: HTTP {rc.status_code}")
                        except Exception as ex:
                            msgs.append(f"✗ CONNECT: {ex}")

                        # Test POST base with wrapped body
                        try:
                            rw = requests.post(
                                f"{base}/inventory_levels.json",
                                headers=headers,
                                json={
                                    'inventory_level': {
                                        'inventory_item_id': inv_item_id,
                                        'location_id': loc_id,
                                        'available': cur_qty,
                                    },
                                },
                                timeout=15,
                            )
                            if rw.status_code == 200:
                                msgs.append("✓ POST base WRAPPED works")
                            else:
                                bod = rw.text[:150]
                                msgs.append(f"✗ POST wrapped: HTTP {rw.status_code} — {bod}")
                        except Exception as ex:
                            msgs.append(f"✗ POST wrapped: {ex}")
                    else:
                        msgs.append(f"✗ Inventory read: HTTP {ri.status_code}")
        except requests.exceptions.RequestException as e:
            msgs.append(f"✗ Locations: {e}")

        result = "\n".join(msgs)
        all_ok = all(m.startswith('✓') for m in msgs)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Connection Test',
                'message': result,
                'type': 'success' if all_ok else 'warning',
                'sticky': True,
            }
        }

    def action_fetch_shopify_locations(self):
        """Fetch locations from Shopify API and show them in a notification.
        The user can copy the correct ID into the Location ID field."""
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
                # Auto-fill when there's only one location
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
        by SKU, create bindings, overwrite Shopify inventory to match Odoo
        stock, and ensure requires_shipping is set on every inventory item.

        This is a blocking operation — it pages through every variant in
        Shopify and makes one API call per matched product.  For stores
        with thousands of products it may take several minutes.
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
                    'message': 'Set Shopify Location ID first. Use Fetch Locations button.',
                    'type': 'danger',
                    'sticky': True,
                }
            }

        api_version = self.shopify_api_version or ICP.get_param('shopify.api_version') or '2024-10'
        base_url = f"https://{shop}/admin/api/{api_version}"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        # -- Step 1: fetch ALL Shopify variants ----------------------------
        page_url = f"{base_url}/variants.json?limit=250&fields=id,sku,inventory_item_id,product_id"
        matched = 0
        synced = 0
        errors = 0

        Binding = self.env['shopify.product.binding'].sudo()
        OdooProduct = self.env['product.product'].sudo()

        while page_url:
            try:
                resp = requests.get(page_url, headers=headers, timeout=30)
                # rate-limit check
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
                _logger.error("Full inventory sync: API error: %s", e)
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
                variant_id = str(v.get('id', ''))
                inventory_item_id = str(v.get('inventory_item_id', ''))

                # Create or update binding
                binding = Binding.search(
                    [('product_id', '=', odoo_product.id)], limit=1,
                )
                binding_vals = {
                    'product_id': odoo_product.id,
                    'shopify_product_id': str(v.get('product_id', '')),
                    'shopify_variant_id': variant_id,
                    'shopify_inventory_item_id': inventory_item_id,
                    'shopify_location_id': location_id,
                    'sync_inventory': True,
                }
                if binding:
                    binding.write(binding_vals)
                else:
                    binding = Binding.create(binding_vals)

                # -- Overwrite Shopify inventory → Odoo qty (GraphQL) -------
                qty = int(odoo_product.qty_available)
                try:
                    Sync = self.env['shopify.sync'].sudo()
                    sync_rec = Sync.search([], limit=1) or Sync.create({'name': 'API Helper'})
                    sync_rec._set_shopify_inventory(
                        inventory_item_id, location_id, qty,
                    )

                    binding.write({
                        'last_synced_qty': qty,
                        'last_sync_date': fields.Datetime.now(),
                    })
                    synced += 1
                except Exception as e:
                    _logger.error(
                        "Full sync: inventory set failed for %s: %s", sku, e,
                    )
                    errors += 1

            # -- next page -------------------------------------------------
            page_url = None
            link_header = resp.headers.get('Link', '')
            for part in link_header.split(','):
                if 'rel="next"' in part:
                    page_url = part.split(';')[0].strip(' <>')
                    break

        _logger.info(
            "Full inventory sync done: %d matched, %d synced, %d errors",
            matched, synced, errors,
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Full Inventory Sync Complete',
                'message': (
                    f"Matched: {matched} products\n"
                    f"Synced to Shopify: {synced}\n"
                    f"Errors: {errors}"
                ),
                'type': 'success' if errors == 0 else 'warning',
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
