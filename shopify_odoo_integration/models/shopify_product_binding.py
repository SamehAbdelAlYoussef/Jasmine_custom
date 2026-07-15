# -*- coding: utf-8 -*-
"""Shopify product binding and inventory sync models.

- ``shopify.product.binding`` — links ``product.product`` to a Shopify
  variant / inventory-item, storing the IDs needed for inventory and
  shipping API calls.
- ``shopify.stock.queue`` — lightweight queue table; a ``stock.quant``
  write hook enqueues pending updates, a cron processes them in batches
  with rate-limit awareness and **auto-discovers** Shopify products by
  SKU when no binding exists yet.
"""

import logging

import requests

from odoo import fields, models

_logger = logging.getLogger(__name__)

# ── tunables ────────────────────────────────────────────────────────────
CATALOG_PAGE_SIZE = 250       # variants per page when fetching from Shopify
STOCK_QUEUE_BATCH = 200       # max queue records per cron invocation
# ─────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────
# 1. Product ↔ Shopify binding
# ──────────────────────────────────────────────────────────────────────────

class ShopifyProductBinding(models.Model):
    _name = 'shopify.product.binding'
    _description = 'Shopify Product Binding'
    _rec_name = 'product_id'

    product_id = fields.Many2one(
        'product.product', string='Odoo Product',
        required=True, index=True, ondelete='cascade',
        help="The Odoo product linked to a Shopify variant.",
    )
    shopify_product_id = fields.Char(
        string='Shopify Product ID',
        help="Shopify Product numeric or GID.",
    )
    shopify_variant_id = fields.Char(
        string='Shopify Variant ID',
        help="Shopify Product Variant numeric / GID.",
    )
    shopify_inventory_item_id = fields.Char(
        string='Shopify Inventory Item ID',
        help="Shopify InventoryItem ID.  Required for inventory-level "
             "and requires_shipping API calls.",
    )
    shopify_location_id = fields.Char(
        string='Shopify Location ID',
        help="Fulfilment location for this product.  Falls back to "
             "the global config parameter 'shopify.location_id'.",
    )
    requires_shipping = fields.Boolean(
        string='Requires Shipping', default=True,
        help="Synced to Shopify InventoryItem.requires_shipping.",
    )
    sync_inventory = fields.Boolean(
        string='Sync Inventory', default=True,
        help="When enabled, stock changes for this product are "
             "automatically pushed to Shopify.",
    )
    last_synced_qty = fields.Float(
        string='Last Synced Quantity', copy=False,
    )
    last_sync_date = fields.Datetime(
        string='Last Sync Date', copy=False,
    )

    _sql_constraints = [
        (
            'unique_binding_product',
            'UNIQUE(product_id)',
            'Each Odoo product can have only one Shopify binding.',
        ),
    ]

    # ------------------------------------------------------------------
    # Shopify SKU lookup — auto-discover bindings
    # ------------------------------------------------------------------

    def _get_sync_helper(self):
        """Return (or create) a ``shopify.sync`` record to reuse its
        API helpers (auth, base URL, rate limiting)."""
        Sync = self.env['shopify.sync'].sudo()
        helper = Sync.search([], limit=1)
        if not helper:
            helper = Sync.create({'name': 'API Helper'})
        return helper

    def _discover_binding_for_product(self, product):
        """Search Shopify for a variant matching *product*.

        Tries in order:

        1. **SKU match** — pages through all Shopify variants searching
           for an exact ``sku`` match against ``product.default_code``.
        2. **Name match** (fallback) — if SKU doesn't match, searches
           Shopify products by ``product.name`` via the product-title
           filter, then picks the first variant of the best match.

        :param product: ``product.product`` record
        :return: ``shopify.product.binding`` or empty recordset
        """
        sku = product.default_code
        product_name = (product.name or '').strip()

        if not sku and not product_name:
            _logger.debug(
                "shopify discover: product #%s has no SKU and no name, skipping",
                product.id,
            )
            return self.env['shopify.product.binding']

        # Already bound? Return cached binding immediately.
        existing = self.search([('product_id', '=', product.id)], limit=1)
        if existing:
            return existing

        sync = self._get_sync_helper()
        base_url = sync._get_api_base()
        headers = sync._get_shopify_headers()

        # ── Step 1: search by SKU (exact match on variant.sku) ─────────
        if sku:
            _logger.info(
                "shopify discover: searching Shopify for SKU '%s'", sku,
            )
            binding = self._search_shopify_by_sku(
                sku, product, sync, base_url, headers,
            )
            if binding:
                return binding
            _logger.info(
                "shopify discover: SKU '%s' not found, trying name search...", sku,
            )

        # ── Step 2: search by product name (partial match) ─────────────
        if product_name:
            _logger.info(
                "shopify discover: searching Shopify products for '%s'",
                product_name,
            )
            binding = self._search_shopify_by_name(
                product_name, sku, product, sync, base_url, headers,
            )
            if binding:
                return binding

        _logger.debug(
            "shopify discover: product '%s' (SKU=%s) not found in Shopify",
            product_name or '?', sku or '?',
        )
        return self.env['shopify.product.binding']

    # ------------------------------------------------------------------
    # Search helpers
    # ------------------------------------------------------------------

    def _search_shopify_by_sku(self, sku, product, sync, base_url, headers):
        """Page through Shopify variants looking for an exact SKU match.

        :returns: ``shopify.product.binding`` or ``None``
        """
        page_url = (
            f"{base_url}/variants.json"
            f"?limit={CATALOG_PAGE_SIZE}"
            "&fields=id,sku,inventory_item_id,product_id,title"
        )
        pages = 0

        while page_url:
            pages += 1
            try:
                response = requests.get(
                    page_url, headers=headers, timeout=30,
                )
                sync._check_rate_limit(response.headers)
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.RequestException as exc:
                _logger.error(
                    "shopify discover: API error on page %d for "
                    "SKU '%s': %s", pages, sku, exc,
                )
                return None

            variants = data.get('variants', [])
            for v in variants:
                if v.get('sku') == sku:
                    _logger.info(
                        "shopify discover: found SKU '%s' (page %d) → "
                        "variant_id=%s, inventory_item_id=%s",
                        sku, pages, v.get('id'), v.get('inventory_item_id'),
                    )
                    return self.create({
                        'product_id': product.id,
                        'shopify_product_id': str(v.get('product_id', '')),
                        'shopify_variant_id': str(v.get('id', '')),
                        'shopify_inventory_item_id': str(
                            v.get('inventory_item_id', ''),
                        ),
                    })

            # follow Link header for next page
            page_url = None
            link_header = response.headers.get('Link', '')
            for link_part in link_header.split(','):
                if 'rel="next"' in link_part:
                    page_url = link_part.split(';')[0].strip(' <>')
                    break

        return None

    def _search_shopify_by_name(self, product_name, sku, product, sync,
                                base_url, headers):
        """Search Shopify products by title (partial match).

        Shopify's ``/products.json?title=`` filter performs a
        case-insensitive substring match.  We pick the first product
        whose title contains the Odoo product name (or vice-versa),
        then attach its first variant.

        If *sku* is provided, we prefer the variant whose SKU matches
        *sku* within the found product.

        :returns: ``shopify.product.binding`` or ``None``
        """
        import urllib.parse

        search_url = (
            f"{base_url}/products.json"
            f"?title={urllib.parse.quote(product_name)}"
            "&limit=10"
            "&fields=id,title,variants"
        )

        try:
            response = requests.get(search_url, headers=headers, timeout=30)
            sync._check_rate_limit(response.headers)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as exc:
            _logger.error(
                "shopify discover: product name search failed — %s", exc,
            )
            return None

        products = data.get('products', [])
        if not products:
            _logger.debug(
                "shopify discover: no Shopify product matches name '%s'",
                product_name,
            )
            return None

        # Score matches: prefer closer title match
        odoo_name_lower = product_name.lower()
        best_product = None
        best_score = 99

        for p in products:
            shopify_title = (p.get('title') or '').lower()
            # Simple scoring: shorter edit distance = better match
            if shopify_title == odoo_name_lower:
                score = 0  # exact match
            elif odoo_name_lower in shopify_title:
                score = 1  # Odoo name is substring of Shopify title
            elif shopify_title in odoo_name_lower:
                score = 2  # Shopify title is substring of Odoo name
            else:
                # Fuzzy — count matching words
                odoo_words = set(odoo_name_lower.split())
                shopify_words = set(shopify_title.split())
                common = odoo_words & shopify_words
                score = 10 - min(len(common), 9)

            if score < best_score:
                best_score = score
                best_product = p
                if score == 0:
                    break

        if not best_product:
            return None

        variants = best_product.get('variants', [])
        if not variants:
            _logger.debug(
                "shopify discover: product '%s' has no variants",
                best_product.get('title'),
            )
            return None

        # Prefer variant with matching SKU, else take the first one
        chosen = variants[0]
        if sku:
            for v in variants:
                if v.get('sku') == sku:
                    chosen = v
                    break

        _logger.info(
            "shopify discover: name match → product='%s' (id=%s), "
            "variant_id=%s, inventory_item_id=%s",
            best_product.get('title'), best_product.get('id'),
            chosen.get('id'), chosen.get('inventory_item_id'),
        )
        return self.create({
            'product_id': product.id,
            'shopify_product_id': str(best_product.get('id', '')),
            'shopify_variant_id': str(chosen.get('id', '')),
            'shopify_inventory_item_id': str(
                chosen.get('inventory_item_id', ''),
            ),
        })

    # ------------------------------------------------------------------
    # Bulk catalog sync — pull ALL variants from Shopify
    # ------------------------------------------------------------------

    def _sync_all_bindings_from_shopify(self):
        """Fetch **all** product variants from Shopify and create /
        update bindings for any Odoo product whose ``default_code``
        matches a Shopify SKU.

        This is meant to be called manually (via a button or menu item)
        or once per day via cron.  It pages through *every* variant in
        Shopify, so it can be slow for large catalogues — use it
        sparingly.
        """
        sync = self._get_sync_helper()
        ICP = self.env['ir.config_parameter'].sudo()
        default_location = ICP.get_param('shopify.location_id')
        base_url = sync._get_api_base()
        headers = sync._get_shopify_headers()

        page_url = (
            f"{base_url}/variants.json"
            f"?limit={CATALOG_PAGE_SIZE}"
            "&fields=id,sku,inventory_item_id,product_id"
        )
        page_count = 0
        created = 0
        updated = 0

        _logger.info("shopify catalog sync: starting full variant import")

        while page_url:
            page_count += 1
            try:
                response = requests.get(
                    page_url, headers=headers, timeout=30,
                )
                sync._check_rate_limit(response.headers)
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.RequestException as exc:
                _logger.error(
                    "shopify catalog sync: API error on page %d: %s",
                    page_count, exc,
                )
                break

            variants = data.get('variants', [])
            for v in variants:
                sku = v.get('sku')
                if not sku:
                    continue

                odoo_product = self.env['product.product'].search(
                    [('default_code', '=', sku)], limit=1,
                )
                if not odoo_product:
                    continue

                binding = self.search(
                    [('product_id', '=', odoo_product.id)], limit=1,
                )
                vals = {
                    'shopify_product_id': str(v.get('product_id', '')),
                    'shopify_variant_id': str(v.get('id', '')),
                    'shopify_inventory_item_id': str(
                        v.get('inventory_item_id', ''),
                    ),
                }
                if not binding.shopify_location_id and default_location:
                    vals['shopify_location_id'] = default_location

                if binding:
                    binding.write(vals)
                    updated += 1
                else:
                    vals['product_id'] = odoo_product.id
                    self.create(vals)
                    created += 1

            _logger.info(
                "shopify catalog sync: page %d — %d created, %d updated so far",
                page_count, created, updated,
            )

            # Follow Link header for next page
            page_url = None
            link_header = response.headers.get('Link', '')
            for link_part in link_header.split(','):
                if 'rel="next"' in link_part:
                    page_url = link_part.split(';')[0].strip(' <>')
                    break

        _logger.info(
            "shopify catalog sync: done — %d created, %d updated in %d pages",
            created, updated, page_count,
        )
        return {'created': created, 'updated': updated}


