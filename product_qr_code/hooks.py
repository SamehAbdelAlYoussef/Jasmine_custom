# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def recompute_barcode_images(cr, registry):
    """
    Post-init hook: force recompute barcode images for all products
    that already have a barcode set.
    """
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    products = env['product.product'].search([('barcode', '!=', False)])
    if products:
        _logger.info("Recomputing barcode images for %s products...", len(products))
        products._compute_barcode_image()

    templates = env['product.template'].search([('barcode', '!=', False)])
    if templates:
        templates._compute_barcode_image_template()

    _logger.info("Barcode images recomputed successfully.")
