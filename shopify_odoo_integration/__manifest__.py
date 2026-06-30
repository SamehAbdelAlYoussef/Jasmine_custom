# -*- coding: utf-8 -*-
{
    'name': 'Shopify Odoo Integration',
    'summary': 'Sync Shopify orders and customers into Odoo sale orders',
    'description': """
        Full integration between Shopify and Odoo:
        - Sync Shopify orders into Odoo sale.order
        - Auto-create customers from Shopify order data
        - Auto-create products from Shopify line items
        - Real-time webhook endpoint for orders/create
        - Scheduled cron job for periodic order sync
        - Configurable API credentials via Settings
    """,
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'author': 'Sameh AbdelAl',
    'sequence': 1,
    'depends': ['base', 'sale', 'sale_management', 'account', 'sales_orders_payment_follow'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/res_config_settings_views.xml',
        'views/shopify_sync_views.xml',
        'views/sale_order_views.xml',
        'views/account_payment_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
