# -*- coding: utf-8 -*-

import logging

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


class EnviaShipmentErrorWizard(models.TransientModel):
    _name = 'envia.shipment.error.wizard'
    _description = 'Envia Shipment Error Wizard'

    picking_id = fields.Many2one('stock.picking', string='Delivery', required=True)
    error_message = fields.Text(string='Error', readonly=True)

    def action_retry(self):
        """Retry the whole validation.

        button_validate() now wraps the validation in a savepoint, so the
        failed attempt left the picking un-validated (not done, no stock
        moved). Re-running button_validate() re-attempts both the stock move
        and the label generation atomically. If it fails again it returns the
        error wizard action; on success it validates and creates the label."""
        self.ensure_one()
        return self.picking_id.button_validate()

    def action_address_only(self):
        """Skip shipment generation, just validate the picking.
        Address was already synced in button_validate."""
        self.ensure_one()
        picking = self.picking_id
        picking.message_post(
            body=_("Envia.com label skipped (address synced). Error was: %s") %
                 self.error_message)
        # Temporarily remove carrier to skip send_shipping, then restore
        carrier = picking.carrier_id
        picking.carrier_id = False
        try:
            picking.with_context(
                envia_skip_shipping=True,
                cancel_backorder=True,
            ).button_validate()
        finally:
            picking.carrier_id = carrier
        return {'type': 'ir.actions.act_window_close'}

    def action_cancel(self):
        """Cancel - do nothing, return to picking."""
        return {'type': 'ir.actions.act_window_close'}
