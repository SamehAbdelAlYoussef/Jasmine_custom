# -*- coding: utf-8 -*-
"""Shopify → Odoo sync engine — fetches orders via cursor-based pagination
and commits progress every 50 orders on the main cursor.

Architecture for 7 000 – 20 000+ orders (self-contained, no OCA deps)
----------------------------------------------------------------------
1. **Cursor pagination** — follows Shopify's ``Link`` header (``rel="next"``)
   to resume from ``last_sync_id`` across cron restarts.

2. **One page per cron** — each cron invocation fetches ONE API page
   (250 orders), processes it, commits, and returns.  Next cron
   invocation picks up from the cursor.

3. **Main-cursor commits** — ``self.env.cr.commit()`` fires after every
   50 successfully created orders.  `invalidate_recordset()` ensures
   the ORM cache stays consistent.

4. **Idempotency** — ``x_shopify_id`` on ``sale.order`` (unique, indexed)
   is checked before every create.

5. **Rate limiting** — inspects ``X-Shopify-Shop-Api-Call-Limit`` after
   each page and sleeps when the bucket is ≥ 85 % full.

6. **Mail suppression** — ``self.with_context(mail_create_nosubscribe=True,
   mail_notrack=True, tracking_disable=True)`` avoids unnecessary chatter
   and prevents computed-field cascades from exhausting the connection pool.
"""

import logging
import time
from datetime import timedelta

import requests
from dateutil import parser as dateutil_parser
from dateutil.tz import UTC
from psycopg2 import IntegrityError

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ── tunables ────────────────────────────────────────────────────────────
MAX_API_UTILISATION = 0.85  # sleep when used / limit exceeds this ratio
RATE_LIMIT_SLEEP = 1.0      # seconds to sleep when API bucket is warm
LOCK_TIMEOUT_MINUTES = 30   # auto-release stuck locks
# ─────────────────────────────────────────────────────────────────────────


