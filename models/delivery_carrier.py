import base64
import json
import logging
import re

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .envia_request import EnviaRequest, EnviaApiError, MULTI_PACKAGE_UNSUPPORTED

_logger = logging.getLogger(__name__)


def _user_friendly_rate_error(raw_message):
    """Convert a raw shipping-provider error string into a friendly, translatable
    message that does not reveal the provider's identity.

    Always returns a string already passed through ``_()`` where appropriate.
    """
    # Scrub provider-identifying references
    sanitized = re.sub(r'(?i)\benvia(?:\.com)?\b', '', str(raw_message)).strip(' :-.')

    lower = sanitized.lower()
    if 'postcode' in lower or 'postal code' in lower or 'zip' in lower:
        m = re.search(r'([A-Z]{2}\s*-\s*[\w()\d]+)', sanitized)
        if m:
            return _("Invalid postal code format. Expected: %s") % m.group(1)
        return _("Invalid postal code format for the destination country.")
    if 'no rates' in lower or 'no rate' in lower:
        return _("No shipping rates available for this destination.")
    if 'coverage' in lower:
        return _("Not available for this address.")
    if 'internal error' in lower:
        # The provider answers some malformed/unsupported payloads with nothing
        # but 'Internal error' (HTTP 200, no usable detail). The full request
        # and response are in the server log — see the module logger.
        return _("The shipping service could not quote this route. Please try "
                 "again; if it persists, check the package configuration.")
    if 'timeout' in lower or 'timed out' in lower:
        return _("The shipping service timed out. Please try again.")
    if 'could not connect' in lower or 'connection' in lower:
        return _("Could not reach the shipping service. Please try again later.")
    # anything else is technical detail for the log (rate_shipment already logs it), not for the customer
    return _("Shipping quote unavailable.")


def _envia_shipment_error_message(exc):
    """Return a clear, translated message for an Envia label / cancellation error.

    Maps the numeric error codes returned by /ship/generate/ and /ship/cancel/
    to actionable text (translated per the user's language via ``_()``). For
    unknown codes it falls back to the clean API message plus the code — never
    the raw ``{'code': ...}`` dict.

    Every returned string contains the literal ``Envia.com`` so the
    ``button_validate`` wizard in stock_picking.py intercepts the error and
    rolls the validation back, leaving the picking ready to retry.

    Codes observed from the live Envia.com API (see /ship/generate/ responses):
      1125 service not available/incorrect · 1126 invalid postcode/address
      1127 destination branch required · 1129 missing phone/customs data
      1170 not enough money (wallet balance) · 1220 postal code unresolved
      1300 external carrier error / account not enabled
    """
    code = getattr(exc, 'code', None)
    known = {
        1125: _("Envia.com: the selected shipping service is not available or is incorrect for this route. Check the delivery method / carrier service code."),
        1126: _("Envia.com: the destination postal code or address is invalid. Check the customer's postal code and address."),
        1127: _("Envia.com: a destination branch is required for a 'to branch' shipment. Set the branch on the customer's address and try again."),
        1129: _("Envia.com: missing shipment data — the recipient's phone number or the customs information. Complete it and try again."),
        1170: _("Envia.com: insufficient balance in the Envia.com account. Top up your Envia.com wallet and try again."),
        1220: _("Envia.com: the destination postal code could not be resolved. Check the customer's postal code and country."),
        1300: _("Envia.com: the carrier rejected the shipment (external carrier error or account not enabled). Retry; if it persists, contact Envia.com."),
    }
    if code in known:
        return known[code]
    raw = getattr(exc, 'message', None) or str(exc)
    if code:
        return _("Envia.com error %(code)s: %(msg)s", code=code, msg=raw)
    return _("Envia.com shipment error: %s") % raw


