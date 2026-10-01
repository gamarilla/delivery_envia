# -*- coding: utf-8 -*-

import logging

from odoo import _, http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.website_sale.controllers.main import WebsiteSale

from ..models.envia_request import validate_postal_code

_logger = logging.getLogger(__name__)


def _envia_zip_error(country, postal_code):
    """Return a translated error message when *postal_code* is invalid for
    *country* according to Envia.com, or None when it is valid.

    Fails open (returns None) when no active Envia carrier exists or the
    Envia APIs are unreachable, so checkout is never blocked by an outage.
    """
    if not country or not country.code or not postal_code:
        return None
    if not request.env['delivery.carrier'].sudo().search_count(
            [('delivery_type', '=', 'envia'), ('active', '=', True)], limit=1):
        return None

    valid, reason, format_hint = validate_postal_code(country.code, postal_code)
    if valid:
        return None
    if reason == 'format' and format_hint:
        return _("The postal code format is incorrect for %(country)s. "
                 "The correct format is: %(format)s") % {
                     'country': country.name, 'format': format_hint}
    if format_hint:
        return _("The postal code %(zip)s was not found for %(country)s. "
                 "The correct format is: %(format)s") % {
                     'zip': postal_code, 'country': country.name,
                     'format': format_hint}
    return _("The postal code %(zip)s was not found for %(country)s. "
             "Please double-check it.") % {
                 'zip': postal_code, 'country': country.name}


class WebsiteSaleEnviaZip(WebsiteSale):

    def checkout_form_validate(self, mode, all_form_values, data):
        error, error_message = super().checkout_form_validate(mode, all_form_values, data)
        zip_value = (data.get('zip') or '').strip()
        if zip_value and data.get('country_id') and 'zip' not in error:
            try:
                country = request.env['res.country'].browse(
                    int(data['country_id'])).exists()
            except (ValueError, TypeError):
                country = None
            message = _envia_zip_error(country, zip_value)
            if message:
                error['zip'] = 'error'
                error_message.append(message)
        return error, error_message


class CustomerPortalEnviaZip(CustomerPortal):

    def details_form_validate(self, data, partner_creation=False):
        error, error_message = super().details_form_validate(
            data, partner_creation=partner_creation)
        zip_value = (data.get('zipcode') or '').strip()
        if zip_value and data.get('country_id') and not error.get('zip') and not error.get('zipcode'):
            try:
                country = request.env['res.country'].browse(
                    int(data['country_id'])).exists()
            except (ValueError, TypeError):
                country = None
            message = _envia_zip_error(country, zip_value)
            if message:
                # The portal template highlights the field via error['zip']
                error['zip'] = 'error'
                error_message.append(message)
        return error, error_message


class EnviaZipValidationController(http.Controller):

    @http.route('/delivery_envia/validate_zip', type='json', auth='public',
                website=True, methods=['POST'])
    def validate_zip(self, country_id=None, zip=None, **kwargs):
        """Inline validation endpoint used by the address forms on focusout.
        Returns {'valid': bool, 'message': str}."""
        country = None
        try:
            if country_id:
                country = request.env['res.country'].sudo().browse(
                    int(country_id)).exists()
        except (ValueError, TypeError):
            pass
        message = _envia_zip_error(country, (zip or '').strip())
        return {'valid': not message, 'message': message or ''}
