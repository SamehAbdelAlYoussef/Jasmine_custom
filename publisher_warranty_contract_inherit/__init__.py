# -*- coding: utf-8 -*-
from . import models
from odoo import api, SUPERUSER_ID

def _update_database_parameters(env):
    """تحديث قيم قاعدة البيانات مباشرة فور تثبيت الموديول لضمان توافق النظام"""
    cr = env.cr
    cr.execute("""
        UPDATE ir_config_parameter 
        SET value = '2036-01-01 00:00:00' 
        WHERE key = 'database.expiration_date';
    """)
    cr.execute("""
        UPDATE ir_config_parameter 
        SET value = 'valid' 
        WHERE key = 'database.status';
    """)