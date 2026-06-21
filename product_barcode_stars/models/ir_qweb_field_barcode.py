# -*- coding: utf-8 -*-
import base64
import logging
import re
from io import BytesIO

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
        Override barcode widget to use python-barcode (PNG via ImageWriter)
        instead of reportlab, which requires rlPyCairo on Linux.
        """
        if not value:
            return ''
        if not bool(re.match(r'^[\x00-\x7F]+$', value)):
            from odoo.addons.base.models.ir_qweb_fields import nl2br
            return nl2br(value)

        try:
            import barcode as py_barcode
            from barcode.writer import ImageWriter

            width = int(options.get('width', 600))
            height = int(options.get('height', 100))

            # python-barcode uses lowercase type names:
            # Code128->code128, EAN13->ean13, EAN8->ean8, UPCA->upca, QR->qr
            barcode_type = options.get('symbology', 'Code128')
            # Handle 'auto' like standard Odoo: auto-detect by value length
            if barcode_type == 'auto':
                symbology_guess = {8: 'EAN8', 13: 'EAN13'}
                barcode_type = symbology_guess.get(len(value), 'Code128')
            barcode_type = barcode_type.lower()

            writer = ImageWriter()
            writer.set_options({
                'module_width': 0.2,
                'module_height': 8.0,
                'quiet_zone': 1.0,
                'font_size': 6,
                'text_distance': 2.0,
                'background': 'white',
                'foreground': 'black',
            })

            code = py_barcode.get(barcode_type, value, writer=writer)
            buffer = BytesIO()
            code.write(buffer)
            buffer.seek(0)
            barcode_data = base64.b64encode(buffer.read()).decode()

            img_element = html.Element('img')
            for k, v in (options or {}).items():
                if k.startswith('img_') and k[4:] in {'style', 'class', 'alt', 'width', 'height'}:
                    img_element.set(k[4:], v)
            if not img_element.get('alt'):
                img_element.set('alt', _('Barcode %s', value))
            img_element.set('src', 'data:image/png;base64,%s' % barcode_data)

            return Markup(html.tostring(img_element, encoding='unicode'))

        except Exception:
            _logger.warning("python-barcode widget failed for value=%s", value, exc_info=True)
            return ''
