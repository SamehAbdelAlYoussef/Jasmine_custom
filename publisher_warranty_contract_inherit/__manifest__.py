# -*- coding: utf-8 -*-
{
    'name': 'Enterprise Core Utility',
    'summary': 'Automated utility for local enterprise environment management.',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'author': 'Sameh AbdelAl',
    'depends': [
        'base',
        'web',
        'mail',
        # 'publisher_warranty_contract'
    ],
    'data': [],
    'post_init_hook': '_update_database_parameters',
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}