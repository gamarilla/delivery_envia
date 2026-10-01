# -*- coding: utf-8 -*-

import logging
from odoo import http
from odoo.http import request
from ..models.envia_request import EnviaRequest

_logger = logging.getLogger(__name__)

STATUS_LABELS = {
    'Created': 'Creado',
    'In Transit': 'En tránsito',
    'Out for Delivery': 'En reparto',
    'Delivered': 'Entregado',
    'Exception': 'Incidencia',
    'Cancelled': 'Cancelado',
}


class EnviaTrackingController(http.Controller):

    def _find_picking(self, ref):
        """Find outgoing picking by tracking ref for an envia carrier."""
        return request.env['stock.picking'].sudo().search([
            ('carrier_tracking_ref', '=', ref),
            ('picking_type_code', '=', 'outgoing'),
            ('carrier_id.delivery_type', '=', 'envia'),
        ], limit=1, order='id desc')

    def _fetch_envia_tracking(self, picking, ref):
        """Call Envia API and return parsed tracking data, or None on error."""
        carrier = picking.carrier_id
        api_key = carrier.envia_api_key_production if carrier.prod_environment else carrier.envia_api_key_sandbox
        if not api_key:
            return None

        try:
            envia = EnviaRequest(api_key, carrier.prod_environment)
            result = envia.track(carrier.envia_carrier_code, ref)
        except Exception as e:
            _logger.warning("Envia tracking API error for %s: %s", ref, e)
            return None

        if isinstance(result, dict) and result.get('meta') == 'error':
            return None

        data = result.get('data', [])
        if not data:
            return None

        shipment = data[0]
        events = []
        for ev in shipment.get('eventHistory', []):
            events.append({
                'date': ev.get('date') or ev.get('datetime') or ev.get('timestamp', ''),
                'description': ev.get('description') or ev.get('status') or ev.get('message', ''),
                'location': ev.get('location') or ev.get('city', ''),
            })

        status_raw = shipment.get('status', '')
        return {
            'status': status_raw,
            'status_label': STATUS_LABELS.get(status_raw, status_raw),
            'status_color': shipment.get('statusColor', ''),
            'carrier_description': shipment.get('carrierDescription', ''),
            'service_description': shipment.get('serviceDescription', ''),
            'estimated_delivery': shipment.get('estimatedDelivery', ''),
            'pickup_date': shipment.get('pickupDate', ''),
            'shipped_at': shipment.get('shippedAt', ''),
            'delivered_at': shipment.get('deliveredAt', ''),
            'signed_by': shipment.get('signedBy', ''),
            'track_url_site': shipment.get('trackUrlSite', ''),
            'events': events,
        }

    @http.route('/track-envia', type='http', auth='public', website=True)
    def tracking_form(self, **kwargs):
        return request.render('delivery_envia.tracking_form_page', {})

    @http.route('/track-envia/<string:ref>', type='http', auth='public', website=True)
    def tracking_result(self, ref, **kwargs):
        picking = self._find_picking(ref)
        if not picking:
            return request.render('delivery_envia.tracking_not_found', {
                'tracking_ref': ref,
            })

        products = []
        for move in picking.move_ids:
            if move.product_id:
                products.append({
                    'name': move.product_id.name,
                    'qty': move.quantity,
                })

        # Fetch live tracking from Envia API
        envia_data = self._fetch_envia_tracking(picking, ref)

        values = {
            'tracking_ref': ref,
            'carrier_name': picking.carrier_id.name or '',
            'picking_state': picking.state,
            'is_done': picking.state == 'done',
            'has_tracking_ref': bool(picking.carrier_tracking_ref),
            'date_done': picking.date_done,
            'scheduled_date': picking.scheduled_date,
            'city': picking.partner_id.city or '',
            'state_name': picking.partner_id.state_id.name if picking.partner_id.state_id else '',
            'country_name': picking.partner_id.country_id.name if picking.partner_id.country_id else '',
            'products': products,
            'envia': envia_data,
        }
        return request.render('delivery_envia.tracking_result_page', values)