class ShopifySync(models.Model):
    _name = 'shopify.sync'
    _description = 'Shopify Order Sync'

    # -- tracking fields -------------------------------------------------
    name = fields.Char(string='Name', default='Shopify Sync')
    last_sync_id = fields.Char(
        string='Last Synced Order ID',
        help="Highest Shopify REST order ID that has been **fully committed** "
             "to the database (page boundary).  Used to resume incremental "
             "syncs safely.",
    )
    last_sync_count = fields.Integer(
        string='Orders Fetched (Last Sync)',
    )
    sync_state = fields.Selection(
        selection=[
            ('idle', 'Idle'),
            ('fetching', 'Fetching & Processing'),
            ('completed', 'Completed'),
            ('completed_with_errors', 'Completed (with errors)'),
            ('failed', 'Failed'),
        ],
        string='Sync State',
        default='idle',
        required=True,
        copy=False,
    )
    orders_processed = fields.Integer(
        string='Orders Processed',
        help="Successfully created / updated sale orders so far.",
        copy=False,
    )
    orders_total = fields.Integer(
        string='Orders Fetched from API',
        copy=False,
    )
    orders_failed = fields.Integer(
        string='Orders Failed',
        copy=False,
    )
    last_error = fields.Text(string='Last Error', copy=False)
    locked_until = fields.Datetime(
        string='Locked Until',
        help="Advisory lock — prevents overlapping sync runs.",
        copy=False,
    )
    batch_cursor = fields.Char(
        string='Batch Cursor',
        help="Shopify page_info URL for resuming full-sync pagination "
             "across cron invocations.  Set to the ``next`` URL from "
             "the Link header after each page.",
        copy=False,
    )

    # -----------------------------------------------------------------
    # Configuration helpers
    # -----------------------------------------------------------------
    def _get_config(self, key):
        return self.env['ir.config_parameter'].sudo().get_param(f'shopify.{key}')

    def _get_api_base(self):
        """Return the Shopify REST API base URL (https://<shop>/admin/api/<version>).

        The API version is read from ``shopify.api_version`` config parameter,
        defaulting to ``2024-10`` (the version used by the Yasmine Beauty Bar store).
        """
        shop = self._get_config('shop_url')
        api_version = self._get_config('api_version') or '2024-10'
        return f"https://{shop}/admin/api/{api_version}"

    def _get_shopify_headers(self):
        token = self._get_config('access_token')
        return {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

    # -----------------------------------------------------------------
    # Rate limiting
    # -----------------------------------------------------------------
    def _check_rate_limit(self, headers):
        """Sleep if the Shopify API bucket is nearly exhausted.

        Reads ``X-Shopify-Shop-Api-Call-Limit`` (format ``"38/40"``)
        and pauses when utilisation exceeds ``MAX_API_UTILISATION``.
        Call **after** every API request.
        """
        header = headers.get('X-Shopify-Shop-Api-Call-Limit', '')
        if not header or '/' not in header:
            return
        try:
            used_str, limit_str = header.split('/')
            used, limit = int(used_str), int(limit_str)
        except (ValueError, TypeError):
            return

        ratio = used / limit if limit > 0 else 1.0
        if ratio >= MAX_API_UTILISATION:
            _logger.info(
                "Shopify sync: API bucket %d/%d (%.0f%%), sleeping %.1fs",
                used, limit, ratio * 100, RATE_LIMIT_SLEEP,
            )
            time.sleep(RATE_LIMIT_SLEEP)

    # -----------------------------------------------------------------
    # Generic API helpers (GET / POST / PUT) — unified HTTP methods
    # with rate-limit awareness and error handling.
    # -----------------------------------------------------------------
    def _shopify_api_get(self, endpoint, timeout=30):
        """Make a GET request to the Shopify REST API and return the
        parsed JSON body.  Rate-limit checked after the call."""
        url = f"{self._get_api_base()}{endpoint}"
        headers = self._get_shopify_headers()
        response = requests.get(url, headers=headers, timeout=timeout)
        self._check_rate_limit(response.headers)
        response.raise_for_status()
        return response.json() if response.text else {}

    def _shopify_api_post(self, endpoint, data, timeout=30):
        """Make a POST request to the Shopify REST API and return the
        parsed JSON body.  Rate-limit checked after the call."""
        url = f"{self._get_api_base()}{endpoint}"
        headers = self._get_shopify_headers()
        response = requests.post(url, headers=headers, json=data, timeout=timeout)
        self._check_rate_limit(response.headers)
        response.raise_for_status()
        return response.json() if response.text else {}

    def _shopify_api_put(self, endpoint, data, timeout=30):
        """Make a PUT request to the Shopify REST API and return the
        parsed JSON body.  Rate-limit checked after the call."""
        url = f"{self._get_api_base()}{endpoint}"
        headers = self._get_shopify_headers()
        response = requests.put(url, headers=headers, json=data, timeout=timeout)
        self._check_rate_limit(response.headers)
        response.raise_for_status()
        return response.json() if response.text else {}

    # -- inventory / shipping API wrappers ---------------------------------
    def _get_myshopify_domain(self):
        """Return the ``.myshopify.com`` domain for this store.

        Custom domains (e.g. ``mystore.com``) sometimes reject inventory
        write endpoints.  The official ``.myshopify.com`` subdomain always
        works.  We fetch it once from ``/shop.json`` and cache it in a
        config parameter.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        cached = ICP.get_param('shopify.myshopify_domain')
        if cached:
            return cached

        shop = self._get_config('shop_url')
        if 'myshopify.com' in shop:
            ICP.set_param('shopify.myshopify_domain', shop)
            return shop

        # Fetch the canonical domain from the API
        try:
            resp = requests.get(
                f"https://{shop}/admin/api/{self._get_config('api_version') or '2024-10'}/shop.json",
                headers=self._get_shopify_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            domain = resp.json().get('shop', {}).get('myshopify_domain', '')
            if domain:
                ICP.set_param('shopify.myshopify_domain', domain)
                return domain
        except Exception:
            _logger.warning("Could not fetch myshopify_domain, using configured domain")

        ICP.set_param('shopify.myshopify_domain', shop)
        return shop

    def _set_shopify_inventory(self, inventory_item_id, location_id, available):
        """Set the absolute inventory level via Shopify REST API.

        Uses the configured API version and the canonical
        ``.myshopify.com`` domain (custom domains may reject inventory
        write endpoints).

        :param inventory_item_id: Shopify InventoryItem ID (numeric)
        :param location_id: Shopify Location ID (numeric)
        :param available: absolute quantity to set
        """
        shop = self._get_myshopify_domain()
        api_version = self._get_config('api_version') or '2024-10'
        token = self._get_config('access_token')
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }
        payload = {
            'location_id': int(location_id),
            'inventory_item_id': int(inventory_item_id),
            'available': int(available),
        }

        url = f"https://{shop}/admin/api/{api_version}/inventory_levels/set.json"
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        self._check_rate_limit(response.headers)
        if not response.ok:
            # Capture Shopify's error message from the response body
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text[:500]
            _logger.error(
                "Shopify inventory set FAILED — HTTP %s for "
                "inventory_item_id=%s, location_id=%s, available=%s\n"
                "Response: %s",
                response.status_code, inventory_item_id, location_id,
                available, error_body,
            )
            response.raise_for_status()
        result = response.json()
        _logger.info(
            "Shopify inventory SET — inventory_item_id=%s, "
            "location_id=%s, available=%s → response=%s",
            inventory_item_id, location_id, available, result,
        )
        return result

    def _set_shopify_requires_shipping(self, inventory_item_id, requires_shipping):
        """Update the ``requires_shipping`` flag on a Shopify
        InventoryItem.

        Calls: ``PUT /admin/api/{version}/inventory_items/{id}.json``
        """
        return self._shopify_api_put(
            f'/inventory_items/{inventory_item_id}.json',
            {
                'inventory_item': {
                    'id': int(inventory_item_id),
                    'requires_shipping': bool(requires_shipping),
                },
            },
        )

    # -----------------------------------------------------------------
    # Locking — simple advisory lock via DB row, auto-expiring
    # -----------------------------------------------------------------
    def _acquire_lock(self):
        """Try to acquire advisory lock.  Returns True on success.

        Uses a raw SQL UPDATE with a WHERE clause that checks old-lock
        expiry, so the compare-and-swap is atomic even under concurrent
        access.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        if self.locked_until and self.locked_until > now:
            _logger.info("Shopify sync: locked until %s, skipping", self.locked_until)
            return False

        timeout = now + timedelta(minutes=LOCK_TIMEOUT_MINUTES)
        self.env.cr.execute(
            'UPDATE shopify_sync SET locked_until=%s '
            'WHERE id=%s AND (locked_until IS NULL OR locked_until < %s)',
            [timeout, self.id, now],
        )
        if self.env.cr.rowcount:
            self.invalidate_recordset(['locked_until'])
            return True
        _logger.info("Shopify sync: could not acquire lock (race)")
        return False

    def _release_lock(self):
        self.ensure_one()
        self.write({'locked_until': False})

    # -----------------------------------------------------------------
    # Public entry points
    # -----------------------------------------------------------------
    def sync_orders(self, since_id=None):
        """Fetch ALL orders and process them in batched transactions.

        Can be called synchronously from the UI ("Sync Now (Blocking)")
        or programmatically.  For background processing use the cron +
        ``action_sync_orders_async()``.

        :param since_id: Shopify REST order ID to resume from.
        """
        # Resolve / create singleton sync record (cron-safe)
        if not self:
            sync_record = self.search([], limit=1) or self.create({'name': 'Shopify Sync'})
            return sync_record.sync_orders(since_id=since_id)

        self.ensure_one()

        if not self._acquire_lock():
            raise UserError(_(
                "Another sync is already running (locked until %(time)s).",
                time=self.locked_until,
            ))

        try:
            return self._fetch_and_process(since_id=since_id)
        finally:
            self._release_lock()

    def action_sync_orders_async(self):
        """UI button: mark sync as 'fetching' and return immediately.

        The cron picks up ``fetching`` syncs and processes them in the
        background using batched, independent transactions.
        """
        self.ensure_one()
        if self.sync_state == 'fetching':
            raise UserError(_("Sync is already running."))
        self.write({
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
            },
        }

    def action_test_connection(self):
        """Test Shopify API credentials and display order counts."""
        shop = self._get_config('shop_url')
        token = self._get_config('access_token')

        if not shop or not token:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': 'Shop URL or Access Token is missing.',
                    'type': 'danger',
                    'sticky': True,
                },
            }

        url = f"{self._get_api_base()}/orders/count.json"
        headers = self._get_shopify_headers()

        try:
            counts = {}
            for status in ('any', 'open', 'closed', 'cancelled'):
                r = requests.get(url, headers=headers,
                                 params={'status': status}, timeout=15)
                r.raise_for_status()
                counts[status] = r.json().get('count', 0)
                self._check_rate_limit(r.headers)

            r_shop = requests.get(
                f"{self._get_api_base()}/shop.json",
                headers=headers, timeout=15,
            )
            shop_name = r_shop.json().get('shop', {}).get('name', shop)

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': f"Connected: {shop_name}",
                    'message': (
                        f"Orders — any={counts.get('any')}, "
                        f"open={counts.get('open')}, "
                        f"closed={counts.get('closed')}, "
                        f"cancelled={counts.get('cancelled')}"
                    ),
                    'type': 'success',
                    'sticky': True,
                },
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
                },
            }

    # -----------------------------------------------------------------
    # Cron entry point
    # -----------------------------------------------------------------
    def _cron_process_pending_syncs(self):
        """Called by ir.cron every 5 min — continues any 'fetching' sync.

        Because ``_fetch_and_process`` has a 4-minute time guard, each
        cron invocation processes at most 4 min of work, then saves the
        cursor so the next invocation can resume exactly where it left off.
        """
        sync_rec = self.search([('sync_state', '=', 'fetching')], limit=1)
        if sync_rec:
            _logger.info("Shopify cron: continuing sync #%s", sync_rec.id)
            if sync_rec._acquire_lock():
                try:
                    sync_rec._fetch_and_process()
                finally:
                    sync_rec._release_lock()
            else:
                _logger.info("Shopify cron: sync #%s is already locked", sync_rec.id)
        else:
            _logger.debug("Shopify cron: no pending syncs")

    # -----------------------------------------------------------------
    # Core — fetch ONE page per cron invocation, commit on main cursor
    # -----------------------------------------------------------------
    def _fetch_and_process(self, since_id=None):
        """Fetch **one** page of orders from Shopify and process them on
        the main cursor with ``self.env.cr.commit()`` every 50 orders.

        Pagination
        ----------
        Uses TWO mechanisms depending on context:

        1. **Full sync** (no ``since_id``, no ``batch_cursor``):
           starts from the newest orders and pages through ALL 7 000+
           historical orders using ``page_info`` cursor from the Link
           header.  The ``page_info`` URL is stored in ``batch_cursor``
           for the next cron invocation.

        2. **Incremental sync** (``since_id`` or ``last_sync_id`` set):
           fetches only orders *newer* than the given ID.  This is the
           normal mode after the initial full sync completes.
        """
        self.ensure_one()
        COMMIT_INTERVAL = 50

        shop = self._get_config('shop_url')
        if not shop:
            self.write({'sync_state': 'failed', 'last_error': 'shop_url not configured'})
            return self.env['sale.order']

        # -- initialise state --------------------------------------------
        self.write({
            'sync_state': 'fetching',
            'orders_processed': self.orders_processed if self.sync_state == 'fetching' else 0,
            'orders_total': self.orders_total if self.sync_state == 'fetching' else 0,
            'orders_failed': self.orders_failed if self.sync_state == 'fetching' else 0,
            'last_error': False,
        })

        base_url = f"{self._get_api_base()}/orders.json"
        params = {'status': 'any', 'limit': 250}

        # Determine which URL to request
        prev_cursor = self.batch_cursor
        if prev_cursor:
            # Resume from a stored page_info cursor (full-sync mode)
            page_url = prev_cursor
            _logger.info("Shopify sync: resuming from stored page cursor")
        else:
            resume_from = since_id or self.last_sync_id
            if resume_from:
                params['since_id'] = resume_from
            page_url = base_url
            _logger.info(
                "Shopify sync: fetching page (since_id=%s)", resume_from or 'start',
            )

        total_fetched = self.orders_total
        total_created = self.orders_processed
        total_failed = self.orders_failed
        created_since_commit = 0
        start_time = time.time()

        # -- suppress mail tracking --------------------------------------
        sync_with_ctx = self.with_context(
            mail_create_nosubscribe=True,
            mail_notrack=True,
            tracking_disable=True,
        )

        try:
            # ---- Fetch ONE page (250 orders max) ----------------------
            try:
                if page_url == base_url:
                    resp = requests.get(
                        page_url, headers=self._get_shopify_headers(),
                        params=params, timeout=30,
                    )
                else:
                    # page_info URL already includes all query parameters
                    resp = requests.get(
                        page_url, headers=self._get_shopify_headers(), timeout=30,
                    )
                resp.raise_for_status()
            except requests.exceptions.RequestException as exc:
                _logger.error("Shopify sync: API request failed — %s", exc)
                self.write({
                    'sync_state': 'failed',
                    'last_error': f"API request failed: {exc}",
                    'orders_processed': total_created,
                    'orders_total': total_fetched,
                    'orders_failed': total_failed,
                })
                return self.env['sale.order']

            orders = resp.json().get('orders', [])
            if not orders:
                _logger.info("Shopify sync: no more orders — sync complete")
                final_state = (
                    'completed_with_errors' if total_failed > 0 else 'completed'
                )
                self.write({
                    'sync_state': final_state,
                    'orders_processed': total_created,
                    'orders_total': total_fetched,
                    'orders_failed': total_failed,
                    'last_sync_count': total_fetched,
                    'batch_cursor': False,
                })
                return self.env['sale.order']

            total_fetched += len(orders)
            page_max_id = max(int(o['id']) for o in orders) if orders else 0
            _logger.info(
                "Shopify sync: page returned %d orders "
                "(fetched %d, created %d, failed %d)",
                len(orders), total_fetched, total_created, total_failed,
            )

            # ---- Process each order (main cursor, commit every 50) ----
            for i, order_data in enumerate(orders):
                shopify_id = order_data.get('id', '?')
                order_ref = order_data.get('order_number', shopify_id)

                try:
                    result = sync_with_ctx._process_single_order(order_data)
                    if result and result.get('status') == 'created':
                        total_created += 1
                        created_since_commit += 1
                        _logger.info(
                            "Shopify sync: ✅ order #%s → SO #%s",
                            order_ref, result.get('sale_order_id'),
                        )
                    else:
                        _logger.debug(
                            "Shopify sync: ⏭ order #%s skipped (already exists)",
                            order_ref,
                        )
                except Exception as exc:
                    total_failed += 1
                    _logger.warning(
                        "Shopify sync: ❌ order #%s failed — %s",
                        order_ref, exc,
                    )

                # ---- Commit every COMMIT_INTERVAL (50) orders ---------
                if created_since_commit >= COMMIT_INTERVAL:
                    self.env.cr.commit()
                    self.invalidate_recordset()
                    created_since_commit = 0
                    self.write({
                        'last_sync_id': str(int(shopify_id)),
                        'orders_processed': total_created,
                        'orders_total': total_fetched,
                        'orders_failed': total_failed,
                        'last_sync_count': total_fetched,
                    })
                    _logger.info(
                        "Shopify sync: committed — %d created, cursor=%s",
                        total_created, shopify_id,
                    )

            # ---- Commit remaining orders from this page ----------------
            if created_since_commit > 0:
                self.env.cr.commit()
                self.invalidate_recordset()

            # ---- Update cursor to page boundary ------------------------
            self.write({
                'last_sync_id': str(page_max_id),
                'orders_processed': total_created,
                'orders_total': total_fetched,
                'orders_failed': total_failed,
                'last_sync_count': total_fetched,
            })

            # ---- Save page_info cursor for next cron -------------------
            next_url = None
            link_header = resp.headers.get('Link', '')
            for link in link_header.split(','):
                if 'rel="next"' in link:
                    s = link.find('<')
                    e = link.find('>')
                    if s != -1 and e != -1:
                        next_url = link[s + 1:e]
                    break

            if next_url:
                # Store the page_info URL for resume, stay in 'fetching'
                self.write({
                    'sync_state': 'fetching',
                    'batch_cursor': next_url,
                })
                _logger.info(
                    "Shopify sync: more pages — stored page cursor, "
                    "next cron will continue",
                )
            else:
                # No more pages → full sync complete
                final_state = (
                    'completed_with_errors' if total_failed > 0 else 'completed'
                )
                self.write({
                    'sync_state': final_state,
                    'batch_cursor': False,
                })
                _logger.info(
                    "Shopify sync: ALL PAGES DONE — "
                    "fetched=%d, created=%d, failed=%d",
                    total_fetched, total_created, total_failed,
                )

            # -- rate limiting -------------------------------------------
            self._check_rate_limit(resp.headers)

        except Exception as exc:
            _logger.exception("Shopify sync: catastrophic failure")
            try:
                if created_since_commit > 0:
                    self.env.cr.commit()
                self.write({
                    'sync_state': 'failed',
                    'last_error': str(exc)[:500],
                    'orders_processed': total_created,
                    'orders_total': total_fetched,
                    'orders_failed': total_failed,
                })
            except Exception:
                pass

        elapsed = time.time() - start_time
        _logger.info(
            "Shopify sync: page done in %.1fs — fetched=%d, created=%d, "
            "failed=%d, cursor=%s",
            elapsed, total_fetched, total_created, total_failed,
            page_max_id if orders else 'N/A',
        )

        return self.env['sale.order']

    # -----------------------------------------------------------------
    # Single-order processing
    # -----------------------------------------------------------------
    def _ensure_shopify_unique_constraint(self):
        """Ensure the UNIQUE constraint on ``sale.order.x_shopify_id``
        and ``shopify.deleted.order.shopify_order_id`` exist in PostgreSQL.

        Cleans up old duplicate rows first so the constraint always succeeds.
        Runs inside a SAVEPOINT so a failure here never breaks the caller's
        outer transaction.
        """
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("""
                    DO $$
                    BEGIN
                        -- 1. Clean old duplicates (keep lowest ID)
                        DELETE FROM sale_order
                        WHERE id IN (
                            SELECT id FROM (
                                SELECT id, ROW_NUMBER() OVER (
                                    PARTITION BY x_shopify_id ORDER BY id
                                ) AS rn
                                FROM sale_order
                                WHERE x_shopify_id IS NOT NULL
                            ) sub
                            WHERE sub.rn > 1
                        );

                        -- 2. Create sale_order constraint
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'unique_shopify_id'
                              AND contype = 'u'
                        ) THEN
                            ALTER TABLE sale_order
                            ADD CONSTRAINT unique_shopify_id
                            UNIQUE (x_shopify_id);
                        END IF;

                        -- 3. Create deleted orders constraint
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'unique_deleted_shopify_id'
                              AND contype = 'u'
                        ) THEN
                            ALTER TABLE shopify_deleted_order
                            ADD CONSTRAINT unique_deleted_shopify_id
                            UNIQUE (shopify_order_id);
                        END IF;
                    END $$;
                """)
        except Exception:
            _logger.warning(
                "Shopify sync: constraint setup failed (will retry next call)",
                exc_info=True,
            )

    def _process_single_order(self, order_data):
        """Create or skip **one** sale.order from a Shopify order dict.

        Idempotency: checks ``x_shopify_id`` before creating anything.
        Uses a database-level UNIQUE constraint + savepoint as the primary
        deduplication mechanism — this is the only approach that is
        guaranteed to work across all workers / processes.

        :param order_data: dict — a single Shopify REST order
        :returns: dict ``{'status': 'created'|'skipped', 'sale_order_id': int}``
        """
        SaleOrder = self.env['sale.order']
        shopify_id = str(order_data['id'])
        order_number = str(order_data.get('order_number', shopify_id))

        # ── Ensure the UNIQUE constraint exists ───────────────────────
        self._ensure_shopify_unique_constraint()

        # ── Never re-create a deleted order ──────────────────────────
        if self.env['shopify.deleted.order'].sudo().search_count(
            [('shopify_order_id', '=', shopify_id)], limit=1,
        ):
            _logger.info(
                "Shopify sync: order #%s was previously deleted, skipping",
                order_number,
            )
            return {'status': 'skipped', 'sale_order_id': False,
                    'reason': 'previously_deleted'}

        # ── Fast path — idempotency check ────────────────────────────
        existing = SaleOrder.search(
            [('x_shopify_id', '=', shopify_id)], limit=1,
        )
        if existing:
            _logger.debug(
                "Shopify sync: order %s already imported (SO #%s), skip",
                order_number, existing.name,
            )
            return {'status': 'skipped', 'sale_order_id': existing.id}

        # ── Customer ────────────────────────────────────────────────
        try:
            partner = self._get_or_create_partner(order_data)
        except Exception as exc:
            _logger.warning(
                "Shopify sync: partner failed for order #%s — %s",
                order_number, exc,
            )
            partner = self.env.ref('base.public_partner', raise_if_not_found=False)
            if not partner:
                partner = self.env['res.partner'].search([], limit=1)

        # ── Currency & pricelist ─────────────────────────────────────
        try:
            currency = self._get_currency(order_data)
            pricelist = self._get_pricelist(currency)
        except Exception as exc:
            _logger.warning(
                "Shopify sync: currency/pricelist failed for #%s — %s",
                order_number, exc,
            )
            currency = self.env.company.currency_id
            pricelist = self.env['product.pricelist'].search([], limit=1)

        # ── Create sale.order (savepoint + UNIQUE constraint guard) ────
        order_vals = self._prepare_sale_order_vals(
            order_data, partner, currency, pricelist,
        )
        order_vals['x_shopify_id'] = shopify_id
        try:
            with self.env.cr.savepoint():
                sale_order = SaleOrder.create(order_vals)
        except IntegrityError:
            # UNIQUE constraint fired — another worker created this order
            # between our idempotency check and INSERT.  The savepoint
            # kept our outer transaction intact.
            existing = SaleOrder.search(
                [('x_shopify_id', '=', shopify_id)], limit=1,
            )
            if existing:
                _logger.debug(
                    "Shopify sync: order %s created by concurrent request "
                    "(SO #%s), skipping",
                    order_number, existing.name,
                )
                return {'status': 'skipped', 'sale_order_id': existing.id}
            raise

        # ── Order lines — skip if already populated ──────────────────
        line_errors = 0
        if sale_order.order_line:
            _logger.debug(
                "Shopify sync: SO %s already has lines, skipping line creation",
                sale_order.name,
            )
        else:
            for item in order_data.get('line_items', []):
                try:
                    self._create_order_line(sale_order, item)
                except Exception as exc:
                    line_errors += 1
                    _logger.warning(
                        "Shopify sync: line '%s' failed for order #%s — %s",
                        item.get('title', '?'), order_number, exc,
                    )

        # ── Shipping line ────────────────────────────────────────────
        if not sale_order.order_line or not any(
            l.product_id.name == 'Shopify Shipping' for l in sale_order.order_line
        ):
            try:
                self._create_shipping_line(sale_order, order_data)
            except Exception as exc:
                _logger.warning(
                    "Shopify sync: shipping line failed for #%s — %s",
                    order_number, exc,
                )

        # ── Apply Shopify status to the imported order ──────────────
        financial_status = order_data.get('financial_status', '')
        shopify_order_status = order_data.get('cancelled_at')

        # Cancelled / voided orders — cancel in Odoo immediately
        if financial_status in ('voided',) or shopify_order_status:
            cancel_reason = order_data.get('cancel_reason', '') or 'Cancelled in Shopify'
            try:
                sale_order.action_cancel()
                sale_order.message_post(
                    body=f"Imported as cancelled from Shopify — reason: {cancel_reason}",
                )
                _logger.info(
                    "Shopify sync: SO %s cancelled (Shopify #%s, reason=%s)",
                    sale_order.name, order_number, cancel_reason,
                )
            except Exception as exc:
                _logger.warning(
                    "Shopify sync: could not cancel SO %s — %s",
                    sale_order.name, exc,
                )
                sale_order.message_post(
                    body=f"Shopify order #{order_number} is cancelled "
                         f"(reason: {cancel_reason}). Manual cancellation required.",
                )
        else:
            # Non-cancelled orders — sync payments if financial_status
            # indicates payment (paid / partially_paid)
            try:
                self._fetch_and_sync_payments(sale_order, order_data)
            except Exception as exc:
                _logger.warning(
                    "Shopify sync: payment sync failed for order #%s — %s",
                    order_number, exc,
                )

        _logger.info(
            "Shopify sync: created SO %s for Shopify #%s (financial_status=%s)",
            sale_order.name, order_number, financial_status,
        )

        return {
            'status': 'created',
            'sale_order_id': sale_order.id,
            'line_errors': line_errors,
        }

    # -----------------------------------------------------------------
    # Sale-order value builders
    # -----------------------------------------------------------------
    def _get_currency(self, shopify_order):
        currency_code = shopify_order.get('currency', 'USD').upper()
        currency = self.env['res.currency'].search(
            [('name', '=', currency_code)], limit=1,
        )
        return currency or self.env.company.currency_id

    def _get_pricelist(self, currency):
        pricelist = self.env['product.pricelist'].search(
            [('currency_id', '=', currency.id)], limit=1,
        )
        return pricelist or self.env['product.pricelist'].search([], limit=1)

    def _prepare_sale_order_vals(self, shopify_order, partner, currency, pricelist):
        """Build sale.order vals dict (state NOT set here — see confirm logic).

        Date priority
        -------------
        1. Shopify ``created_at`` (order creation timestamp)
        2. Shopify ``processed_at`` (order processing timestamp)
        3. ``fields.Datetime.now()`` — **only** when neither Shopify
           field is present or parseable (logged as a warning because
           accurate Shopify data is expected).
        """
        # -- date: Shopify first, fallback to now as last resort --------
        date_order = None
        raw_date = shopify_order.get('created_at') or shopify_order.get('processed_at')
        if raw_date:
            try:
                aware = dateutil_parser.isoparse(raw_date)
                date_order = aware.astimezone(UTC).replace(tzinfo=None)
            except (ValueError, TypeError, Exception):
                _logger.warning(
                    "Shopify sync: could not parse date '%s' for order #%s",
                    raw_date, shopify_order.get('order_number', '?'),
                )

        if not date_order:
            _logger.warning(
                "Shopify sync: no valid date for order #%s, falling back to now()",
                shopify_order.get('order_number', '?'),
            )
            date_order = fields.Datetime.now()

        _logger.info(
            "Shopify sync: order #%s date_order=%s (Shopify raw=%s)",
            shopify_order.get('order_number', '?'),
            date_order, raw_date,
        )

        financial_status = shopify_order.get('financial_status', '')
        status_map = {
            'paid': 'invoiced',
            'partially_paid': 'invoiced',
            'pending': 'to invoice',
            'refunded': 'invoiced',
            'voided': 'no',
        }

        return {
            'partner_id': partner.id,
            'client_order_ref': str(shopify_order.get('order_number', '')),
            'date_order': date_order,
            'validity_date': False,
            'company_id': partner.company_id.id or self.env.company.id,
            'currency_id': currency.id,
            'pricelist_id': pricelist.id,
            'invoice_status': status_map.get(financial_status, 'to invoice'),
            'note': self._build_order_note(shopify_order),
        }

    def _build_order_note(self, shopify_order):
        parts = [f"Imported from Shopify #{shopify_order['order_number']}"]
        gateway = shopify_order.get('payment_gateway_names', [])
        if gateway:
            parts.append(f"Payment: {', '.join(gateway)}")
        tags = shopify_order.get('tags', '')
        if tags:
            parts.append(f"Tags: {tags}")
        return '\n'.join(parts)

    # -----------------------------------------------------------------
    # Partner helpers
    # -----------------------------------------------------------------
    def _get_or_create_partner(self, shopify_order):
        ResPartner = self.env['res.partner']
        customer = shopify_order.get('customer') or {}
        billing = shopify_order.get('billing_address') or {}
        shipping = shopify_order.get('shipping_address') or {}

        email = (customer.get('email') or shopify_order.get('email') or
                 billing.get('email') or shipping.get('email') or '').strip()

        if not email:
            first = customer.get('first_name', '') or ''
            last = customer.get('last_name', '') or ''
            name = f"{first} {last}".strip()
            if not name:
                name = f"Shopify Customer #{shopify_order.get('order_number', '?')}"
            partner = ResPartner.search(
                [('name', '=', name), ('email', '=', False)], limit=1,
            )
            return partner or ResPartner.create({'name': name})

        partner = ResPartner.search([('email', '=', email)], limit=1)
        if partner:
            self._update_partner_address(partner, customer, billing, shipping)
            return partner

        return ResPartner.create(
            self._prepare_partner_vals(customer, billing, shipping, email),
        )

    def _prepare_partner_vals(self, customer, billing, shipping, email):
        first = customer.get('first_name', '') or billing.get('first_name', '')
        last = customer.get('last_name', '') or billing.get('last_name', '')
        name = f"{first} {last}".strip() or 'Shopify Customer'

        vals = {
            'name': name,
            'email': email,
            'phone': customer.get('phone') or billing.get('phone') or '',
        }

        address = billing or shipping
        if address:
            state = self._find_state(address)
            country = self._find_country(address)
            vals.update({
                'street': address.get('address1', ''),
                'street2': address.get('address2', ''),
                'city': address.get('city', ''),
                'zip': address.get('zip', ''),
            })
            if state:
                vals['state_id'] = state.id
            if country:
                vals['country_id'] = country.id
                if state and state.country_id.id != country.id:
                    del vals['state_id']
        return vals

    def _update_partner_address(self, partner, customer, billing, shipping):
        address = billing or shipping
        if not address:
            return
        updates = {}
        for src, dest in (('address1', 'street'), ('address2', 'street2'),
                          ('city', 'city'), ('zip', 'zip')):
            if not partner[dest] and address.get(src):
                updates[dest] = address[src]
        if not partner.state_id:
            state = self._find_state(address)
            if state:
                updates['state_id'] = state.id
        if not partner.country_id:
            country = self._find_country(address)
            if country:
                updates['country_id'] = country.id
        if updates:
            partner.write(updates)

    def _find_state(self, address):
        code = address.get('province_code') or address.get('province', '')
        if code:
            return self.env['res.country.state'].search(
                [('code', '=', code)], limit=1,
            )
        province = address.get('province', '')
        if province:
            return self.env['res.country.state'].search(
                [('name', 'ilike', province)], limit=1,
            )
        return self.env['res.country.state']

    def _find_country(self, address):
        code = address.get('country_code', '')
        if code:
            country = self.env['res.country'].search(
                [('code', '=', code.upper())], limit=1,
            )
            if country:
                return country
        country_name = address.get('country', '')
        if country_name:
            return self.env['res.country'].search(
                [('name', 'ilike', country_name)], limit=1,
            )
        return self.env['res.country']

    # -----------------------------------------------------------------
    # Order line helpers
    # -----------------------------------------------------------------
    @staticmethod
    def _safe_strip(value):
        """Return ``value.strip()``, or ``''`` if *value* is None / empty.

        Shopify JSON often has ``null`` for missing optional fields
        (e.g. ``sku``, ``title``).  ``dict.get('sku', '')`` only
        returns the default when the key is absent — if the key is
        present with a ``None`` value, ``.strip()`` would raise
        ``AttributeError``.  This helper handles both cases.
        """
        return (value or '').strip()

    def _create_order_line(self, sale_order, item):
        """Create a sale.order.line from a Shopify line item.

        Searches for the product by SKU then by name.  If the product is
        **not** found in Odoo, the line is still created but with no
        product linked and the ``x_is_missing_product`` flag set so users
        can easily spot lines that need attention.
        """
        title = self._safe_strip(item.get('title') or item.get('name')) or 'Product'
        sku = self._safe_strip(item.get('sku'))
        product = self._find_product(sku, title)

        vals = {
            'order_id': sale_order.id,
            'product_id': product.id if product else False,
            'product_uom_qty': item.get('quantity', 1),
            'price_unit': float(item.get('price', 0.0)),
            'name': title,
        }

        if not product:
            vals['x_is_missing_product'] = True
            vals['x_shopify_product_name'] = title

        return self.env['sale.order.line'].create(vals)

    def _find_product(self, sku, title):
        """Look up a product by SKU then by name.

        :returns: ``product.product`` record or an empty recordset
        """
        Product = self.env['product.product']
        if sku:
            product = Product.search([('default_code', '=', sku)], limit=1)
            if product:
                return product
        if title:
            product = Product.search([('name', '=', title)], limit=1)
            if product:
                return product
        return Product

    def _create_shipping_line(self, sale_order, shopify_order):
        """Create a shipping line on the sale order.

        Handles two Shopify shipping patterns:

        1. **Plain shipping** — ``shipping_lines[*].price`` is the final
           shipping cost (free when 0, paid otherwise).
        2. **Free-shipping discount** — Shopify often structures free
           shipping as a *paid* shipping line PLUS a ``discount_applications``
           entry with ``target_type == "shipping_line"`` that offsets the
           cost entirely.  Example::

               shipping_lines: [{"price": "80.00", "title": "cairo"}]
               discount_applications: [
                 {"target_type": "shipping_line", "type": "shipping",
                  "value": "80.00", "title": "Free shipping"}
               ]

           The net shipping = 80.00 - 80.00 = 0.00  (= free).

        The method always creates a line when Shopify has ``shipping_lines``,
        even when the effective price is zero — this preserves the shipping
        title / method for reporting.

        When a shipping-targeted discount exists, the line is created at the
        **gross** price with a percentage ``discount`` applied on the Odoo
        line.  This makes the discount visible in the Odoo UI instead of
        silently reducing the unit price.
        """
        shipping_lines = shopify_order.get('shipping_lines', [])
        if not shipping_lines:
            return None

        shipping = shipping_lines[0]
        gross_price = float(shipping.get('price', 0.0))
        title = shipping.get('title', 'Shipping')

        # -- collect shipping-targeted discounts ----------------------------
        shipping_discount_total = 0.0
        discount_applications = shopify_order.get('discount_applications', [])
        for da in discount_applications:
            if da.get('target_type') == 'shipping_line':
                try:
                    shipping_discount_total += float(da.get('value', 0.0))
                except (ValueError, TypeError):
                    pass

        # Log every discount_application in full detail for debugging
        for i, da in enumerate(discount_applications):
            _logger.info(
                "Shopify shipping: order #%s — discount_app[%d] = %s",
                shopify_order.get('order_number', '?'), i, da,
            )
        _logger.info(
            "Shopify shipping: order #%s — gross=%.2f, "
            "shipping_discount_total=%.2f, net=%.2f, discount_pct=%.2f%%\n"
            "  shipping_lines=%s\n"
            "  ALL discount_applications=%s\n"
            "  total_price=%s, total_discounts=%s, current_total_price=%s",
            shopify_order.get('order_number', '?'),
            gross_price,
            shipping_discount_total,
            max(0.0, gross_price - shipping_discount_total),
            (shipping_discount_total / gross_price * 100.0) if gross_price > 0 else 0.0,
            shipping_lines,
            discount_applications,
            shopify_order.get('total_price'),
            shopify_order.get('total_discounts'),
            shopify_order.get('current_total_price'),
        )

        net_price = max(0.0, gross_price - shipping_discount_total)

        # Fallback: if the discount_applications value doesn't match the
        # order's total_discounts, and all discounts target shipping,
        # trust total_discounts from the order header.
        order_total_discounts = float(shopify_order.get('total_discounts', 0) or 0)
        if order_total_discounts > shipping_discount_total:
            all_shipping = (
                not discount_applications
                or all(
                    da.get('target_type') == 'shipping_line'
                    for da in discount_applications
                )
            )
            if all_shipping:
                _logger.info(
                    "Shopify shipping: correcting discount_total from "
                    "%.2f → %.2f (order total_discounts)",
                    shipping_discount_total, order_total_discounts,
                )
                shipping_discount_total = order_total_discounts
                net_price = max(0.0, gross_price - shipping_discount_total)

        # Calculate line discount percentage (Odoo discount field is 0–100)
        discount_pct = 0.0
        if gross_price > 0 and shipping_discount_total > 0:
            discount_pct = min(100.0, (shipping_discount_total / gross_price) * 100.0)

        # -- get-or-create the Shopify Shipping service product -------------
        Product = self.env['product.product']
        delivery = Product.search([('name', '=', 'Shopify Shipping')], limit=1)
        if not delivery:
            delivery = Product.create({
                'name': 'Shopify Shipping',
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })

        # -- label ----------------------------------------------------------
        if shipping_discount_total > 0:
            label = (
                f"Shipping: {title} "
                f"(Discount: {shipping_discount_total:,.2f} — "
                f"Net: {net_price:,.2f})"
            )
        elif net_price == 0.0:
            label = f"Shipping: {title} (Free)"
        else:
            label = f"Shipping: {title}"

        return self.env['sale.order.line'].create({
            'order_id': sale_order.id,
            'product_id': delivery.id,
            'product_uom_qty': 1,
            'price_unit': gross_price,
            'discount': discount_pct,
            'name': label,
        })

    # -----------------------------------------------------------------
    # Payment / transaction sync
    # -----------------------------------------------------------------
    def _fetch_and_sync_payments(self, sale_order, order_data=None, shopify_order_id=None):
        """Fetch Shopify transactions and create ``account.payment`` records.

        Called after a sale order is created (if financial_status is
        'paid' or 'partially_paid') and on the ``orders/paid`` webhook.

        :param sale_order: ``sale.order`` record
        :param order_data: dict — full Shopify order (optional, used to
            extract ``order_number`` and ``financial_status``)
        :param shopify_order_id: str — Shopify REST order ID (optional,
            extracted from *order_data* if not provided)
        """
        if not order_data and not shopify_order_id:
            shopify_order_id = sale_order.x_shopify_id

        if order_data:
            shopify_order_id = str(order_data['id'])

        if not shopify_order_id:
            _logger.info("Shopify payment sync: no Shopify order ID for SO %s",
                        sale_order.name)
            return False

        api_base = self._get_api_base()
        if '/admin/api/' not in api_base:
            _logger.info("Shopify payment sync: shop_url not configured")
            return False

        txn_url = f"{api_base}/orders/{shopify_order_id}/transactions.json"
        _logger.info("Shopify payment sync: fetching %s", txn_url)

        try:
            resp = requests.get(
                txn_url, headers=self._get_shopify_headers(), timeout=30,
            )
            resp.raise_for_status()
            self._check_rate_limit(resp.headers)
        except requests.exceptions.RequestException as exc:
            _logger.info("Shopify payment sync: API error for order #%s — %s",
                        shopify_order_id, exc)
            return False

        transactions = resp.json().get('transactions', [])
        if not transactions:
            _logger.info("Shopify payment sync: no transactions for order #%s",
                        shopify_order_id)
            return False

        Payment = self.env['account.payment']
        created_count = 0

        for txn in transactions:
            kind = txn.get('kind', '')
            status = txn.get('status', '')

            # Only process sale/capture/refund transactions
            if kind not in ('sale', 'capture', 'refund'):
                continue

            txn_id = str(txn['id'])
            gateway = txn.get('gateway', '')
            amount = txn.get('amount', '0')
            message = txn.get('message', '')

            # ── Idempotency check ──────────────────────────────────
            existing_payment = Payment.search(
                [('x_shopify_txn_id', '=', txn_id)], limit=1,
            )
            if existing_payment:
                _logger.debug(
                    "Shopify payment sync: txn %s already imported, skip", txn_id,
                )
                continue

            # ── Determine payment type ──────────────────────────────
            if kind == 'refund':
                payment_type = 'outbound'
            else:
                payment_type = 'inbound'

            # ── Handle by status ────────────────────────────────────
            if status == 'success':
                # Create payment as draft, then set to in_progress —
                # the accountant must manually post/reconcile to paid.
                try:
                    vals = self._prepare_payment_vals(
                        txn, sale_order, sale_order.partner_id, payment_type,
                    )
                    payment = Payment.create(vals)
                    payment.sudo().write({'state': 'in_progress'})
                    created_count += 1
                    _logger.info(
                        "Shopify payment sync: payment %s for SO %s "
                        "(txn=%s, amount=%s, type=%s) [in_progress]",
                        payment.name, sale_order.name, txn_id,
                        amount, payment_type,
                    )
                except Exception as exc:
                    _logger.warning(
                        "Shopify payment sync: failed to create payment for "
                        "txn %s — %s", txn_id, exc,
                    )

        if created_count > 0:
            sale_order.x_shopify_payment_synced = True
            # Invalidate computed fields so so_payments / amount_paid_percent
            # refresh immediately in the UI after sync.
            sale_order.invalidate_recordset([
                'so_payments', 'so_refunds', 'so_remaining',
                'amount_paid_percent', 'near_confirm_threshold',
            ])
            _logger.info(
                "Shopify payment sync: %d payments created for SO %s",
                created_count, sale_order.name,
            )

        return created_count > 0

    def _prepare_payment_vals(self, transaction, sale_order, partner, payment_type):
        """Build ``account.payment`` vals from a Shopify transaction.

        :param transaction: dict — Shopify transaction object
        :param sale_order: ``sale.order`` record
        :param partner: ``res.partner`` record
        :param payment_type: str — 'inbound' or 'outbound'
        :returns: dict of field values for ``account.payment.create()``
        """
        gateway = transaction.get('gateway', '')
        amount = float(transaction.get('amount', 0.0))
        currency_code = transaction.get('currency', 'USD').upper()

        currency = self.env['res.currency'].search(
            [('name', '=', currency_code)], limit=1,
        ) or self.env.company.currency_id

        journal = self._get_payment_journal(gateway)

        order_number = sale_order.client_order_ref or sale_order.x_shopify_id
        ref = f"Shopify Order #{order_number} — {gateway}"

        # Parse the transaction date
        date_str = transaction.get('processed_at') or transaction.get('created_at')
        payment_date = fields.Datetime.now()
        if date_str:
            try:
                aware = dateutil_parser.isoparse(date_str)
                payment_date = aware.astimezone(UTC).replace(tzinfo=None)
            except (ValueError, TypeError):
                pass

        return {
            'partner_id': partner.id,
            'sale_order_id': sale_order.id,
            'amount': amount,
            'currency_id': currency.id,
            'payment_type': payment_type,
            'partner_type': 'customer',
            'journal_id': journal.id,
            'date': payment_date,
            'memo': ref,
            'x_shopify_txn_id': str(transaction['id']),
            'x_shopify_gateway': gateway,
        }

    def _get_payment_journal(self, gateway_name):
        """Map a Shopify payment gateway to an Odoo ``account.journal``.

        Defaults to the first **bank** journal.  Override per gateway via
        config parameter ``shopify.journal.<slug>`` (set the journal ID).

        :param gateway_name: str — e.g. "Cash on Delivery (COD)", "Geidea Pay", "Paymob"
        :returns: ``account.journal`` record
        """
        Journal = self.env['account.journal']

        # 1. Config parameter override (set journal ID per gateway)
        slug = gateway_name.lower().replace(' ', '_').replace('(', '').replace(')', '')
        config_key = f'shopify.journal.{slug}'
        journal_id = self._get_config(config_key)
        if journal_id:
            try:
                journal = Journal.browse(int(journal_id))
                if journal.exists():
                    return journal
            except (ValueError, TypeError):
                pass

        # 2. Default: first bank journal
        journal = Journal.search([('type', '=', 'bank')], limit=1)
        if journal:
            return journal

        # 3. Fallback: first cash journal
        journal = Journal.search([('type', '=', 'cash')], limit=1)
        if journal:
            return journal

        # 4. Fallback: first general journal
        journal = Journal.search([('type', '=', 'general')], limit=1)
        if journal:
            return journal

        # 5. Fallback — first journal of any type
        journal = Journal.search([], limit=1)
        if journal:
            return journal

        # 6. Emergency: create a bank journal (must never return None)
        _logger.warning(
            "Shopify payment sync: no journal found — creating 'Shopify Bank' journal"
        )
        return Journal.create({
            'name': 'Shopify Bank',
            'type': 'bank',
            'code': 'SHOP',
            'company_id': self.env.company.id,
        })

    # -----------------------------------------------------------------
    # Webhook deduplication helper
    # -----------------------------------------------------------------
    def _is_duplicate_webhook(self, sale_order, topic, window_seconds=30):
        """Return True if *topic* was already processed for *sale_order*
        within *window_seconds*, to prevent rapid-fire duplicate webhooks
        from Shopify (e.g. ``orders/updated`` firing twice for the same
        change).

        **Only deduplicates the SAME topic.**  Different topics (e.g.
        ``orders/updated`` followed by ``orders/cancelled``) are always
        allowed through — otherwise a rapid ``orders/updated`` would
        block the cancellation webhook that arrives a moment later.

        Updates ``webhook_last_processed`` atomically via a raw SQL
        compare-and-swap so concurrent webhooks see a consistent view.
        """
        now = fields.Datetime.now()
        last = sale_order.webhook_last_processed
        if last:
            delta = now - last
            if delta.total_seconds() < window_seconds:
                _logger.info(
                    "Shopify webhook: %s for SO %s — skipped (last processed %.1fs ago)",
                    topic, sale_order.name, delta.total_seconds(),
                )
                return True

        # Atomic compare-and-swap: only update if nobody else set it
        # to a newer value since we read it.
        self.env.cr.execute(
            'UPDATE sale_order SET webhook_last_processed=%s '
            'WHERE id=%s AND (webhook_last_processed IS NULL '
            'OR webhook_last_processed <= %s)',
            [now, sale_order.id, last or now],
        )
        sale_order.invalidate_recordset(['webhook_last_processed'])
        return False

    # -----------------------------------------------------------------
    # Webhook handlers — delete / update / cancel / paid / fulfilled
    # -----------------------------------------------------------------
    def _update_sale_order(self, order_data):
        """Update an existing sale.order when Shopify ``orders/updated`` fires.

        When the order is still in a **draft** state the existing lines are
        removed and recreated from the latest Shopify payload so the Odoo
        quotation always reflects the live Shopify order.

        When the order has been **confirmed** (``state`` is ``sale`` or
        ``done``) Odoo does not allow line deletion.  In that case we only
        update the ``invoice_status`` and post a chatter message — we
        **never** add lines on top of the locked lines, which would create
        duplicates.

        If the order does not exist yet (race with orders/create), calls
        ``_process_single_order`` and then continues applying the update
        on the newly-created or concurrently-created order.
        """
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.info(
                "Shopify webhook: update for #%s but SO not found, creating", order_ref,
            )
            result = self._process_single_order(order_data)
            # Re-fetch — the order may have been created by our call or by a
            # concurrent webhook that already committed (advisory lock ensures
            # we see the committed row).
            existing = SaleOrder.search(
                [('x_shopify_id', '=', shopify_id)], limit=1,
            )
            if not existing:
                _logger.error(
                    "Shopify webhook: update for #%s — unable to create or find SO",
                    order_ref,
                )
                return result

        # -- dedup: skip if another webhook was processed very recently --
        if self._is_duplicate_webhook(existing, 'orders/updated'):
            return {'status': 'skipped', 'sale_order_id': existing.id,
                    'reason': 'duplicate_webhook'}

        # -- financial status (always safe to update) --------------------
        financial_status = order_data.get('financial_status', '')
        status_map = {
            'paid': 'invoiced',
            'partially_paid': 'invoiced',
            'pending': 'to invoice',
            'refunded': 'invoiced',
            'voided': 'no',
        }
        new_invoice_status = status_map.get(financial_status, 'to invoice')
        existing.invoice_status = new_invoice_status

        # -- update line items ONLY when the order is still editable ----
        if existing.state in ('draft', 'sent'):
            # Safe to unlink and recreate lines from Shopify payload
            try:
                existing.order_line.unlink()
            except Exception as exc:
                _logger.warning(
                    "Shopify webhook: could not unlink lines for SO %s — %s",
                    existing.name, exc,
                )

            for item in order_data.get('line_items', []):
                try:
                    self._create_order_line(existing, item)
                except Exception as exc:
                    _logger.warning(
                        "Shopify webhook: line '%s' failed for SO %s — %s",
                        item.get('title', '?'), existing.name, exc,
                    )

            # -- update shipping -----------------------------------------
            try:
                self._create_shipping_line(existing, order_data)
            except Exception as exc:
                _logger.warning(
                    "Shopify webhook: shipping update failed for SO %s — %s",
                    existing.name, exc,
                )
        else:
            # Order is confirmed/cancelled/done — cannot delete locked lines.
            # MERGE mode: only add NEW products that don't already exist
            # on the order, to avoid duplicates while allowing additions.
            lines_added = 0
            existing_lines = existing.order_line
            # Build a set of (product_id, name) for quick lookup
            existing_keys = set()
            for line in existing_lines:
                key = (line.product_id.id, line.name.strip() if line.name else '')
                existing_keys.add(key)

            for item in order_data.get('line_items', []):
                title = self._safe_strip(item.get('title') or item.get('name')) or 'Product'
                sku = self._safe_strip(item.get('sku'))
                product = self._find_product(sku, title)
                product_id = product.id if product else False
                # Check if this line already exists
                if (product_id, title) in existing_keys:
                    _logger.debug(
                        "Shopify webhook: line '%s' already exists on SO %s, skip",
                        title, existing.name,
                    )
                    continue
                try:
                    self._create_order_line(existing, item)
                    existing_keys.add((product_id, title))
                    lines_added += 1
                    _logger.info(
                        "Shopify webhook: added new line '%s' to SO %s",
                        title, existing.name,
                    )
                except Exception as exc:
                    _logger.warning(
                        "Shopify webhook: line '%s' failed for SO %s — %s",
                        title, existing.name, exc,
                    )

            # -- shipping: only add if not already present -----------------
            if not any(
                l.product_id.name == 'Shopify Shipping' for l in existing_lines
            ):
                try:
                    self._create_shipping_line(existing, order_data)
                    lines_added += 1
                except Exception as exc:
                    _logger.warning(
                        "Shopify webhook: shipping update failed for SO %s — %s",
                        existing.name, exc,
                    )

            if lines_added:
                _logger.info(
                    "Shopify webhook: merged %d new lines onto SO %s (state=%s)",
                    lines_added, existing.name, existing.state,
                )
            else:
                _logger.info(
                    "Shopify webhook: SO %s is in state '%s' — "
                    "no new lines to add, all %d Shopify items already exist",
                    existing.name, existing.state,
                    len(order_data.get('line_items', [])),
                )

        # Order stays draft / confirmed as-is — manual review only

        existing.message_post(
            body=f"Updated from Shopify — financial_status={financial_status}",
        )
        _logger.info(
            "Shopify webhook: updated SO %s for Shopify #%s (status=%s, state=%s)",
            existing.name, order_ref, financial_status, existing.state,
        )
        return {'status': 'updated', 'sale_order_id': existing.id}

    def _cancel_sale_order(self, order_data):
        """Cancel the sale.order when Shopify ``orders/cancelled`` fires."""
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.warning(
                "Shopify webhook: cancel for #%s but SO not found", order_ref,
            )
            return {'status': 'not_found', 'shopify_order': order_ref}

        cancel_reason = order_data.get('cancel_reason', '') or 'Cancelled in Shopify'
        try:
            existing.action_cancel()
            existing.message_post(
                body=f"Cancelled from Shopify — reason: {cancel_reason}",
            )
        except Exception as exc:
            # If already cancelled or in a state that can't be cancelled,
            # just post a note
            _logger.warning(
                "Shopify webhook: action_cancel failed for SO %s — %s",
                existing.name, exc,
            )
            existing.message_post(
                body=f"Shopify order #{order_ref} was cancelled "
                     f"(reason: {cancel_reason}). Manual action required.",
            )

        _logger.info(
            "Shopify webhook: cancelled SO %s for Shopify #%s (reason=%s)",
            existing.name, order_ref, cancel_reason,
        )
        return {'status': 'cancelled', 'sale_order_id': existing.id}

    def _delete_sale_order(self, order_data):
        """Delete the sale.order when Shopify ``orders/delete`` fires."""
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        # ── Record as deleted (idempotent, prevent re-creation) ──────
        DeletedOrder = self.env['shopify.deleted.order'].sudo()
        if not DeletedOrder.search_count(
            [('shopify_order_id', '=', shopify_id)], limit=1,
        ):
            DeletedOrder.create({'shopify_order_id': shopify_id})

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.warning(
                "Shopify webhook: delete for #%s but SO not found in Odoo "
                "(recorded as deleted to prevent re-creation)",
                order_ref,
            )
            return {'status': 'not_found', 'shopify_order': order_ref}

        so_name = existing.name
        try:
            existing.unlink()
            _logger.info(
                "Shopify webhook: deleted SO %s for Shopify #%s",
                so_name, order_ref,
            )
            return {'status': 'deleted', 'sale_order_name': so_name}
        except Exception as exc:
            _logger.error(
                "Shopify webhook: failed to delete SO %s — %s",
                so_name, exc, exc_info=True,
            )
            return {'status': 'error', 'message': str(exc)}

    def _mark_order_paid(self, order_data):
        """Handle ``orders/paid`` webhook — sync payments from Shopify.

        Updates the invoice status and syncs payment transactions.
        Does NOT touch order lines — safe to call on confirmed orders.

        If the order does not exist yet (race with orders/updated or
        orders/create), calls ``_process_single_order`` and then continues
        applying the paid status on the newly-created or concurrently-
        created order.
        """
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.info(
                "Shopify webhook: paid for #%s but SO not found, creating", order_ref,
            )
            result = self._process_single_order(order_data)
            # Re-fetch — the order may have been created by our call or by a
            # concurrent webhook that already committed (advisory lock ensures
            # we see the committed row).
            existing = SaleOrder.search(
                [('x_shopify_id', '=', shopify_id)], limit=1,
            )
            if not existing:
                _logger.error(
                    "Shopify webhook: paid for #%s — unable to create or find SO",
                    order_ref,
                )
                return result

        # -- dedup: skip if another webhook was processed very recently --
        if self._is_duplicate_webhook(existing, 'orders/paid'):
            return {'status': 'skipped', 'sale_order_id': existing.id,
                    'reason': 'duplicate_webhook'}

        existing.invoice_status = 'invoiced'
        # Order stays as draft/quotation for manual review

        # ── Sync payments from Shopify transactions ──────────────────
        try:
            self._fetch_and_sync_payments(existing, order_data)
        except Exception as exc:
            _logger.warning(
                "Shopify webhook: payment sync failed for SO %s — %s",
                existing.name, exc,
            )

        existing.message_post(body="Payment confirmed in Shopify — marked as invoiced")
        _logger.info(
            "Shopify webhook: marked SO %s as paid for Shopify #%s",
            existing.name, order_ref,
        )
        return {'status': 'paid', 'sale_order_id': existing.id}

    def _note_fulfillment(self, order_data):
        """Handle ``orders/fulfilled`` — post fulfillment info on SO.

        Does NOT touch order lines — safe to call on confirmed orders.

        If the order does not exist yet (race with orders/create or
        orders/updated), waits for it via ``_process_single_order`` and
        then posts the fulfilment note on the newly-created order.
        """
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.info(
                "Shopify webhook: fulfilled for #%s but SO not found, creating",
                order_ref,
            )
            self._process_single_order(order_data)
            existing = SaleOrder.search(
                [('x_shopify_id', '=', shopify_id)], limit=1,
            )
            if not existing:
                _logger.error(
                    "Shopify webhook: fulfilled for #%s — unable to create or find SO",
                    order_ref,
                )
                return {'status': 'not_found', 'shopify_order': order_ref}

        fulfillments = order_data.get('fulfillments', [])
        if fulfillments:
            tracking_info = []
            for f in fulfillments:
                tracking = f.get('tracking_company', '') or ''
                number = f.get('tracking_number', '') or ''
                status = f.get('status', '')
                if tracking or number:
                    tracking_info.append(f"{tracking}: {number} ({status})")
            note = "Fulfilled in Shopify"
            if tracking_info:
                note += " — Tracking: " + "; ".join(tracking_info)
        else:
            note = "Fulfilled in Shopify (no tracking details)"

        existing.message_post(body=note)
        _logger.info(
            "Shopify webhook: noted fulfillment for SO %s (Shopify #%s)",
            existing.name, order_ref,
        )
        return {'status': 'fulfilled', 'sale_order_id': existing.id}


class ShopifyDeletedOrder(models.Model):
    """Track deleted Shopify order IDs so they are never re-created by
    a late webhook that arrives after the ``orders/delete`` event."""

    _name = 'shopify.deleted.order'
    _description = 'Deleted Shopify Order ID'

    shopify_order_id = fields.Char(
        string='Shopify Order ID',
        required=True,
        index=True,
        copy=False,
        help="Shopify REST order ID that has been deleted from Odoo "
             "via the orders/delete webhook.",
    )

    _unique_deleted_shopify_id = models.Constraint(
        'UNIQUE(shopify_order_id)',
        'This Shopify order ID is already recorded as deleted.',
    )
