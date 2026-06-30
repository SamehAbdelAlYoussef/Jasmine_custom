# -*- coding: utf-8 -*-

{
    'name': 'Advance Payments on SO and Customer Smart Buttons',
    'version': '19.0.1.0.0',
    'summary': 'Smart Buttons on the Customer or Partner Forms to calculate the payments related to SO & Remaining or SO ammount & Total Refunds ',
    'author': 'Alaa Bektash, OVERNETWORK',
    'sequence': 2,
    'description': """ """,
    'category': 'Sales',
    'website': 'https://overnetwork.cloud',
    'images': [],
    'depends': ['sale_management','base','account'],
    'data': [
        'views/account_move.xml',
        'views/account_payment_view.xml',
        'views/report_customers_payments_follow.xml',
        'views/res_partner_views.xml',
        'views/report_payment_receipt_templates.xml',
        'views/sale_order_quot_inherit_views.xml',
        'views/sale_order_inherit_views.xml',
        'security/security.xml',
        'security/ir.model.access.csv',
    ],
    'assets': {
        'web.assets_backend': [
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}
