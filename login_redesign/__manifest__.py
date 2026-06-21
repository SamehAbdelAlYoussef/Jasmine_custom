# -*- coding: utf-8 -*-
{
    'name': 'Login Page Redesign',
    'summary': 'Modern animated login page with glassmorphism design',
    'version': '19.0.1.0.0',
    'category': 'Authentication',
    'sequence': 2,
    'author': 'Sameh AbdelAl',
    'depends': [
        'web',
        'auth_signup',
    ],
    'data': [
        'views/login_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'login_redesign/static/src/css/login_redesign.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
