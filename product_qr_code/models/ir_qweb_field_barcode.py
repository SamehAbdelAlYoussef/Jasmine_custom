# -*- coding: utf-8 -*-
import base64
import logging
import re

from lxml import html
from markupsafe import Markup

from odoo import _, api, models
from odoo.models import AbstractModel

_logger = logging.getLogger(__name__)


class IrQwebFieldBarcode(AbstractModel):
    _inherit = 'ir.qweb.field.barcode'

    @api.model
    def value_to_html(self, value, options=None):
        """
        Override barcode widget to handle SVG output from python-barcode.
        """
        if not value:
            return ''
        if not bool(re.match(r'^[\x00-\x7F]+$', value)):
            from odoo.addons.base.models.ir_qweb_fields import nl2br
            return nl2br(value)

        barcode_symbology = options.get('symbology', 'Code128')
        barcode_result = self.env['ir.actions.report'].barcode(
            barcode_symbology,
            value,
            **{key: v for key, v in options.items()
               if key in ['width', 'height', 'humanreadable', 'quiet', 'mask']})

        img_element = html.Element('img')
        for k, v in options.items():
            if k.startswith('img_') and k[4:] in {'style', 'class', 'alt', 'width', 'height'}:
                img_element.set(k[4:], v)
        if not img_element.get('alt'):
            img_element.set('alt', _('Barcode %s', value))

        # Handle both SVG (string) and PNG (bytes) results
        if isinstance(barcode_result, str):
            # SVG string -> base64 encode and use SVG data URI
            barcode_data = base64.b64encode(barcode_result.encode()).decode()
            img_element.set('src', 'data:image/svg+xml;base64,%s' % barcode_data)
        else:
            # PNG bytes -> standard PNG data URI
            img_element.set('src', 'data:image/png;base64,%s' % base64.b64encode(barcode_result).decode())

        return Markup(html.tostring(img_element, encoding='unicode'))
