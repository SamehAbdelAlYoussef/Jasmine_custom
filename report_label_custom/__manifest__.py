{
    'name': 'Custom Product Label',
    'version': '19.0.1.0.0',
    'summary': 'Print custom barcode labels for products and stock pickings',
    'author': 'Sameh AbdelAl',
    'sequence': 4,
    'depends': ['stock', 'product'],
    'data': [
        'views/product_views.xml',
        'views/stock_picking_view.xml',
        'reports/product_label_custom.xml',
        'reports/stock_label.xml',
        'data/server_action.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}
