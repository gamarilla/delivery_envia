from odoo import models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _action_confirm(self):
        """Odoo 19 creates a 'delivery' partner from the chosen pickup location on confirmation.
        Keep the Envia branch code on it so the label goes to that branch."""
        res = super()._action_confirm()
        for order in self:
            if order.carrier_id.delivery_type != 'envia':
                continue
            data = order.pickup_location_data if isinstance(order.pickup_location_data, dict) else {}
            branch = data.get('id')
            if branch and order.partner_shipping_id and order.partner_shipping_id.envia_branch_code != branch:
                order.partner_shipping_id.sudo().write({'envia_branch_code': branch})
        return res
