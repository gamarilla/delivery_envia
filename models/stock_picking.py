import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Envia.com package limits for the configured services
ENVIA_WEIGHT_MIN_KG = 0.01
ENVIA_WEIGHT_MAX_KG = 10.0
ENVIA_DIM_MIN_CM = 1.0
ENVIA_DIM_MAX_CM = 100.0


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """Sync addresses to Envia address book before validation.
        On shipment generation error, show wizard with retry/skip/cancel options.
        """
        for picking in self:
            _logger.info("button_validate override called for picking %s, carrier=%s, delivery_type=%s",
                         picking.name,
                         picking.carrier_id.name if picking.carrier_id else 'None',
                         picking.carrier_id.delivery_type if picking.carrier_id else 'None')
            if (picking.carrier_id
                    and picking.carrier_id.delivery_type == 'envia'
                    and picking.picking_type_code == 'outgoing'):
                picking._envia_sync_addresses()

        # If skipping shipping (from wizard "Validate without label")
        if self.env.context.get('envia_skip_shipping'):
            return super(StockPicking, self.with_context(
                cancel_backorder=True)).button_validate()

        # Wrap the validation in a savepoint. The standard delivery flow calls
        # send_to_shipper() at the very end of _action_done(), i.e. AFTER the
        # picking has already been marked done and the stock moved. If the
        # Envia label fails there and we merely catch the UserError to show the
        # wizard, the exception is swallowed and the cursor is NOT rolled back:
        # the picking ends up validated/closed with no label or tracking.
        # The savepoint rolls the whole validation back (and clears the ORM
        # cache via cr.clear()) so a failed shipment leaves the picking
        # untouched, ready to retry from the wizard.
        try:
            with self.env.cr.savepoint():
                return super().button_validate()
        except UserError as e:
            # Only intercept Envia errors for envia carriers
            error_msg = str(e)
            envia_pickings = self.filtered(
                lambda p: p.carrier_id and p.carrier_id.delivery_type == 'envia'
                and p.picking_type_code == 'outgoing')
            if envia_pickings and 'Envia.com' in error_msg:
                wizard = self.env['envia.shipment.error.wizard'].create({
                    'picking_id': envia_pickings[0].id,
                    'error_message': error_msg,
                })
                return {
                    'type': 'ir.actions.act_window',
                    'name': 'Envia.com Shipment Error',
                    'res_model': 'envia.shipment.error.wizard',
                    'res_id': wizard.id,
                    'view_mode': 'form',
                    'target': 'new',
                }
            raise

    def _envia_sync_addresses(self):
        """Sync origin and destination addresses to Envia.com address book."""
        self.ensure_one()
        carrier = self.carrier_id
        try:
            from .envia_request import EnviaRequest
            api_key = carrier._get_envia_api_key()
            request = EnviaRequest(api_key, carrier.prod_environment)

            # Sync destination
            shipping_partner = self.partner_id
            dest_address_id = request.sync_address(shipping_partner, address_type=2)
            if dest_address_id and dest_address_id != shipping_partner.envia_address_id:
                shipping_partner.sudo().write({'envia_address_id': dest_address_id})

            # Sync origin (cached on carrier)
            if not carrier.envia_origin_address_id:
                warehouse_partner = self.picking_type_id.warehouse_id.partner_id
                origin_address_id = request.sync_address(warehouse_partner, address_type=1)
                if origin_address_id:
                    carrier.sudo().write({'envia_origin_address_id': origin_address_id})
                    warehouse_partner.sudo().write({'envia_address_id': origin_address_id})

        except Exception as e:
            _logger.warning("Envia address sync failed for picking %s: %s", self.name, e)

    def action_envia_sync_address(self):
        """Manual button to sync destination address to Envia.com address book."""
        self.ensure_one()
        if not self.carrier_id or self.carrier_id.delivery_type != 'envia':
            raise UserError(_("This picking does not use an Envia.com carrier."))

        self._envia_sync_addresses()

        # Check result
        partner = self.partner_id
        if partner.envia_address_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Address Synced"),
                    'message': _("Destination address for %s synced to Envia.com (ID: %s)") % (
                        partner.name, partner.envia_address_id),
                    'type': 'success',
                    'sticky': False,
                },
            }
        else:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Sync Failed"),
                    'message': _("Could not sync address to Envia.com. Check the server log for details."),
                    'type': 'warning',
                    'sticky': True,
                },
            }

    envia_declared_value = fields.Float(
        string="Envia Declared Value",
        help="Override declared value for customs/insurance in company currency. "
             "Leave at 0 to use the product's insurance value or the package cost. "
             "Priority: this override > product insurance value > package calculated cost.",
    )
    envia_shipping_weight = fields.Float(
        string="Envia Shipping Weight (kg)",
        help="Override shipping weight. Leave at 0 to use the calculated weight from products.",
    )
    envia_package_length = fields.Float(
        string="Envia Package Length (cm)",
        help="Override package length. Leave at 0 to use the default package type dimensions.",
    )
    envia_package_width = fields.Float(
        string="Envia Package Width (cm)",
        help="Override package width. Leave at 0 to use the default package type dimensions.",
    )
    envia_package_height = fields.Float(
        string="Envia Package Height (cm)",
        help="Override package height. Leave at 0 to use the default package type dimensions.",
    )

    envia_planned_declared_value = fields.Float(
        string="Declared Value To Send",
        compute='_compute_envia_planned_values',
        help="Value that will actually be sent to Envia.com "
             "(override > product insurance value > package cost).",
    )
    envia_planned_weight = fields.Float(
        string="Weight To Send (kg)",
        compute='_compute_envia_planned_values',
        help="Weight that will actually be sent to Envia.com "
             "(override > calculated weight from products).",
    )
    envia_planned_length = fields.Float(
        string="Length To Send (cm)",
        compute='_compute_envia_planned_values',
        help="Length that will actually be sent to Envia.com "
             "(override > default package type dimensions).",
    )
    envia_planned_width = fields.Float(
        string="Width To Send (cm)",
        compute='_compute_envia_planned_values',
        help="Width that will actually be sent to Envia.com "
             "(override > default package type dimensions).",
    )
    envia_planned_height = fields.Float(
        string="Height To Send (cm)",
        compute='_compute_envia_planned_values',
        help="Height that will actually be sent to Envia.com "
             "(override > default package type dimensions).",
    )

    @api.depends('carrier_id', 'envia_declared_value', 'envia_shipping_weight',
                 'envia_package_length', 'envia_package_width', 'envia_package_height',
                 'move_line_ids.quantity', 'move_line_ids.result_package_id', 'sale_id')
    def _compute_envia_planned_values(self):
        for picking in self:
            weight = picking.envia_shipping_weight
            length = picking.envia_package_length
            width = picking.envia_package_width
            height = picking.envia_package_height
            declared = picking.envia_declared_value
            carrier = picking.carrier_id
            if carrier and carrier.delivery_type == 'envia':
                pkgs = None
                try:
                    pkgs = picking._envia_prepare_packages()
                except Exception:
                    pass
                if pkgs:
                    first = pkgs[0]
                    dims = first.get('dimensions') or {}
                    weight = first.get('weight') or 0.0
                    length = dims.get('length') or 0.0
                    width = dims.get('width') or 0.0
                    height = dims.get('height') or 0.0
                    declared = first.get('declaredValue') or 0.0
                else:
                    # Packages could not be built yet (e.g. zero weight):
                    # show override > raw data so the user sees what is missing.
                    declared = picking._envia_get_declared_value()
                    weight = weight or picking.shipping_weight or picking.weight
                    pkg_type = carrier.envia_default_package_type_id
                    if pkg_type:
                        # stock.package.type stores dimensions in mm
                        length = length or pkg_type.packaging_length / 10
                        width = width or pkg_type.width / 10
                        height = height or pkg_type.height / 10
            picking.envia_planned_weight = weight
            picking.envia_planned_length = length
            picking.envia_planned_width = width
            picking.envia_planned_height = height
            picking.envia_planned_declared_value = declared

    def _envia_get_declared_value(self):
        """Declared value priority: picking override > product insurance values.
        Returns 0 when neither is set (the package calculated cost is then
        used as last fallback inside EnviaRequest._prepare_packages)."""
        self.ensure_one()
        if self.envia_declared_value:
            return self.envia_declared_value
        sale_order = self.sale_id
        if sale_order:
            insurance_total = sum(
                line.product_id.envia_insurance_value * line.product_uom_qty
                for line in sale_order.order_line
                if line.product_id and line.product_id.envia_insurance_value > 0
                and not line.display_type and not line.is_delivery
                and line.product_id.type != 'service'
            )
            if insurance_total > 0:
                return insurance_total
        return 0.0

    def _envia_prepare_packages(self):
        """Build the package payloads exactly as they will be sent to Envia.com,
        combining real data with the picking overrides."""
        self.ensure_one()
        from .envia_request import EnviaRequest
        carrier = self.carrier_id
        default_pkg_type = carrier.envia_default_package_type_id
        if not default_pkg_type:
            raise UserError(_("Please configure a default package type on this delivery method."))
        try:
            packages = carrier._get_packages_from_picking(self, default_pkg_type)
        except UserError:
            if self.envia_shipping_weight > 0:
                # Products have no weight but the override covers it: build a
                # single package from the default package type.
                from odoo.addons.stock_delivery.models.delivery_request_objects import DeliveryPackage
                packages = [DeliveryPackage(
                    [], self.envia_shipping_weight, default_pkg_type,
                    name=self.name, total_cost=0,
                    currency=self.company_id.currency_id, picking=self,
                )]
            else:
                raise UserError(_(
                    "Envia.com: the shipping weight is 0 kg. Fill in the product "
                    "weights or the 'Envia Shipping Weight (kg)' override."))
        return EnviaRequest._prepare_packages(
            packages,
            weight_override=self.envia_shipping_weight,
            length_override=self.envia_package_length,
            width_override=self.envia_package_width,
            height_override=self.envia_package_height,
            declared_value_override=self._envia_get_declared_value(),
        )

    def _envia_validate_packages(self, envia_packages):
        """Validate the final package values before calling Envia.com.
        Raises a UserError listing every missing or out-of-range value."""
        self.ensure_one()
        errors = []
        for idx, pkg in enumerate(envia_packages, start=1):
            prefix = _("Package %s: ") % idx if len(envia_packages) > 1 else ""
            weight = pkg.get('weight') or 0.0
            if weight <= 0:
                errors.append(prefix + _(
                    "the weight is 0 kg. Fill in the product weights or the "
                    "'Envia Shipping Weight (kg)' override."))
            elif not (ENVIA_WEIGHT_MIN_KG <= weight <= ENVIA_WEIGHT_MAX_KG):
                errors.append(prefix + _(
                    "the weight (%(weight).2f kg) is outside the range allowed by "
                    "Envia.com (%(min).2f to %(max).2f kg).") % {
                        'weight': weight,
                        'min': ENVIA_WEIGHT_MIN_KG,
                        'max': ENVIA_WEIGHT_MAX_KG})
            dims = pkg.get('dimensions') or {}
            dim_labels = [
                ('length', _("length"), _("'Envia Package Length (cm)'")),
                ('width', _("width"), _("'Envia Package Width (cm)'")),
                ('height', _("height"), _("'Envia Package Height (cm)'")),
            ]
            for key, label, override_label in dim_labels:
                value = dims.get(key) or 0.0
                if value <= 0:
                    errors.append(prefix + _(
                        "the package %(dim)s is 0 cm. Fill in the package type "
                        "dimensions or the %(override)s override.") % {
                            'dim': label, 'override': override_label})
                elif not (ENVIA_DIM_MIN_CM <= value <= ENVIA_DIM_MAX_CM):
                    errors.append(prefix + _(
                        "the package %(dim)s (%(value).1f cm) is outside the range "
                        "allowed by Envia.com (%(min).0f to %(max).0f cm).") % {
                            'dim': label, 'value': value,
                            'min': ENVIA_DIM_MIN_CM, 'max': ENVIA_DIM_MAX_CM})
            declared = pkg.get('declaredValue') or 0.0
            if declared <= 0:
                errors.append(prefix + _(
                    "the declared value is 0. Fill in the 'Envia Declared Value' "
                    "override or the product's insurance value."))
        if errors:
            raise UserError(
                _("Envia.com: the shipment cannot be generated. Please complete "
                  "the following data:") + "\n- " + "\n- ".join(errors))
