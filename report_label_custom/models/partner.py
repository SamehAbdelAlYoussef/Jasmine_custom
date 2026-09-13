from odoo import api, fields, models

class ResPartner(models.Model):
    _inherit = 'res.partner'

    x_vendor_code = fields.Char(
        'Vendor Code',
        help='Vendor code for the partner.',
    )