# ──────────────────────────────────────────────────────────────────────────
# 2. Stock-change queue (auto-discover on first sync)
# ──────────────────────────────────────────────────────────────────────────

class ShopifyStockQueue(models.Model):
    _name = 'shopify.stock.queue'
    _description = 'Shopify Stock Sync Queue'
    _order = 'create_date'

    product_id = fields.Many2one(
        'product.product', string='Product',
        required=True, index=True, ondelete='cascade',
        help="The Odoo product whose stock changed.  Used to look up "
             "or auto-discover a Shopify binding.",
    )
    binding_id = fields.Many2one(
        'shopify.product.binding', string='Binding',
        index=True, ondelete='set null',
        help="Cached binding.  May be empty for products that have "
             "never been synced — the cron will auto-discover it.",
    )
    new_quantity = fields.Float(
        string='New Quantity', required=True,
        help="Absolute available quantity to set in Shopify.",
    )
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        string='State', default='pending', required=True, copy=False,
        index=True,
    )
    error_message = fields.Text(string='Error Message', copy=False)
    create_date = fields.Datetime(string='Created', default=fields.Datetime.now)

    # ------------------------------------------------------------------
    # Enqueue — called from stock.quant.write()
    # ------------------------------------------------------------------

    def _enqueue_products(self, product_ids):
        """Create or update a *pending* queue record for every product
        in *product_ids* that has a ``default_code`` (SKU).

        Products without a SKU are silently skipped — we can't match
        them to Shopify without one.

        Deduplication: when a record in ``pending`` or ``processing``
        state already exists for the product, its ``new_quantity`` is
        updated in-place.  This also covers the race condition where
        the cron has picked up a record (state → ``processing``) while
        a new stock move fires.

        We invalidate ``qty_available`` before reading it to ensure the
        computed field reflects the latest quant writes, even inside
        long-running batch transactions (e.g. PO receipt).
        """
        products = self.env['product.product'].sudo().search([
            ('id', 'in', product_ids),
            ('default_code', '!=', False),
        ])
        if not products:
            return

        Binding = self.env['shopify.product.binding'].sudo()
        for product in products:
            # Force fresh computation — bypass ORM cache
            product.invalidate_recordset(['qty_available'])
            qty = product.sudo().qty_available

            existing = self.search([
                ('product_id', '=', product.id),
                ('state', 'in', ('pending', 'processing')),
            ], limit=1)
            if existing:
                existing.write({
                    'new_quantity': qty,
                    'state': 'pending',  # reset if stuck in processing
                    'error_message': False,
                })
                _logger.debug(
                    "shopify stock queue: updated pending for %s → %s",
                    product.default_code, qty,
                )
            else:
                # Try to attach an existing binding (speed up the cron)
                binding = Binding.search(
                    [('product_id', '=', product.id)], limit=1,
                )
                self.create({
                    'product_id': product.id,
                    'binding_id': binding.id if binding else False,
                    'new_quantity': qty,
                })
                _logger.debug(
                    "shopify stock queue: enqueued %s → %s (binding=%s)",
                    product.default_code, qty,
                    'yes' if binding else 'no',
                )

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------

    def _cron_process_stock_queue(self):
        """Process pending stock-sync queue records.

        For each record:

        1. If no binding exists, **auto-discover** the product in
           Shopify by SKU and create the binding on the fly.
        2. Push the absolute quantity to Shopify via
           ``inventory_levels/set.json``.
        3. Mark the record *done* or *error*.

        Products whose SKU is not found in Shopify are marked *error*
        with a descriptive message — we never create products in Shopify.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        global_enabled = ICP.get_param('shopify.stock_sync_enabled', 'False')
        if global_enabled != 'True':
            _logger.debug("shopify stock sync: disabled in settings")
            return

        pending = self.search(
            [('state', '=', 'pending')],
            limit=STOCK_QUEUE_BATCH,
        )
        if not pending:
            return

        _logger.info(
            "shopify stock sync: processing %d queue records", len(pending),
        )
        pending.write({'state': 'processing'})

        sync = self.env['shopify.product.binding']._get_sync_helper()
        default_location_id = ICP.get_param('shopify.location_id')
        Binding = self.env['shopify.product.binding'].sudo()

        for rec in pending:
            binding = rec.binding_id

            # -- auto-discover binding if missing ----------------------------
            if not binding:
                binding = Binding._discover_binding_for_product(rec.product_id)
                if binding:
                    rec.binding_id = binding.id
                elif not rec.product_id.default_code:
                    rec.write({
                        'state': 'error',
                        'error_message': 'Product has no SKU — cannot match to Shopify.',
                    })
                    continue
                else:
                    rec.write({
                        'state': 'error',
                        'error_message': (
                            f'SKU "{rec.product_id.default_code}" not found '
                            'in Shopify. Ensure the product exists in Shopify '
                            'with the same SKU.'
                        ),
                    })
                    continue

            # -- validate required IDs --------------------------------------
            location_id = binding.shopify_location_id or default_location_id
            inventory_item_id = binding.shopify_inventory_item_id

            if not location_id:
                rec.write({
                    'state': 'error',
                    'error_message': (
                        'Missing Shopify Location ID. Set it in '
                        'Settings → Shopify → Inventory Sync, or on '
                        'the product binding.'
                    ),
                })
                continue

            if not inventory_item_id:
                rec.write({
                    'state': 'error',
                    'error_message': 'Missing Shopify Inventory Item ID.',
                })
                continue

            # -- push to Shopify --------------------------------------------
            try:
                sync._set_shopify_inventory(
                    inventory_item_id, location_id, int(rec.new_quantity),
                )
                binding.write({
                    'last_synced_qty': rec.new_quantity,
                    'last_sync_date': fields.Datetime.now(),
                })
                rec.write({'state': 'done'})
                _logger.info(
                    "shopify stock sync: %s (%s) → %s",
                    rec.product_id.default_code,
                    inventory_item_id,
                    int(rec.new_quantity),
                )
            except Exception as exc:
                _logger.error(
                    "shopify stock sync failed for %s: %s",
                    rec.product_id.default_code, exc,
                )
                rec.write({
                    'state': 'error',
                    'error_message': str(exc),
                })

        # Delete done records (keep errors for review)
        done_records = pending.filtered(lambda r: r.state == 'done')
        if done_records:
            done_records.unlink()
        _logger.info("shopify stock sync: batch complete")


# ──────────────────────────────────────────────────────────────────────────
# 3. product.product extension
# ──────────────────────────────────────────────────────────────────────────

class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_binding_ids = fields.One2many(
        'shopify.product.binding', 'product_id',
        string='Shopify Bindings',
    )

    def _get_shopify_binding(self):
        """Return the first Shopify binding for this product (if any)."""
        self.ensure_one()
        return self.shopify_binding_ids[:1]


# ──────────────────────────────────────────────────────────────────────────
# 4. stock.quant hook — enqueue on quantity change
# ──────────────────────────────────────────────────────────────────────────

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def write(self, vals):
        """After writing to quants, enqueue affected products whose
        ``quantity`` field changed.

        We intentionally do NOT trigger on ``reserved_quantity`` changes
        because that does not affect ``qty_available`` — it would send
        the same value to Shopify, producing no visible change.
        """
        res = super().write(vals)
        if 'quantity' in vals:
            product_ids = list(set(self.mapped('product_id').ids))
            if product_ids:
                self.env['shopify.stock.queue'].sudo()._enqueue_products(
                    product_ids,
                )
        return res
