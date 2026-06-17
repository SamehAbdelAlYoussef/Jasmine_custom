# -*- coding: utf-8 -*-
import requests
import logging

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifySync(models.Model):
    _name = 'shopify.sync'
    _description = 'Shopify Order Sync'

    name = fields.Char(string='Name', default='Shopify Sync')
    last_sync_id = fields.Char(
        string='Last Synced Order ID',
        help="Tracks the last Shopify order ID synced for incremental sync",
    )
    last_sync_count = fields.Integer(
        string='Orders Fetched',
        help="Number of orders returned by the Shopify API in last sync",
    )

    def _get_config(self, key):
        """Get a Shopify config value from system parameters."""
        return self.env['ir.config_parameter'].sudo().get_param(f'shopify.{key}')

    def _get_shopify_headers(self):
        """Return HTTP headers for Shopify API requests."""
        token = self._get_config('access_token')
        return {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

    # -----------------------------------------------------------------
    # ORDER SYNC
    # -----------------------------------------------------------------

    def sync_orders(self, since_id=None):
        """Fetch ALL orders from Shopify (with pagination) and create/update sale orders.

        Can be called on a recordset or empty model (cron-safe).
        :param since_id: int or str — only fetch orders with ID > since_id
        :return: recordset of created/updated sale.order records
        """
        # Resolve to a singleton record — create one if needed (cron calls on empty model)
        if not self:
            sync_record = self.search([], limit=1)
            if not sync_record:
                sync_record = self.create({'name': 'Shopify Sync'})
            return sync_record.sync_orders(since_id=since_id)

        shop = self._get_config('shop_url')
        if not shop:
            _logger.error("Shopify sync: shop_url is not configured")
            return self.env['sale.order']

        base_url = f"https://{shop}/admin/api/2024-01/orders.json"
        params = {'status': 'any', 'limit': 250}
        if since_id:
            params['since_id'] = since_id

        all_sale_orders = self.env['sale.order']
        total_orders = 0
        page = 1
        next_url = base_url

        while next_url:
            _logger.info(
                "Shopify sync: page %d — fetching %s", page,
                next_url if next_url != base_url else f"{base_url}?{params}"
            )

            try:
                if next_url == base_url:
                    response = requests.get(
                        next_url, headers=self._get_shopify_headers(),
                        params=params, timeout=30,
                    )
                else:
                    # next_url already includes full URL with params
                    response = requests.get(
                        next_url, headers=self._get_shopify_headers(), timeout=30,
                    )
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                _logger.error("Shopify sync: API request failed on page %d — %s", page, e)
                break

            orders = response.json().get('orders', [])
            if not orders:
                break

            # Check Shopify API call limit header
            api_limit = response.headers.get('X-Shopify-Shop-Api-Call-Limit', '?')

            total_orders += len(orders)
            _logger.info(
                "Shopify sync: page %d — %d orders (total so far: %d) [API limit: %s]",
                page, len(orders), total_orders, api_limit,
            )

            for order_data in orders:
                try:
                    so = self._create_or_update_sale_order(order_data)
                    if so:
                        all_sale_orders |= so
                except Exception as e:
                    _logger.error(
                        "Shopify sync: failed to process order #%s — %s",
                        order_data.get('order_number', '?'), e,
                    )

            # Update last sync ID from the first page (highest ID)
            if page == 1 and orders:
                self.last_sync_id = str(orders[0]['id'])

            # Shopify pagination via Link header (rel="next")
            next_url = None
            link_header = response.headers.get('Link', '')
            for link in link_header.split(','):
                if 'rel="next"' in link:
                    # Extract URL from <url>
                    start = link.find('<')
                    end = link.find('>')
                    if start != -1 and end != -1:
                        next_url = link[start + 1:end]
                    break

            page += 1

        _logger.info(
            "Shopify sync: done — %d orders from API, %d sale orders in Odoo, %d pages",
            total_orders, len(all_sale_orders), page - 1,
        )
        self.last_sync_count = total_orders
        return all_sale_orders

    def action_test_connection(self):
        """Test Shopify API and show order count."""
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
                }
            }

        # Count orders with different statuses
        url = f"https://{shop}/admin/api/2024-01/orders/count.json"
        headers = {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

        try:
            # Count by status
            counts = {}
            for status in ['any', 'open', 'closed', 'cancelled']:
                r = requests.get(url, headers=headers, params={'status': status}, timeout=15)
                r.raise_for_status()
                counts[status] = r.json().get('count', 0)

            shop_r = requests.get(
                f"https://{shop}/admin/api/2024-01/shop.json",
                headers=headers, timeout=15,
            )
            shop_name = shop_r.json().get('shop', {}).get('name', shop)

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': f"Connected: {shop_name}",
                    'message': (
                        f"Orders: any={counts.get('any', '?')}, "
                        f"open={counts.get('open', '?')}, "
                        f"closed={counts.get('closed', '?')}, "
                        f"cancelled={counts.get('cancelled', '?')}"
                    ),
                    'type': 'success',
                    'sticky': True,
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

    def _create_or_update_sale_order(self, shopify_order):
        """Create or update a sale.order from a Shopify order dict.

        :param shopify_order: dict — Shopify order JSON
        :return: sale.order record or None
        """
        self.ensure_one()
        SaleOrder = self.env['sale.order']
        order_number = str(shopify_order['order_number'])

        # Check if order already exists
        existing = SaleOrder.search([
            ('client_order_ref', '=', order_number)
        ], limit=1)

        if existing:
            _logger.debug("Shopify sync: order #%s already exists, skipping", order_number)
            return existing

        # Find or create partner (customer)
        partner = self._get_or_create_partner(shopify_order)

        # Get or match currency
        currency = self._get_currency(shopify_order)
        pricelist = self._get_pricelist(currency)

        # Build sale order values
        order_vals = self._prepare_sale_order_vals(shopify_order, partner, currency, pricelist)
        sale_order = SaleOrder.create(order_vals)

        # Add product line items
        for item in shopify_order.get('line_items', []):
            self._create_order_line(sale_order, item)

        # Add shipping as a line item
        self._create_shipping_line(sale_order, shopify_order)

        _logger.info(
            "Shopify sync: created sale order %s for Shopify #%s (total: %s %s)",
            sale_order.name, order_number,
            shopify_order.get('total_price'), shopify_order.get('currency', ''),
        )
        return sale_order

    def _get_currency(self, shopify_order):
        """Find Odoo currency matching Shopify order currency."""
        currency_code = shopify_order.get('currency', 'USD').upper()
        currency = self.env['res.currency'].search([
            ('name', '=', currency_code)
        ], limit=1)
        return currency or self.env.company.currency_id

    def _get_pricelist(self, currency):
        """Get or create a pricelist for the given currency."""
        Pricelist = self.env['product.pricelist']
        pricelist = Pricelist.search([
            ('currency_id', '=', currency.id)
        ], limit=1)
        if not pricelist:
            pricelist = Pricelist.search([], limit=1)
        return pricelist

    def _prepare_sale_order_vals(self, shopify_order, partner, currency, pricelist):
        """Prepare the values dict for creating a sale.order."""
        # Parse Shopify ISO 8601 date with timezone (e.g. "2026-06-12T18:43:05-04:00")
        date_order = fields.Datetime.now()
        created_at = shopify_order.get('created_at') or shopify_order.get('processed_at')
        if created_at:
            try:
                date_order = fields.Datetime.to_datetime(created_at)
            except (ValueError, TypeError):
                _logger.warning("Could not parse date: %s, using now", created_at)

        vals = {
            'partner_id': partner.id,
            'client_order_ref': str(shopify_order.get('order_number', '')),
            'date_order': date_order,
            'validity_date': False,  # No expiration for imported orders
            'company_id': partner.company_id.id or self.env.company.id,
            'currency_id': currency.id,
            'pricelist_id': pricelist.id,
            'note': self._build_order_note(shopify_order),
        }

        # Map Shopify financial status to Odoo invoice_status
        financial_status = shopify_order.get('financial_status', '')
        status_map = {
            'paid': 'invoiced',
            'partially_paid': 'invoiced',
            'pending': 'to invoice',
            'refunded': 'invoiced',
            'voided': 'no',
        }
        if financial_status in status_map:
            vals['invoice_status'] = status_map[financial_status]

        return vals

    def _build_order_note(self, shopify_order):
        """Build a human-readable note from Shopify order metadata."""
        parts = [f"Imported from Shopify #{shopify_order['order_number']}"]

        gateway = shopify_order.get('payment_gateway_names', [])
        if gateway:
            parts.append(f"Payment: {', '.join(gateway)}")

        tags = shopify_order.get('tags', '')
        if tags:
            parts.append(f"Tags: {tags}")

        return '\n'.join(parts)

    # -----------------------------------------------------------------
    # PARTNER (CUSTOMER) HELPERS
    # -----------------------------------------------------------------

    def _get_or_create_partner(self, shopify_order):
        """Find an existing res.partner by email, or create a new one.

        :param shopify_order: dict — Shopify order JSON
        :return: res.partner record
        """
        ResPartner = self.env['res.partner']
        # Handle null values from JSON
        customer = shopify_order.get('customer') or {}
        billing = shopify_order.get('billing_address') or {}
        shipping = shopify_order.get('shipping_address') or {}

        email = (customer.get('email') or shopify_order.get('email') or
                 billing.get('email') or shipping.get('email') or '').strip()

        # For draft orders without customer info, use a generic partner
        if not email:
            name_parts = []
            if customer.get('first_name'):
                name_parts.append(customer.get('first_name', ''))
            if customer.get('last_name'):
                name_parts.append(customer.get('last_name', ''))
            name = ' '.join(name_parts).strip()
            if not name:
                name = f"Shopify Customer #{shopify_order.get('order_number', '?')}"
            # Search by name to avoid duplicates for draft orders
            partner = ResPartner.search([('name', '=', name), ('email', '=', False)], limit=1)
            if partner:
                return partner
            return ResPartner.create({'name': name})

        partner = ResPartner.search([('email', '=', email)], limit=1)
        if partner:
            self._update_partner_address(partner, customer, billing, shipping)
            return partner

        partner_vals = self._prepare_partner_vals(customer, billing, shipping, email)
        return ResPartner.create(partner_vals)

    def _prepare_partner_vals(self, customer, billing, shipping, email):
        """Prepare res.partner values from Shopify customer data."""
        first = customer.get('first_name', '') or billing.get('first_name', '')
        last = customer.get('last_name', '') or billing.get('last_name', '')
        name = f"{first} {last}".strip()

        vals = {
            'name': name or 'Shopify Customer',
            'email': email,
            'phone': customer.get('phone') or billing.get('phone') or '',
        }

        # Use billing for main address
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
                # Also set state only if it belongs to this country
                if state and state.country_id.id != country.id:
                    del vals['state_id']

        return vals

    def _update_partner_address(self, partner, customer, billing, shipping):
        """Update partner address fields if they are empty."""
        address = billing or shipping
        if not address:
            return
        updates = {}
        for src, dest in [
            ('address1', 'street'),
            ('address2', 'street2'),
            ('city', 'city'),
            ('zip', 'zip'),
        ]:
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
        """Find a res.country.state from an address dict. Returns record or False."""
        code = address.get('province_code') or address.get('province', '')
        if code:
            state = self.env['res.country.state'].search([
                ('code', '=', code)
            ], limit=1)
            return state if state else False
        # Try by name
        province = address.get('province', '')
        if province:
            state = self.env['res.country.state'].search([
                ('name', 'ilike', province)
            ], limit=1)
            return state if state else False
        return False

    def _find_country(self, address):
        """Find a res.country from an address dict. Returns record or False."""
        code = address.get('country_code', '')
        if code:
            country = self.env['res.country'].search([
                ('code', '=', code.upper())
            ], limit=1)
            return country if country else False
        # Try by name
        country_name = address.get('country', '')
        if country_name:
            country = self.env['res.country'].search([
                ('name', 'ilike', country_name)
            ], limit=1)
            return country if country else False
        return False

    # -----------------------------------------------------------------
    # ORDER LINE HELPERS
    # -----------------------------------------------------------------

    def _create_order_line(self, sale_order, item):
        """Create a sale.order.line from a Shopify line item.

        :param sale_order: sale.order record
        :param item: dict — Shopify line_item JSON
        :return: sale.order.line record
        """
        try:
            product = self._get_or_create_product(item)
        except Exception as e:
            _logger.warning(
                "Shopify sync: failed to find/create product '%s', using generic — %s",
                item.get('title', '?'), e,
            )
            product = self._get_generic_product()

        vals = {
            'order_id': sale_order.id,
            'product_id': product.id,
            'product_uom_qty': item.get('quantity', 1),
            'price_unit': float(item.get('price', 0.0)),
            'name': item.get('title') or item.get('name') or 'Product',
        }

        # Prefer SKU match
        sku = item.get('sku', '')
        if sku:
            product_by_sku = self.env['product.product'].search([
                ('default_code', '=', sku)
            ], limit=1)
            if product_by_sku:
                vals['product_id'] = product_by_sku.id

        return self.env['sale.order.line'].create(vals)

    def _get_generic_product(self):
        """Return a generic product to use as fallback."""
        Product = self.env['product.product']
        generic = Product.search([('name', '=', 'Shopify Product')], limit=1)
        if not generic:
            generic = Product.create({
                'name': 'Shopify Product',
                'type': 'service',
            })
        return generic

    def _get_or_create_product(self, item):
        """Find a product by Shopify item title/SKU, or create a new one.

        :param item: dict — Shopify line_item JSON
        :return: product.product record
        """
        Product = self.env['product.product']
        title = (item.get('title') or item.get('name') or '').strip()

        # First, try SKU
        sku = item.get('sku', '').strip()
        if sku:
            product = Product.search([('default_code', '=', sku)], limit=1)
            if product:
                return product

        # Then try name
        if title:
            product = Product.search([('name', '=', title)], limit=1)
            if product:
                return product

        # Create a new product
        return Product.create({
            'name': title or f"Product {item.get('product_id', '')}",
            'type': 'consu',
            'default_code': sku or '',
            'list_price': float(item.get('price', 0.0)),
            'sale_ok': True,
            'purchase_ok': False,
        })

    def _create_shipping_line(self, sale_order, shopify_order):
        """Create a sale.order.line for shipping cost from Shopify."""
        shipping_lines = shopify_order.get('shipping_lines', [])
        if not shipping_lines:
            return None

        shipping = shipping_lines[0]
        price = float(shipping.get('price', 0.0))
        if price <= 0.0:
            return None

        title = shipping.get('title', 'Shipping')
        delivery_product = self._get_delivery_product()

        return self.env['sale.order.line'].create({
            'order_id': sale_order.id,
            'product_id': delivery_product.id,
            'product_uom_qty': 1,
            'price_unit': price,
            'name': f"Shipping: {title}",
        })

    def _get_delivery_product(self):
        """Get or create a delivery product for shipping lines."""
        Product = self.env['product.product']
        delivery = Product.search([
            ('name', '=', 'Shopify Shipping')
        ], limit=1)
        if not delivery:
            delivery = Product.create({
                'name': 'Shopify Shipping',
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })
        return delivery