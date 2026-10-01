# -*- coding: utf-8 -*-

from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    envia_insurance_value = fields.Float(
        string="Declared Value (Insurance)",
        help="Default declared value for shipping insurance/customs in company currency. "
             "Used by Envia.com when no manual override is set on the picking. "
             "Set a lower value than the sale price if you want to insure for less.",
    )
    envia_export_description = fields.Char(
        string="Customs Description",
        help="Product description for the customs declaration on international "
             "shipments (e.g. 'Data Acquisition Kit for Dynamometer OEM'). "
             "Sent to Envia.com together with the HS Code so duties and taxes "
             "can be classified and quoted (prepaid DDP). "
             "Falls back to the product name when empty.",
    )
