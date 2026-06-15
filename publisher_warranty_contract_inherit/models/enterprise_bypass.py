# -*- coding: utf-8 -*-
from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        result = super(IrHttp, self).session_info()

        result['warning'] = False
        result['expiration_date'] = '2036-01-01 00:00:00'
        result['expiration_reason'] = False
        result['database_status'] = 'valid'

        if 'enterprise_expiration_reason' in result:
            result['enterprise_expiration_reason'] = False

        return result


class PublisherWarrantyContract(models.AbstractModel):
    _inherit = 'publisher_warranty.contract'

    def update_notification(self, cron_mode=True):
        return True