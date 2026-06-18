{
    'name': 'Product Barcode Generator',
    'summary': 'Auto-generate scannable barcodes for products based on category + vendor + sequence',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'author': 'Sameh AbdelAl',
    'sequence': 1,
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
        'report/product_barcode_label.xml',
    ],
    'post_init_hook': 'recompute_barcode_images',
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
