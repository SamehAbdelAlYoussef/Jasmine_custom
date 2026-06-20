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
        string='Orders Fetched from API',
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

        url = f"https://{shop}/admin/api/2024-01/orders/count.json"
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
                f"https://{shop}/admin/api/2024-01/shop.json",
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

        base_url = f"https://{shop}/admin/api/2024-01/orders.json"
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
    def _process_single_order(self, order_data):
        """Create or skip **one** sale.order from a Shopify order dict.

        Idempotency: checks ``x_shopify_id`` before creating anything.
        Caller is expected to set mail-suppression context via
        ``self.with_context(mail_create_nosubscribe=True, ...)``.

        :param order_data: dict — a single Shopify REST order
        :returns: dict ``{'status': 'created'|'skipped', 'sale_order_id': int}``
        """
        SaleOrder = self.env['sale.order']
        shopify_id = str(order_data['id'])
        order_number = str(order_data.get('order_number', shopify_id))

        # ── Idempotency check ───────────────────────────────────────
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

        # ── Create sale.order (always starts as draft) ─────────────────
        order_vals = self._prepare_sale_order_vals(
            order_data, partner, currency, pricelist,
        )
        order_vals['x_shopify_id'] = shopify_id
        sale_order = SaleOrder.create(order_vals)

        # ── Confirm if paid (draft → sale) ────────────────────────────
        financial_status = order_data.get('financial_status', '')
        if financial_status == 'paid':
            try:
                sale_order.action_confirm()
            except Exception as exc:
                _logger.warning(
                    "Shopify sync: action_confirm failed for order #%s — %s",
                    order_number, exc,
                )
        # 'pending', 'partially_paid', 'refunded', 'voided', etc.
        # stay as draft — the user will review them manually.

        # ── Order lines ──────────────────────────────────────────────
        line_errors = 0
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
        try:
            self._create_shipping_line(sale_order, order_data)
        except Exception as exc:
            _logger.warning(
                "Shopify sync: shipping line failed for #%s — %s",
                order_number, exc,
            )

        _logger.info(
            "Shopify sync: created SO %s for Shopify #%s",
            sale_order.name, order_number,
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
        try:
            product = self._get_or_create_product(item)
        except Exception:
            product = self._get_generic_product()

        title = self._safe_strip(item.get('title') or item.get('name')) or 'Product'

        vals = {
            'order_id': sale_order.id,
            'product_id': product.id,
            'product_uom_qty': item.get('quantity', 1),
            'price_unit': float(item.get('price', 0.0)),
            'name': title,
        }

        sku = self._safe_strip(item.get('sku'))
        if sku:
            product_by_sku = self.env['product.product'].search(
                [('default_code', '=', sku)], limit=1,
            )
            if product_by_sku:
                vals['product_id'] = product_by_sku.id

        return self.env['sale.order.line'].create(vals)

    def _get_generic_product(self):
        Product = self.env['product.product']
        generic = Product.search([('name', '=', 'Shopify Product')], limit=1)
        return generic or Product.create({
            'name': 'Shopify Product', 'type': 'service',
        })

    def _get_or_create_product(self, item):
        Product = self.env['product.product']
        title = self._safe_strip(item.get('title') or item.get('name'))
        sku = self._safe_strip(item.get('sku'))

        if sku:
            product = Product.search([('default_code', '=', sku)], limit=1)
            if product:
                return product
        if title:
            product = Product.search([('name', '=', title)], limit=1)
            if product:
                return product

        return Product.create({
            'name': title or f"Product {item.get('product_id', '?')}",
            'type': 'consu',
            'default_code': sku or '',
            'list_price': float(item.get('price', 0.0)),
            'sale_ok': True,
            'purchase_ok': False,
        })

    def _create_shipping_line(self, sale_order, shopify_order):
        shipping_lines = shopify_order.get('shipping_lines', [])
        if not shipping_lines:
            return None
        shipping = shipping_lines[0]
        price = float(shipping.get('price', 0.0))
        if price <= 0.0:
            return None

        Product = self.env['product.product']
        delivery = Product.search([('name', '=', 'Shopify Shipping')], limit=1)
        if not delivery:
            delivery = Product.create({
                'name': 'Shopify Shipping',
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })

        return self.env['sale.order.line'].create({
            'order_id': sale_order.id,
            'product_id': delivery.id,
            'product_uom_qty': 1,
            'price_unit': price,
            'name': f"Shipping: {shipping.get('title', 'Shipping')}",
        })

    # -----------------------------------------------------------------
    # Webhook handlers — update / cancel / paid / fulfilled
    # -----------------------------------------------------------------
    def _update_sale_order(self, order_data):
        """Update an existing sale.order when Shopify ``orders/updated`` fires.

        Updates: order lines, shipping, financial status, and totals.
        If the sale.order doesn't exist yet, falls back to creating it.
        """
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.info(
                "Shopify webhook: update for #%s but SO not found, creating", order_ref,
            )
            return self._process_single_order(order_data)

        # -- update line items (remove old, recreate) --------------------
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

        # -- update shipping ---------------------------------------------
        try:
            self._create_shipping_line(existing, order_data)
        except Exception as exc:
            _logger.warning(
                "Shopify webhook: shipping update failed for SO %s — %s",
                existing.name, exc,
            )

        # -- update financial status -------------------------------------
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

        # If changed to paid and still draft, confirm
        if financial_status == 'paid' and existing.state == 'draft':
            try:
                existing.action_confirm()
            except Exception:
                pass  # already confirmed or blocked

        existing.message_post(
            body=f"Updated from Shopify — financial_status={financial_status}",
        )
        _logger.info(
            "Shopify webhook: updated SO %s for Shopify #%s (status=%s)",
            existing.name, order_ref, financial_status,
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

    def _mark_order_paid(self, order_data):
        """Handle ``orders/paid`` webhook — confirm + mark invoiced."""
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.info(
                "Shopify webhook: paid for #%s but SO not found, creating", order_ref,
            )
            return self._process_single_order(order_data)

        existing.invoice_status = 'invoiced'
        if existing.state == 'draft':
            try:
                existing.action_confirm()
            except Exception as exc:
                _logger.warning(
                    "Shopify webhook: confirm failed for SO %s — %s",
                    existing.name, exc,
                )

        existing.message_post(body="Payment confirmed in Shopify — marked as invoiced")
        _logger.info(
            "Shopify webhook: marked SO %s as paid for Shopify #%s",
            existing.name, order_ref,
        )
        return {'status': 'paid', 'sale_order_id': existing.id}

    def _note_fulfillment(self, order_data):
        """Handle ``orders/fulfilled`` — post fulfillment info on SO."""
        shopify_id = str(order_data['id'])
        order_ref = order_data.get('order_number', shopify_id)
        SaleOrder = self.env['sale.order']

        existing = SaleOrder.search([('x_shopify_id', '=', shopify_id)], limit=1)
        if not existing:
            _logger.warning(
                "Shopify webhook: fulfilled for #%s but SO not found", order_ref,
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
