# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    rating_stars = fields.Selection(
        [('0', 'No Stars'), ('1', '1 Star'), ('2', '2 Stars'), ('3', '3 Stars')],
        string='Customer Stars',
        compute='_compute_star_rating',
        store=True,
        readonly=True,
        help="Loyalty stars based on confirmed sale orders (max 3).",
    )

    @api.depends('sale_order_ids.state')
    def _compute_star_rating(self):
        """Compute star rating from confirmed (sale/done) orders count.

        Logic:
            0-1 orders  → 0 stars
            2 orders    → 1 star
            3 orders    → 2 stars
            4+ orders   → 3 stars (max)
        """
        for partner in self:
            confirmed = partner.sale_order_ids.filtered(
                lambda o: o.state in ('sale', 'done')
            )
            count = len(confirmed)
            if count <= 1:
                partner.rating_stars = '0'
            elif count == 2:
                partner.rating_stars = '1'
            elif count == 3:
                partner.rating_stars = '2'
            else:
                partner.rating_stars = '3'

    def _notify_star_increase(self, new_stars):
        """Post a chatter message when the star rating increases."""
        self.ensure_one()
        stars_int = int(new_stars)
        if stars_int <= 0:
            return
        message = _(
            "مبروك! لقد وصلت إلى مستوى %(stars)s نجوم 🌟 "
            "تقديراً لثقتك بنا. شكراً لكونك عميلاً مميزاً!"
        ) % {'stars': stars_int}
        self.message_post(body=message)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _get_partner_stars_snapshot(self):
        """Read current star rating from DB for relevant partners."""
        partners = self.mapped('partner_id').filtered('id')
        if not partners:
            return {}
        # Read from DB directly to avoid cached/invalidated values
        self.env.cr.execute(
            'SELECT id, rating_stars FROM res_partner WHERE id = ANY(%s)',
            [partners.ids],
        )
        return dict(self.env.cr.fetchall())

    def _notify_star_increases(self, old_stars_map):
        """Notify partners whose star rating actually increased."""
        notified = set()
        for order in self:
            partner = order.partner_id
            if not partner or partner.id in notified:
                continue
            # Accessing triggers recompute with new state
            new_stars = partner.rating_stars  # selection value: '0'|'1'|'2'|'3'
            old_stars = old_stars_map.get(partner.id, '0') or '0'
            if int(new_stars) > int(old_stars):
                partner._notify_star_increase(new_stars)
                notified.add(partner.id)
                _logger.info(
                    "Partner %s stars: %s → %s",
                    partner.display_name, old_stars, new_stars,
                )

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        confirmed = orders.filtered(lambda o: o.state in ('sale', 'done'))
        if confirmed:
            confirmed._notify_star_increases(confirmed._get_partner_stars_snapshot())
        return orders

    def write(self, vals):
        # Only check if state transitions to confirmed/completed
        state_change = 'state' in vals and vals['state'] in ('sale', 'done')
        old_stars = self._get_partner_stars_snapshot() if state_change else {}
        res = super().write(vals)
        if state_change:
            self._notify_star_increases(old_stars)
        return res
