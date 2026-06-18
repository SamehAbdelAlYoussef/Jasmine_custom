from odoo import models
import base64
from io import BytesIO
import barcode
from barcode.writer import ImageWriter

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def print_labels(self):
        return self.env.ref(
            'report_label_custom.action_report_product_template_label_custom'
        ).report_action(self)



    def get_barcode_image(self):
        """Return base64 PNG barcode image for this product's barcode."""
        if not self.barcode:
            return ''
        rv = BytesIO()
        code = barcode.get('code128', self.barcode, writer=ImageWriter())
        code.write(rv)
        return base64.b64encode(rv.getvalue()).decode('utf-8')