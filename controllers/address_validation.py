# -*- coding: utf-8 -*-

import logging

from odoo import _, http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal

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


class CustomerPortalEnviaZip(CustomerPortal):
    """Odoo 19: the checkout (/shop/address/submit) and the portal (/my/address/submit) both validate
    through CustomerPortal._validate_address_values, so one override covers both forms."""

    def _validate_address_values(self, address_values, partner_sudo, address_type, use_delivery_as_billing,
                                 required_fields, **kwargs):
        invalid_fields, missing_fields, error_messages = super()._validate_address_values(
            address_values, partner_sudo, address_type, use_delivery_as_billing, required_fields, **kwargs)
        zip_value = (address_values.get('zip') or '').strip()
        country_id = address_values.get('country_id')
        if zip_value and country_id and 'zip' not in invalid_fields:
            try:
                country = request.env['res.country'].browse(int(country_id)).exists()
            except (ValueError, TypeError):
                country = None
            message = _envia_zip_error(country, zip_value)
            if message:
                invalid_fields.add('zip')
                error_messages.append(message)
        return invalid_fields, missing_fields, error_messages


class EnviaZipValidationController(http.Controller):

    @http.route('/delivery_envia/validate_zip', type='jsonrpc', auth='public',
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
