# -*- coding: utf-8 -*-

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    envia_address_id = fields.Integer(
        string="Envia Address ID",
        copy=False,
        help="Address ID in Envia.com address book. Auto-filled on first shipment.",
    )
    envia_branch_code = fields.Char(
        string="Envia Branch Code",
        copy=False,
        help="Manual override for the destination branch code used by "
             "'a sucursal' shipping services (e.g. Correo Argentino "
             "'standard_suc'). Leave empty to auto-resolve from the partner's "
             "postal code. Codes come from "
             "GET https://queries.envia.com/branches/<country>.",
    )
