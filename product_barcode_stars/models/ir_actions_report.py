# -*- coding: utf-8 -*-
import base64
import io
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

try:
    import barcode
    from barcode.writer import SVGWriter
    HAS_PYTHON_BARCODE = True
except ImportError:
    HAS_PYTHON_BARCODE = False


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    @api.model
    def barcode(self, barcode_type, value, **kwargs):
        """
        Override barcode to use python-barcode SVG output.
        SVG renders natively in wkhtmltopdf.
        """
        if not HAS_PYTHON_BARCODE:
            return super().barcode(barcode_type, value, **kwargs)

        try:
            type_map = {
                'QR': 'qr', 'Code128': 'code128', 'Code39': 'code39',
                'EAN13': 'ean13', 'EAN8': 'ean8', 'UPCA': 'upca',
                'auto': 'code128',
            }
            py_type = type_map.get(barcode_type, 'code128')
            if not value.isdigit() and py_type not in ('code128', 'code39', 'qr'):
                py_type = 'code128'

            # Build SVG with proper sizing
            writer = SVGWriter()
            writer.set_options({
                'module_width': 0.4,
                'module_height': 20.0,
                'quiet_zone': 2.0,
                'font_size': 10,
                'text_distance': 3.0,
                'background': 'white',
                'foreground': 'black',
                'compress': False,
            })
            code = barcode.get(py_type, value, writer=writer)
            svg = code.render()

            # Make SVG responsive - remove fixed dimensions so CSS controls sizing
            svg_str = svg.decode() if isinstance(svg, bytes) else svg
            svg_str = svg_str.replace(
                '<svg ',
                '<svg style="max-width:100%;height:auto;" '
            )
            # Remove fixed width/height to allow CSS control
            import re as _re
            svg_str = _re.sub(r'\s+width="[^"]*"', '', svg_str)
            svg_str = _re.sub(r'\s+height="[^"]*"', '', svg_str)

            return svg_str
        except Exception as e:
            _logger.warning("python-barcode SVG failed: %s", e)
            return super().barcode(barcode_type, value, **kwargs)
