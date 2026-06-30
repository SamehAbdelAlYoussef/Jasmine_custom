from odoo import api, fields, models, _

class Partner(models.Model):
    _inherit = 'res.partner'

    def action_open_sale_orders(self):
        """Open all Sale Orders / Quotations related to this partner."""
        self.ensure_one()

        try:
            # Odoo 19 modern API for resolving action XML IDs
            action = self.env["ir.actions.act_window"]._for_xml_id(
                "sale.action_quotations_with_onboarding"
            )
        except ValueError:
            # Fallback: define manually if for some reason the action is missing
            action = {
                'type': 'ir.actions.act_window',
                'name': _('Sales Orders / Quotations'),
                'res_model': 'sale.order',
                'view_mode': 'list',
            }

        # Apply your custom domain and context
        action.update({
            'domain': [
                ('partner_id', '=', self.id),
                ('state', '!=', 'cancel'),
            ],
            'context': {
                'default_partner_id': self.id,
                'search_default_customer': 1,
            },
        })

        return action

    def action_open_customer_payments(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Customer Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
        }
        action.update({
            'domain': [
                ('partner_id', '=', self.id),
                ('state', '!=', 'canceled'),
                ('payment_type', '=', 'inbound'),
            ],
            'context': {
                'default_partner_id': self.id,
                'search_default_customer': 1,
            },
        })
        return action

    def action_open_customer_refunds(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Customer Refunds'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
        }
        action.update({
            'domain': [
                ('partner_id', '=', self.id),
                ('state', '!=', 'canceled'),
                ('payment_type', '=', 'outbound'),
            ],
            'context': {
                'default_partner_id': self.id,
                'search_default_customer': 1,
            },
        })
        return action
