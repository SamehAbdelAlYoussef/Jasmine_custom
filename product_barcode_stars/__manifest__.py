{
    'name': 'Product Barcode & Stars',
    'summary': 'Auto-generate scannable barcodes, barcode images, and customer loyalty stars',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'author': 'Sameh AbdelAl',
    'sequence': 5,
    'depends': [
        'product',
        'purchase',
        'sale_management',
        'stock'
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'views/product_category_views.xml',
        'views/product_template_views.xml',
        'views/res_partner_views.xml',
        'report/product_barcode_label.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'product_barcode_stars/static/src/sass/stars.css',
        ],
    },
    'post_init_hook': 'recompute_barcode_images',
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