class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    delivery_type = fields.Selection(
        selection_add=[('envia', 'Envia.com')],
        ondelete={'envia': 'set default'},
    )

    envia_api_key_sandbox = fields.Char(
        string="Envia Sandbox API Key",
        groups="base.group_system",
    )
    envia_api_key_production = fields.Char(
        string="Envia Production API Key",
        groups="base.group_system",
    )
    envia_carrier_code = fields.Char(
        string="Envia Carrier Code",
        help="Carrier code from envia.com (e.g. 'dhl', 'fedex', 'estafeta')",
    )
    envia_carrier_name = fields.Char(
        string="Envia Carrier Name",
        readonly=True,
    )
    envia_service_code = fields.Char(
        string="Envia Service Code",
        help="Service code from envia.com (e.g. 'express', 'ground')",
    )
    envia_service_name = fields.Char(
        string="Envia Service Name",
        readonly=True,
    )
    envia_default_package_type_id = fields.Many2one(
        'stock.package.type',
        string="Default Package Type",
    )
    envia_duties_payment = fields.Selection(
        selection=[
            ('recipient', 'Recipient pays on delivery'),
            ('sender', 'Sender (billed afterwards)'),
            ('envia_guaranteed', 'Prepaid duties & taxes (DDP, guaranteed)'),
        ],
        string="Duties & Taxes Payment",
        default='recipient',
        help="Who pays customs duties and taxes on international shipments.\n"
             "- Recipient: the customer pays on delivery (default).\n"
             "- Sender: billed to your account afterwards.\n"
             "- Prepaid (DDP, guaranteed): duties & taxes are quoted and prepaid "
             "up front, so the customer receives the parcel with nothing to pay.\n"
             "Ignored for domestic shipments (no customs).",
    )
    envia_export_reason = fields.Selection(
        selection=[
            ('sale', 'Sale'),
            ('gift', 'Gift'),
            ('sample', 'Sample'),
            ('return', 'Return'),
            ('repair', 'Repair'),
            ('documents', 'Documents'),
            ('other', 'Other'),
        ],
        string="Export Reason",
        default='sale',
        help="Reason for export declared to customs on international shipments.",
    )
    envia_available_carriers = fields.Text(
        string="Available Carriers (JSON)",
        readonly=True,
    )
    envia_use_locations = fields.Boolean(
        string="Pickup points at checkout",
        compute='_compute_envia_use_locations', store=True, readonly=False,
        help="When the service delivers to a branch ('a sucursal'), the customer chooses the branch "
             "in the checkout from the Envia.com branches near their postal code (Odoo pickup points). "
             "Computed from the service code; can be overridden.",
    )
    envia_origin_address_id = fields.Integer(
        string="Envia Origin Address ID",
        help="Cached address_id for the warehouse origin in Envia.com address book.",
    )

    # ---------------------------
    # Helpers
    # ---------------------------

    @api.depends('envia_service_code', 'delivery_type')
    def _compute_envia_use_locations(self):
        for carrier in self:
            carrier.envia_use_locations = carrier.delivery_type == 'envia' and carrier._envia_service_needs_branch()

    def _envia_get_close_locations(self, partner_address, **kwargs):
        """Odoo 19 pickup points API: Envia.com branches near the customer's postal code."""
        self.ensure_one()
        country = partner_address.country_id.code
        zipc = (partner_address.zip or '').strip()
        if not country or not zipc or not self.envia_carrier_code:
            return []
        try:
            return self._get_envia_request().list_branches(country, zipc, self.envia_carrier_code)
        except Exception as e:  # noqa: BLE001 - el checkout muestra "sin puntos de retiro"
            _logger.warning("Envia pickup locations failed for %s %s: %s", country, zipc, e)
            return []

    @staticmethod
    def _envia_pickup_branch(order):
        """Branch chosen in the checkout (pickup_location_data), if any."""
        data = order.pickup_location_data if order else None
        return (data or {}).get('id') if isinstance(data, dict) else None

    def _get_envia_api_key(self):
        self.ensure_one()
        if self.prod_environment:
            return self.envia_api_key_production
        return self.envia_api_key_sandbox

    def _envia_service_needs_branch(self):
        """Whether the configured service delivers to a branch ('a sucursal'),
        so the destination address must carry a branchCode.

        Correo Argentino exposes 'standard_suc' (sucursal) vs 'standard_dom'
        (domicilio); other carriers follow the same '_suc' suffix convention.
        When False we must NOT inject a branchCode (it could misroute an
        'a domicilio' parcel to a pickup branch)."""
        self.ensure_one()
        code = (self.envia_service_code or '').strip().lower()
        return code.endswith('suc') or 'sucursal' in code

    def _envia_multi_package_problem(self, envia_packages):
        """Explain why this shipment cannot be sent, or ``None`` when it can.

        Odoo splits a shipment into as many boxes as the package type's
        ``max_weight`` requires, but some carriers reject a multi-box request
        outright (see ``MULTI_PACKAGE_UNSUPPORTED``) — envia answers those with
        an opaque 'Internal error' that says nothing about the real cause.

        We refuse the quote here instead of merging the boxes ourselves: the
        shipment that gets quoted and labelled must be the one that was
        actually packed, so the fix belongs in the package configuration.
        """
        self.ensure_one()
        if len(envia_packages or []) <= 1:
            return None
        if (self.envia_carrier_code or '') not in MULTI_PACKAGE_UNSUPPORTED:
            return None

        total_weight = sum(p.get('weight') or 0 for p in envia_packages)
        pkg_type = self.envia_default_package_type_id
        max_weight = pkg_type.max_weight or 0
        if pkg_type and max_weight > 0:
            return _(
                "The carrier %(carrier)s does not accept shipments split into "
                "several packages. This shipment weighs %(weight).2f kg and the "
                "packaging '%(package)s' takes up to %(max_weight).2f kg, so it "
                "was split into %(count)s packages. Raise the maximum weight of "
                "that package type (Inventory > Configuration > Package Types) "
                "or choose a bigger one, so the shipment fits in a single "
                "package.",
                carrier=self.envia_carrier_code, weight=total_weight,
                package=pkg_type.display_name, max_weight=max_weight,
                count=len(envia_packages))
        return _(
            "The carrier %(carrier)s does not accept shipments split into "
            "several packages, and this shipment (%(weight).2f kg) was split "
            "into %(count)s packages. Send it as a single package.",
            carrier=self.envia_carrier_code, weight=total_weight,
            count=len(envia_packages))

    def _get_envia_request(self):
        self.ensure_one()
        api_key = self._get_envia_api_key()
        if not api_key:
            raise UserError(_(
                "Please configure the Envia.com API key for the %s environment.",
                "production" if self.prod_environment else "sandbox",
            ))
        debug_logger = self.log_xml if self.debug_logging else None
        return EnviaRequest(api_key, self.prod_environment, debug_logger)

    def _envia_customs_settings(self, origin_partner, dest_partner):
        """Build the ``customsSettings`` payload for international shipments.

        Returns ``None`` for domestic routes (same country), so the request
        layer omits the field entirely and envia applies its own default.
        """
        self.ensure_one()
        if not origin_partner.country_id or not dest_partner.country_id:
            return None
        if origin_partner.country_id == dest_partner.country_id:
            return None
        return {
            'dutiesPaymentEntity': self.envia_duties_payment or 'recipient',
            'exportReason': self.envia_export_reason or 'sale',
        }

    def _envia_missing_customs_data(self, order):
        """Return a list of problems (log-only strings) that prevent Envia
        from classifying the goods and quoting prepaid duties (DDP)."""
        self.ensure_one()
        problems = []
        if not order:
            return ["no sale order available"]
        for line in order.order_line:
            if line.display_type or line.is_delivery or not line.product_id:
                continue
            if line.product_uom_qty <= 0:
                continue
            product = line.product_id
            if product.type == 'service':
                continue
            if not (product.hs_code or '').strip():
                problems.append("product '%s' has no HS Code" % product.display_name)
            if product.envia_insurance_value <= 0 and line.price_unit <= 0:
                problems.append(
                    "product '%s' has no declared value (no insurance value "
                    "and no sale price)" % product.display_name)
        return problems

    def _envia_prepare_customs_items(self, order, origin_country_code):
        """Build the per-item customs payload from the sale order lines.

        Envia needs description, unit value and HS code per item to classify
        the goods and quote prepaid duties & taxes (DDP / landed cost) —
        without items the rate response comes back with landedCostTotal null
        and importFee 0 even when dutiesPaymentEntity is 'envia_guaranteed'.

        Unit price priority: product insurance value (already in company
        currency) > sale line unit price converted to company currency, so
        all items are declared consistently in the company currency.
        """
        self.ensure_one()
        items = []
        if not order:
            return items
        company = order.company_id
        company_currency = company.currency_id
        conversion_date = order.date_order or fields.Date.today()
        for line in order.order_line:
            if line.display_type or line.is_delivery or not line.product_id:
                continue
            if line.product_uom_qty <= 0:
                continue
            product = line.product_id
            if product.type == 'service':
                continue
            if product.envia_insurance_value > 0:
                unit_price = product.envia_insurance_value
            else:
                unit_price = order.currency_id._convert(
                    line.price_unit, company_currency, company, conversion_date)
            if unit_price <= 0:
                continue
            item = {
                'description': (product.envia_export_description
                                or product.name or 'Product'),
                'quantity': max(1, int(line.product_uom_qty)),
                'price': round(unit_price, 2),
                'currency': company_currency.name,
                'countryOfManufacture': (
                    product.country_of_origin.code
                    if product.country_of_origin
                    else origin_country_code
                ),
            }
            if product.hs_code:
                hs_code = product.hs_code.strip()
                # Envia's documented item field is 'hsCode'; 'productCode' is
                # kept for backwards compatibility with the customs invoice.
                item['hsCode'] = hs_code
                item['productCode'] = hs_code
            items.append(item)
        return items

    def _envia_download_label(self, label_url):
        try:
            response = requests.get(label_url, timeout=30)
            response.raise_for_status()
            return base64.b64encode(response.content)
        except Exception as e:
            _logger.warning("Could not download Envia label from %s: %s", label_url, e)
            return False

    # ---------------------------
    # Button actions
    # ---------------------------

    def envia_action_fetch_carriers(self):
        self.ensure_one()
        request = self._get_envia_request()
        try:
            carriers = request.get_available_carriers()
        except Exception as e:
            raise UserError(_("Error fetching carriers: %s") % e)

        self.envia_available_carriers = json.dumps(carriers, indent=2, ensure_ascii=False)
        count = len(carriers) if isinstance(carriers, list) else 0
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Carriers Fetched"),
                'message': _("%d carriers retrieved from Envia.com") % count,
                'type': 'success',
                'sticky': False,
            },
        }

    # ---------------------------
    # delivery.carrier API methods
    # ---------------------------

    def envia_rate_shipment(self, order):
        self.ensure_one()
        try:
            if not self.envia_carrier_code:
                raise UserError(_("Please configure the Envia carrier code on this delivery method."))

            request = self._get_envia_request()

            warehouse_partner = order.warehouse_id.partner_id
            shipping_partner = order.partner_shipping_id

            origin = request._prepare_address(warehouse_partner)
            destination = request._prepare_address(
                shipping_partner, carrier_code=self.envia_carrier_code,
                needs_branch=self._envia_service_needs_branch(),
                branch_code=self._envia_pickup_branch(order))

            default_pkg_type = self.envia_default_package_type_id
            if not default_pkg_type:
                raise UserError(_("Please configure a default package type on this delivery method."))
            packages = self._get_packages_from_order(order, default_pkg_type)
            insurance_total = sum(
                line.product_id.envia_insurance_value * line.product_uom_qty
                for line in order.order_line
                if line.product_id and line.product_id.envia_insurance_value > 0
                and not line.display_type and not line.is_delivery
                and line.product_id.type != 'service'
            )
            envia_packages = EnviaRequest._prepare_packages(
                packages, declared_value_override=insurance_total)

            # Some carriers cannot quote a shipment split into several boxes.
            # Say so plainly instead of letting the API answer 'Internal error'.
            multi_package_problem = self._envia_multi_package_problem(envia_packages)
            if multi_package_problem:
                _logger.warning(
                    "Envia rate refused for order %s: %s",
                    order.name, multi_package_problem)
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': multi_package_problem,
                    'warning_message': False,
                }

            customs_settings = self._envia_customs_settings(warehouse_partner, shipping_partner)
            # Prepaid duties (DDP): if the customs data is incomplete Envia
            # quotes freight only, which would silently leave the duties on
            # our account. Refuse to offer this method instead.
            ddp_required = bool(customs_settings) and self.envia_duties_payment == 'envia_guaranteed'
            if customs_settings:
                # International route: add per-item customs data (HS code,
                # unit value) so Envia can quote prepaid duties (DDP).
                items = self._envia_prepare_customs_items(
                    order, warehouse_partner.country_id.code or 'AR')
                if items:
                    for pkg in envia_packages:
                        pkg['items'] = items
                if ddp_required:
                    problems = self._envia_missing_customs_data(order)
                    if not items:
                        problems.append("no customs items could be built from the order")
                    if problems:
                        _logger.warning(
                            "Envia DDP rate refused for order %s: %s",
                            order.name, "; ".join(problems))
                        return {
                            'success': False,
                            'price': 0.0,
                            'error_message': _("Not available: the customs "
                                               "information for this order is "
                                               "incomplete."),
                            'warning_message': False,
                        }

            rates = request.rate(origin, destination, envia_packages,
                                 self.envia_carrier_code, self.envia_service_code,
                                 customs_settings=customs_settings)

            if not rates:
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': _("No rates available for this route."),
                    'warning_message': False,
                }

            rate = rates[0]
            price = float(rate.get('totalPrice') or rate.get('basePrice') or 0)
            api_currency_code = rate.get('currency', 'USD')

            if ddp_required:
                # Per Envia docs landedCostTotal (duties + taxes, included in
                # totalPrice) must be present on a DDP quote. If it is null/0
                # the price is freight only: selling it would force us to pay
                # the customer's duties afterwards. Mark as unavailable — the
                # non-DDP method remains selectable.
                landed_total = float(rate.get('landedCostTotal') or 0)
                if landed_total <= 0:
                    _logger.warning(
                        "Envia DDP rate refused for order %s: response has no "
                        "landed cost (landedCostTotal=%s, importFee=%s, "
                        "classifiedHsCodes=%s); the quoted price %.2f %s is "
                        "freight only",
                        order.name, rate.get('landedCostTotal'),
                        rate.get('importFee'), rate.get('classifiedHsCodes'),
                        price, api_currency_code)
                    return {
                        'success': False,
                        'price': 0.0,
                        'error_message': _("Not available: duties & taxes "
                                           "could not be quoted for this "
                                           "destination."),
                        'warning_message': False,
                    }

            _logger.info(
                "Envia rate for order %s: raw_price=%.2f %s, order_currency=%s, "
                "importFee=%s, landedCostTotal=%s, classifiedHsCodes=%s",
                order.name, price, api_currency_code, order.currency_id.name,
                rate.get('importFee'), rate.get('landedCostTotal'),
                rate.get('classifiedHsCodes'))

            # Convert from API currency to order currency
            order_currency = order.currency_id
            if order_currency.name != api_currency_code:
                api_currency = self.env['res.currency'].search(
                    [('name', '=', api_currency_code)], limit=1)
                if api_currency:
                    try:
                        converted = api_currency._convert(
                            price, order_currency, order.company_id,
                            order.date_order or fields.Date.today())
                        _logger.info(
                            "Envia rate for order %s: converted %.2f %s -> %.2f %s",
                            order.name, price, api_currency_code, converted, order_currency.name)
                        price = converted
                    except Exception as e:
                        _logger.warning(
                            "Envia currency conversion failed for order %s (%s->%s): %s",
                            order.name, api_currency_code, order_currency.name, e)
                        return {
                            'success': False,
                            'price': 0.0,
                            'error_message': _("Currency conversion error (%s to %s). Please check exchange rates.") % (
                                api_currency_code, order_currency.name),
                            'warning_message': False,
                        }
                else:
                    _logger.warning(
                        "Envia rate: currency %s not found in Odoo for order %s",
                        api_currency_code, order.name)
                    return {
                        'success': False,
                        'price': 0.0,
                        'error_message': _("Currency %s not configured in Odoo. Please add it and set an exchange rate.") % api_currency_code,
                        'warning_message': False,
                    }

            if price <= 0:
                _logger.warning("Envia rate for order %s: price is 0 after conversion", order.name)
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': _("Could not calculate shipping cost for this route."),
                    'warning_message': False,
                }

            carrier_name = rate.get('carrier', self.envia_carrier_code)
            service_name = rate.get('service', self.envia_service_code or '')
            estimate = rate.get('deliveryEstimate', {})
            if isinstance(estimate, dict):
                delivery_days = estimate.get('transitDays') or estimate.get('days') or ''
            else:
                delivery_days = str(estimate)

            warning = ''
            if delivery_days:
                warning = _("Estimated delivery: %s (%s %s)") % (delivery_days, carrier_name, service_name)

            return {
                'success': True,
                'price': price,
                'error_message': False,
                'warning_message': warning or False,
            }

        except UserError:
            raise
        except Exception as e:
            _logger.warning("Envia rate_shipment error for order %s: %s", order.name, e)
            return {
                'success': False,
                'price': 0.0,
                'error_message': _user_friendly_rate_error(e),
                'warning_message': False,
            }

    def envia_send_shipping(self, pickings):
        res = []
        for picking in pickings:
            request = self._get_envia_request()

            warehouse_partner = picking.picking_type_id.warehouse_id.partner_id
            shipping_partner = picking.partner_id

            needs_branch = self._envia_service_needs_branch()
            origin = request._prepare_address(warehouse_partner)
            destination = request._prepare_address(
                shipping_partner, carrier_code=self.envia_carrier_code,
                needs_branch=needs_branch,
                branch_code=self._envia_pickup_branch(picking.sale_id))

            # 'A sucursal' services need a destination branch. If we could not
            # resolve one (and the partner has no manual override), envia would
            # reject /ship/generate/ with error 1127. Fail early with a clear,
            # actionable message instead — it contains 'Envia.com' so the
            # button_validate wizard catches it and the validation is rolled
            # back, leaving the picking ready to retry once the branch is set.
            if needs_branch and not destination.get('branchCode'):
                raise UserError(_(
                    "Envia.com: this delivery method delivers to a branch "
                    "('a sucursal') but no branch could be found for the "
                    "destination postal code %(zip)s. Set the branch manually "
                    "on the customer's address (the 'Envia Branch Code' field) "
                    "and try again.",
                    zip=shipping_partner.zip or '-'))

            # Declared value priority: picking override > product insurance > package cost
            envia_packages = picking._envia_prepare_packages()
            picking._envia_validate_packages(envia_packages)

            # Same check as in the quote: refuse a multi-box shipment the
            # carrier cannot take. The message carries the literal 'Envia.com'
            # so button_validate rolls the validation back and the picking
            # stays ready to retry once the packaging is fixed.
            multi_package_problem = self._envia_multi_package_problem(envia_packages)
            if multi_package_problem:
                raise UserError(_("Envia.com: %s") % multi_package_problem)

            # Add customs items only for international shipments
            is_international = warehouse_partner.country_id != shipping_partner.country_id
            if is_international:
                items = self._envia_prepare_customs_items(
                    picking.sale_id, warehouse_partner.country_id.code or 'AR')
                if items:
                    for pkg in envia_packages:
                        pkg['items'] = items

            customs_settings = self._envia_customs_settings(warehouse_partner, shipping_partner)

            try:
                result = request.generate_shipment(
                    origin, destination, envia_packages,
                    self.envia_carrier_code, self.envia_service_code,
                    customs_settings=customs_settings,
                )
            except EnviaApiError as e:
                raise UserError(_envia_shipment_error_message(e))
            except Exception as e:
                raise UserError(_("Envia.com shipment error: %s") % e)

            tracking_number = result.get('trackingNumber') or result.get('data', [{}])[0].get('trackingNumber') or ''
            label_url = result.get('label') or result.get('data', [{}])[0].get('label') or ''
            total_price = float(result.get('totalPrice') or result.get('data', [{}])[0].get('totalPrice') or 0)

            # Convert price from API currency to order currency
            sale_order = picking.sale_id
            if sale_order and total_price:
                api_currency_code = result.get('currency') or result.get('data', [{}])[0].get('currency', 'USD')
                order_currency = sale_order.currency_id
                if order_currency.name != api_currency_code:
                    api_currency = self.env['res.currency'].search(
                        [('name', '=', api_currency_code)], limit=1)
                    if api_currency:
                        total_price = api_currency._convert(
                            total_price, order_currency, sale_order.company_id,
                            fields.Date.today())

            # Download and attach label
            if label_url:
                label_data = self._envia_download_label(label_url)
                if label_data:
                    # Determine file extension from URL
                    ext = '.pdf'
                    if label_url.lower().endswith('.png'):
                        ext = '.png'
                    elif label_url.lower().endswith('.zpl'):
                        ext = '.zpl'

                    attachment = self.env['ir.attachment'].create({
                        'name': f"LabelEnvia-{tracking_number}{ext}",
                        'type': 'binary',
                        'datas': label_data,
                        'res_model': 'stock.picking',
                        'res_id': picking.id,
                    })
                    picking.message_post(
                        body=_("Envia.com shipping label for tracking: %s") % tracking_number,
                        attachment_ids=[attachment.id],
                    )

            logmessage = _("Shipment created via Envia.com<br/>Carrier: %s<br/>Tracking: %s") % (
                self.envia_carrier_code, tracking_number,
            )
            picking.message_post(body=logmessage)

            res.append({
                'exact_price': total_price,
                'tracking_number': tracking_number,
            })
        return res

    def envia_get_tracking_link(self, picking):
        ref = picking.carrier_tracking_ref
        if not ref:
            return False
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f"{base_url}/track-envia/{ref}"

    def envia_cancel_shipment(self, pickings):
        for picking in pickings:
            tracking_ref = picking.carrier_tracking_ref
            if not tracking_ref:
                continue
            request = self._get_envia_request()
            try:
                request.cancel(self.envia_carrier_code, tracking_ref)
            except EnviaApiError as e:
                raise UserError(_envia_shipment_error_message(e))
            except Exception as e:
                raise UserError(_("Envia.com cancellation error: %s") % e)
