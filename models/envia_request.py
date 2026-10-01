import json
import logging
import re
import time

import requests

_logger = logging.getLogger(__name__)

SHIPPING_API_PROD = "https://api.envia.com"
SHIPPING_API_TEST = "https://api-test.envia.com"
QUERIES_API_PROD = "https://queries.envia.com"
QUERIES_API_TEST = "https://queries-test.envia.com"
GEOCODES_API_URL = "https://geocodes.envia.com"  # Always production (sandbox is down)

# Module-level cache for queries.envia.com/branches/<country>. The list is
# ~300 entries / ~100 KB and changes rarely, so a 24h TTL is fine and avoids
# slowing down every shipment with an extra HTTP roundtrip.
_BRANCHES_CACHE = {}  # {(env, country_code): (fetched_at_epoch, payload)}
_BRANCHES_TTL_SECONDS = 24 * 3600

# Caches for postal code validation (both endpoints work without auth).
_ZIP_RULES_CACHE = {}  # {country_code: (fetched_at_epoch, rules_dict_or_None)}
_ZIP_RULES_TTL_SECONDS = 24 * 3600
_ZIP_LOOKUP_CACHE = {}  # {(country_code, zip): (fetched_at_epoch, exists_bool)}
_ZIP_LOOKUP_TTL_SECONDS = 6 * 3600
_ZIP_LOOKUP_CACHE_MAX = 5000

# Carriers whose API cannot handle a multi-entry ``packages`` array: envia
# answers every such request with a generic, useless
# {"code": 400, "message": "Internal error"} body (served with HTTP 200).
# Verified against api.envia.com on 2026-08-03: dhl/int_express fails with two
# or more packages on any route tried (AR->CO, AR->PY), with or without customs
# items and however the items are split, while the very same payload collapsed
# into a single package quotes normally. correoArgentino and andreani accept
# multi-package payloads without trouble, so this is carrier-specific, not a
# platform-wide limit.
#
# We do NOT merge the boxes automatically: silently reshaping a shipment would
# quote and label a parcel nobody packed. Callers check this set up front and
# refuse the quote with an explanatory error instead (see
# ``delivery.carrier._envia_multi_package_problem``).
MULTI_PACKAGE_UNSUPPORTED = {'dhl'}


class EnviaApiError(Exception):
    """Structured error raised when the Envia.com API returns ``meta: error``.

    Carries the numeric ``code`` so the caller can map it to a clear, translated
    message. ``str(self)`` is always a clean one-line summary, never the raw
    ``{'code': ..., 'message': ...}`` dict that the API returns.
    """

    def __init__(self, code=None, message=None, description=None):
        self.code = code
        self.message = message
        self.description = description
        summary = message or description or 'Unknown error'
        super().__init__("[%s] %s" % (code, summary) if code else summary)


def _raise_envia_error(result, default='Envia.com request failed'):
    """Parse an Envia error payload and raise :class:`EnviaApiError`.

    Handles both the nested ``{"error": {"code", "message", "description"}}``
    form and a flat top-level ``message``.
    """
    error_info = result.get('error') if isinstance(result, dict) else None
    code = message = description = None
    if isinstance(error_info, dict):
        code = error_info.get('code')
        message = error_info.get('message')
        description = error_info.get('description')
    elif isinstance(error_info, str):
        message = error_info
    if isinstance(result, dict):
        # Flat error body: {"code": 400, "message": ..., "description": ...},
        # which envia serves with HTTP 200 and no ``meta``/``error`` wrapper.
        if not message:
            message = result.get('message')
        if code is None:
            code = result.get('code')
        if not description:
            description = result.get('description')
    raise EnviaApiError(code=code, message=message or default, description=description)


def _is_error_payload(result):
    """Whether an Envia response body is an error.

    Envia signals failures three different ways: ``meta: "error"``, a nested
    ``error`` object, or — for internal failures — a flat
    ``{"code": 400, "message": "Internal error"}`` body served with **HTTP
    200**, which ``raise_for_status`` cannot catch. Without the last check a
    failed quote is read as a valid rate and silently becomes a price of 0.
    """
    if not isinstance(result, dict):
        return False
    if result.get('meta') == 'error' or result.get('error'):
        return True
    code = result.get('code')
    try:
        return int(code) >= 400
    except (TypeError, ValueError):
        return False


def _get_zip_rules(country_code):
    """Fetch Envia's address-form rules for the postal code of a country.

    Returns a dict {'visible', 'required', 'max', 'regex'} or None when the
    rules could not be fetched / the country has no postalCode field
    (callers must fail open on None). Cached 24h per country.
    """
    now = time.time()
    cached = _ZIP_RULES_CACHE.get(country_code)
    if cached and (now - cached[0]) < _ZIP_RULES_TTL_SECONDS:
        return cached[1]
    try:
        resp = requests.get(
            f"{QUERIES_API_PROD}/generic-form",
            params={'country_code': country_code, 'form': 'address_info'},
            timeout=6)
        resp.raise_for_status()
        fields_def = resp.json()
    except Exception as e:
        _logger.warning("Envia zip-rules fetch failed for %s: %s", country_code, e)
        # Cache the failure briefly so checkout is not slowed down repeatedly.
        _ZIP_RULES_CACHE[country_code] = (now - _ZIP_RULES_TTL_SECONDS + 300, None)
        return None

    rules = None
    if isinstance(fields_def, list):
        for f in fields_def:
            if isinstance(f, dict) and f.get('fieldId') == 'postalCode':
                regex = None
                for oc in (f.get('on_change') or []):
                    cond = oc.get('condition') if isinstance(oc, dict) else None
                    if cond and isinstance(cond, str):
                        regex = cond.strip()
                        if regex.startswith('/') and regex.endswith('/'):
                            regex = regex[1:-1]
                        break
                rules = {
                    'visible': bool(f.get('visible')),
                    'required': bool((f.get('rules') or {}).get('required')),
                    'max': (f.get('rules') or {}).get('max'),
                    'regex': regex,
                }
                break
    _ZIP_RULES_CACHE[country_code] = (now, rules)
    return rules


_TAX_ID_RULES_CACHE = {}  # {country_code: (fetched_at_epoch, rules_or_None)}


def get_tax_id_rules(country_code):
    """Reglas del número de identificación fiscal del destinatario según Envia (generic-form público,
    campo ``identificationNumber``): {'visible', 'required', 'max', 'label_key'} o None si no se pudo
    consultar (los llamadores deben fallar abierto). Hoy Envia solo lo exige para Brasil (CPF/CNPJ).
    ``label_key`` es la clave de traducción interna de Envia (p. ej. 'createLabel.addressInfo.cnpj'):
    sirve de pista, no como nombre a mostrar. Cache 24 h por país."""
    country_code = (country_code or '').strip().upper()
    if not country_code:
        return None
    now = time.time()
    cached = _TAX_ID_RULES_CACHE.get(country_code)
    if cached and (now - cached[0]) < _ZIP_RULES_TTL_SECONDS:
        return cached[1]
    try:
        resp = requests.get(
            f"{QUERIES_API_PROD}/generic-form",
            params={'country_code': country_code, 'form': 'address_info'},
            timeout=6)
        resp.raise_for_status()
        fields_def = resp.json()
    except Exception as e:
        _logger.warning("Envia tax-id rules fetch failed for %s: %s", country_code, e)
        _TAX_ID_RULES_CACHE[country_code] = (now - _ZIP_RULES_TTL_SECONDS + 300, None)
        return None
    rules = None
    for f in fields_def if isinstance(fields_def, list) else []:
        if isinstance(f, dict) and f.get('fieldId') == 'identificationNumber':
            rules = {
                'visible': bool(f.get('visible')),
                'required': bool((f.get('rules') or {}).get('required')),
                'max': (f.get('rules') or {}).get('max'),
                'label_key': f.get('fieldLabelLang'),
            }
            break
    _TAX_ID_RULES_CACHE[country_code] = (now, rules)
    return rules


def _zip_exists(country_code, postal_code):
    """Check a postal code against the Envia geocodes API.

    Returns True/False, or None when the API is unreachable (fail open).
    The API normalizes formats itself (e.g. AR 'X5000ABC' resolves to 5000)
    and returns an empty list for unknown codes. Cached 6h per code.
    """
    key = (country_code, postal_code)
    now = time.time()
    cached = _ZIP_LOOKUP_CACHE.get(key)
    if cached and (now - cached[0]) < _ZIP_LOOKUP_TTL_SECONDS:
        return cached[1]
    try:
        resp = requests.get(
            f"{GEOCODES_API_URL}/zipcode/{country_code}/{requests.utils.quote(postal_code, safe='')}",
            timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _logger.warning("Envia zip lookup failed for %s/%s: %s", country_code, postal_code, e)
        return None
    exists = isinstance(data, list) and len(data) > 0
    if len(_ZIP_LOOKUP_CACHE) >= _ZIP_LOOKUP_CACHE_MAX:
        _ZIP_LOOKUP_CACHE.clear()
    _ZIP_LOOKUP_CACHE[key] = (now, exists)
    return exists


def _zip_regex_example(pattern):
    """Build a human-readable example from Envia's simple zip regexes.

    e.g. '(^[0-9]{5})$' -> '99999'; '(^[0-9]{5}[\\-][0-9]{3})|(^[0-9]{8})$'
    -> '99999-999 / 99999999'. Returns None when the pattern uses constructs
    this converter does not understand.
    """
    if not pattern:
        return None
    examples = []
    for branch in pattern.split('|'):
        s = branch
        s = re.sub(r'\[0-9\]\{(\d+)\}', lambda m: '9' * int(m.group(1)), s)
        s = s.replace('[0-9]', '9')
        s = re.sub(r'\\d\{(\d+)\}', lambda m: '9' * int(m.group(1)), s)
        s = s.replace('\\d', '9')
        s = re.sub(r'\[A-Z(?:a-z)?\]\{(\d+)\}', lambda m: 'A' * int(m.group(1)), s)
        s = re.sub(r'\[A-Z(?:a-z)?\]', 'A', s)
        s = re.sub(r'\[\\?\-\]', '-', s)
        s = re.sub(r'[\^\$\(\)\/]', '', s)
        s = s.replace('\\', '')
        if s and re.fullmatch(r'[A-Z0-9\- ]+', s):
            examples.append(s)
        else:
            return None
    return ' / '.join(examples) or None


def validate_postal_code(country_code, postal_code):
    """Validate a postal code against Envia.com public APIs (no auth needed).

    Returns a tuple (valid, reason, format_hint):
    - (True, None, None): valid, or validation impossible (API down, country
      without zip requirement) — callers must treat this as "do not block".
    - (False, 'format', '99999'): does not match the country's format regex.
    - (False, 'not_found', hint_or_None): the geocodes database knows the
      country but not this code.
    """
    # Sin ningún dígito no es un código postal en ningún país que use Envia (los CPA argentinos
    # llevan letras, pero siempre alrededor de un núcleo numérico); Geocodes devolvería coincidencias
    # espurias al normalizar.
    if postal_code and not re.sub(r'\D', '', postal_code):
        return False, 'format', None
    country_code = (country_code or '').strip().upper()
    postal_code = (postal_code or '').strip()
    if not country_code or not postal_code:
        return True, None, None

    rules = _get_zip_rules(country_code)
    if rules is None:
        return True, None, None
    if not rules.get('visible') or not rules.get('required'):
        # Envia does not require a postal code for this country (e.g. CL, CO)
        return True, None, None

    regex = rules.get('regex')
    if regex:
        try:
            if not re.search(regex, postal_code):
                return False, 'format', _zip_regex_example(regex)
        except re.error:
            _logger.warning("Envia zip regex for %s is not Python-compatible: %s",
                            country_code, regex)

    exists = _zip_exists(country_code, postal_code)
    if exists is False:
        return False, 'not_found', _zip_regex_example(regex)
    return True, None, None


class EnviaRequest:

    def __init__(self, api_key, prod_environment, debug_logger=None):
        self.api_key = api_key
        self.debug_logger = debug_logger
        if prod_environment:
            self.shipping_url = SHIPPING_API_PROD
            self.queries_url = QUERIES_API_PROD
        else:
            self.shipping_url = SHIPPING_API_TEST
            self.queries_url = QUERIES_API_TEST

    def _make_request(self, base_url, endpoint, payload, method='POST'):
        url = f"{base_url}{endpoint}"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        _logger.info("Envia API %s %s request: %s", method, endpoint,
                      json.dumps(payload, indent=2, default=str))

        try:
            if method == 'POST':
                response = requests.post(url, json=payload, headers=headers, timeout=30)
            else:
                response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise Exception("Envia.com API timeout. Please try again.")
        except requests.exceptions.HTTPError as e:
            body = {}
            try:
                body = e.response.json()
            except Exception:
                pass
            error_msg = body.get('message') or body.get('error') or str(e)
            _logger.error("Envia API %s %s error: %s", method, endpoint, error_msg)
            raise Exception(f"Envia.com API error: {error_msg}")
        except requests.exceptions.ConnectionError:
            raise Exception("Could not connect to Envia.com API. Check your internet connection.")

        result = response.json()
        _logger.info("Envia API %s %s response: %s", method, endpoint,
                      json.dumps(result, indent=2, default=str))
        return result

    # -------------------------
    # Address / Package helpers
    # -------------------------

    # Cache for address structure per country (avoids repeated API calls)
    _address_structure_cache = {}

    def _country_uses_city_select(self, country_code, headers):
        """Check if a country uses city_select (DANE code) instead of free-text city.
        Uses queries API /generic-form endpoint. Results are cached in memory.
        """
        if country_code in self._address_structure_cache:
            return self._address_structure_cache[country_code]

        try:
            resp = requests.get(
                f"{QUERIES_API_PROD}/generic-form",
                params={'country_code': country_code, 'form': 'address_info'},
                headers=headers, timeout=10)
            resp.raise_for_status()
            fields = resp.json()
        except Exception as e:
            _logger.warning("Envia address-structure lookup failed for %s: %s", country_code, e)
            self._address_structure_cache[country_code] = False
            return False

        uses_city_select = False
        if isinstance(fields, list):
            for f in fields:
                if not isinstance(f, dict):
                    continue
                if (f.get('fieldId') == 'city_select'
                        and f.get('visible')
                        and f.get('rules', {}).get('required')):
                    uses_city_select = True
                    break

        self._address_structure_cache[country_code] = uses_city_select
        _logger.info("Envia address-structure %s: uses_city_select=%s", country_code, uses_city_select)
        return uses_city_select

    def _normalize_address_via_geocodes(self, country_code, postal_code, city_name='', state_code=''):
        """Normalize address codes via Envia Geocodes API.

        For countries using city_select (e.g. CO): resolves DANE municipal code.
        For all countries: normalizes state code.
        Falls back to city lookup when zipcode lookup fails.
        """
        if not country_code:
            return {}

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }

        needs_dane = self._country_uses_city_select(country_code, headers)
        result = {}

        # Strategy 1: lookup by zipcode
        if postal_code:
            result = self._geocodes_by_zipcode(country_code, postal_code, headers)

        # Strategy 2: fallback to city lookup
        if not result and city_name:
            # Try with state first, then without (state might be wrong)
            result = self._geocodes_by_city(country_code, city_name, headers, state_code)
            if not result and state_code:
                result = self._geocodes_by_city(country_code, city_name, headers, '')

        # Strategy 3: if we have state but still need DANE, do city lookup
        if needs_dane and result and not result.get('dane_code') and city_name:
            resolved_state = result.get('state', state_code)
            city_result = self._geocodes_by_city(country_code, city_name, headers, resolved_state)
            if not city_result and resolved_state:
                city_result = self._geocodes_by_city(country_code, city_name, headers, '')
            if city_result.get('dane_code'):
                result['dane_code'] = city_result['dane_code']
            if city_result.get('postal_code') and not result.get('postal_code'):
                result['postal_code'] = city_result['postal_code']

        # Only include dane_code if country actually needs it
        if not needs_dane:
            result.pop('dane_code', None)

        return result

    def _geocodes_by_zipcode(self, country_code, postal_code, headers):
        """Lookup by zipcode. Returns {'state': ...} and optionally 'dane_code'."""
        url = f"{GEOCODES_API_URL}/zipcode/{country_code}/{postal_code}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            _logger.warning("Envia geocodes zipcode lookup failed for %s/%s: %s", country_code, postal_code, e)
            return {}

        if not data or not isinstance(data, list) or len(data) == 0:
            _logger.info("Envia geocodes: no data for zipcode %s/%s, will try city fallback", country_code, postal_code)
            return {}

        entry = data[0]
        state_codes = entry.get('state', {}).get('code', {})
        state = state_codes.get('2digit') or state_codes.get('3digit') or ''
        result = {'state': state}

        # Extract DANE 8-digit code if available
        dane = entry.get('info', {}).get('stat_8digit', '')
        if not dane:
            suburbs = entry.get('suburbs', [])
            dane = suburbs[0] if suburbs else ''
        if dane:
            result['dane_code'] = dane

        _logger.info("Envia geocodes %s/%s: state=%s, dane=%s (via zipcode)",
                     country_code, postal_code, state, dane or '-')
        return result

    def _geocodes_by_city(self, country_code, city_name, headers, state_code=''):
        """Lookup by city name. Returns {'state': ...} and optionally 'postal_code', 'dane_code'."""
        if state_code:
            url = f"{GEOCODES_API_URL}/locate/{country_code}/{state_code}/{city_name}"
        else:
            url = f"{GEOCODES_API_URL}/locate/{country_code}/{city_name}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            _logger.warning("Envia geocodes city lookup failed for %s/%s: %s", country_code, city_name, e)
            return {}

        if not data or not isinstance(data, list) or len(data) == 0:
            if state_code:
                _logger.info("Envia geocodes: no data for city %s/%s/%s", country_code, state_code, city_name)
            else:
                _logger.warning("Envia geocodes: no data for city %s/%s", country_code, city_name)
            return {}

        entry = data[0]
        state_codes = entry.get('state', {}).get('code', {})
        state = state_codes.get('2digit') or state_codes.get('3digit') or ''
        zip_codes = entry.get('zip_codes', [])
        postal_code = zip_codes[0].get('zip_code', '') if zip_codes else ''

        result = {'state': state}
        if postal_code:
            result['postal_code'] = postal_code

        # Extract DANE code if available
        if zip_codes:
            dane = zip_codes[0].get('info', {}).get('stat_8digit', '')
            if not dane:
                suburbs = zip_codes[0].get('suburbs', [])
                dane = suburbs[0] if suburbs else ''
            if dane:
                result['dane_code'] = dane

        _logger.info("Envia geocodes %s/%s: state=%s, postal_code=%s, dane=%s (via city lookup)",
                     country_code, city_name, state, postal_code or '-', result.get('dane_code', '-'))
        return result

    def _prepare_address(self, partner, carrier_code=None, needs_branch=False, branch_code=None):
        country_code = partner.country_id.code or ''
        postal_code = partner.zip or ''

        address = {
            'name': partner.name or '',
            'company': partner.commercial_company_name or partner.name or '',
            'email': partner.email or '',
            'phone': partner.phone or getattr(partner, 'mobile', '') or '',
            'street': partner.street or '',
            'number': '',
            'district': partner.street2 or '',
            'city': partner.city or '',
            'state': partner.state_id.code or '',
            'country': country_code,
            'postalCode': postal_code,
        }

        # Normalize state, postal code, and DANE code via Envia Geocodes API
        geo = self._normalize_address_via_geocodes(
            country_code, postal_code, partner.city or '', partner.state_id.code or '')
        if geo.get('state'):
            address['state'] = geo['state']
        if geo.get('postal_code'):
            address['postalCode'] = geo['postal_code']
        # Countries like CO use DANE municipal code as city in Envia API
        if geo.get('dane_code'):
            address['city'] = geo['dane_code']

        # Include address_id if partner has one cached
        if hasattr(partner, 'envia_address_id') and partner.envia_address_id:
            address['address_id'] = partner.envia_address_id

        # Branch code for 'a sucursal' (home-to-branch) services. Envia rejects
        # /ship/generate/ with error 1127 when the service requires a branch
        # and this field is missing. Field name must be camelCase 'branchCode'.
        # Resolution order:
        #   1. partner.envia_branch_code (manual override)
        #   2. lookup by postal code via queries.envia.com/branches/<country>,
        #      filtering by carrier_code when provided.
        # Only inject it when the service actually delivers to a branch
        # (needs_branch), and only ever on the destination (never the origin,
        # which is our warehouse / a domicilio). Injecting a branchCode on a
        # 'a domicilio' service or on the origin can misroute the parcel, so we
        # gate it explicitly instead of relying on envia ignoring stray fields.
        if needs_branch:
            # branch_code explícito = punto de retiro elegido en el checkout (Odoo 19 pickup locations)
            branch_code = branch_code or self._resolve_branch_code(partner, carrier_code)
            if branch_code:
                address['branchCode'] = branch_code

        return address

    # ------------------------------------------------------------------
    # Branch resolution (used by 'a sucursal' / home-to-branch services)
    # ------------------------------------------------------------------

    def _resolve_branch_code(self, partner, carrier_code=None):
        """Pick a destination branch code for *partner*.

        Order:
        1. ``partner.envia_branch_code`` — manual override / pickup point chosen at checkout.
        2. ``queries.envia.com/branches/<country>?zipcode=<zip>[&carrier=<code>]`` — the server
           filters by postal code (the unfiltered list is capped at 300 entries per carrier, so
           matching locally would miss most of the country). First try the exact zip, then its
           numeric core (Argentine 'S2600DAA' → '2600').
        """
        if hasattr(partner, 'envia_branch_code') and partner.envia_branch_code:
            return partner.envia_branch_code.strip()
        country = (partner.country_id.code or '').upper() if partner.country_id else ''
        zipc = (partner.zip or '').strip()
        if not country or not zipc:
            return None
        for candidate in dict.fromkeys([zipc, self._zip_core(zipc)]):
            if not candidate:
                continue
            match = self._pick_branch(self._get_branches(country, candidate, carrier_code), candidate, carrier_code)
            if match:
                _logger.info("Envia branch auto-resolved for partner '%s' (zip=%s, carrier=%s): %s (%s)",
                             partner.name, candidate, carrier_code or '-', match.get('branch_code'),
                             (match.get('address') or {}).get('city'))
                return match.get('branch_code')
        return None

    def list_branches(self, country_code, zipcode, carrier_code=None):
        """Sucursales que reciben envíos cerca de un código postal, normalizadas para el selector de
        puntos de retiro de Odoo 19 (sale.order._get_pickup_locations)."""
        results = []
        for candidate in dict.fromkeys([(zipcode or '').strip(), self._zip_core(zipcode)]):
            if not candidate:
                continue
            for group in self._get_branches(country_code, candidate, carrier_code) or []:
                if not isinstance(group, dict):
                    continue
                for cc, entries in group.items():
                    if carrier_code and cc != carrier_code:
                        continue
                    for b in entries or []:
                        addr = (b or {}).get('address') or {}
                        if not addr.get('delivery') or not b.get('branch_code'):
                            continue
                        street = ' '.join(x for x in [addr.get('address'), addr.get('number')] if x)
                        results.append({
                            'id': b['branch_code'],
                            'name': (b.get('reference') or b['branch_code']).title(),
                            'street': street.title(),
                            'city': (addr.get('city') or addr.get('locality') or '').title(),
                            'state': (addr.get('province') or '').title(),
                            'zip_code': addr.get('zipcode') or candidate,
                            'country_code': country_code,
                            'latitude': float(addr.get('latitude') or 0),
                            'longitude': float(addr.get('longitude') or 0),
                            'opening_hours': self._format_hours(b.get('hours')),
                            'additional_data': {'branch_code': b['branch_code'], 'carrier': cc},
                        })
            if results:
                break
        return results

    @staticmethod
    def _format_hours(hours):
        """{'0'..'6': ['09:00 - 18:00']} como espera el selector; vacío si Envia no publica horarios."""
        out = {str(i): [] for i in range(7)}
        for h in hours or []:
            if not isinstance(h, dict):
                continue
            day = h.get('day') if h.get('day') is not None else h.get('dayofweek')
            rng = h.get('hours') or (f"{h.get('open')} - {h.get('close')}" if h.get('open') and h.get('close') else None)
            if day is not None and rng:
                try:
                    out[str(int(day) % 7)].append(str(rng))
                except (TypeError, ValueError):
                    pass
        return out

    @staticmethod
    def _zip_core(zipc):
        """Reduce a postal code to its numeric core for branch matching.

        Argentine CPA codes ('S2600', 'S2600DAA') and the legacy 4-digit code
        ('2600') name the same locality; the branches database stores one form
        while a partner may carry another. Comparing the digits-only core makes
        'S2600' match a branch stored as '2600'. For purely numeric codes
        (most countries) this is a no-op."""
        return re.sub(r'\D', '', zipc or '')

    def _pick_branch(self, branches_payload, zipc, carrier_code):
        """Walk the queries.envia.com /branches response shape and return the
        first branch entry whose zipcode matches and which accepts delivery.

        Matches the exact zip first, then falls back to the numeric core so an
        Argentine CPA destination ('S2600') resolves a branch stored as
        '2600'."""
        zipc = (zipc or '').strip()
        zip_core = self._zip_core(zipc)
        fallback = None
        for group in branches_payload or []:
            if not isinstance(group, dict):
                continue
            for cc, entries in group.items():
                if carrier_code and cc != carrier_code:
                    continue
                for b in entries or []:
                    addr = (b or {}).get('address') or {}
                    if not addr.get('delivery'):
                        continue
                    branch_zip = (addr.get('zipcode') or '').strip()
                    if branch_zip == zipc:
                        return b
                    if fallback is None and zip_core and self._zip_core(branch_zip) == zip_core:
                        fallback = b
        return fallback

    def _get_branches(self, country_code, zipcode=None, carrier_code=None):
        """Fetch and cache the branches for a country, filtered server-side by zipcode / carrier
        (query params `zipcode` and `carrier`). 24h TTL per (env, country, zipcode, carrier)."""
        env = 'prod' if self.shipping_url == SHIPPING_API_PROD else 'test'
        key = (env, country_code, zipcode or '', carrier_code or '')
        now = time.time()
        cached = _BRANCHES_CACHE.get(key)
        if cached and (now - cached[0]) < _BRANCHES_TTL_SECONDS:
            return cached[1]
        params = {k: v for k, v in (('zipcode', zipcode), ('carrier', carrier_code)) if v}
        url = f"{self.queries_url}/branches/{country_code}"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json() or []
        except Exception as e:
            _logger.warning("Envia branches fetch failed for %s %s: %s", country_code, params, e)
            _BRANCHES_CACHE[key] = (now - _BRANCHES_TTL_SECONDS + 300, [])
            return []
        _BRANCHES_CACHE[key] = (now, data)
        return data

    @staticmethod
    def _prepare_phone(phone, country_phone_code):
        """Strip international prefix from phone number.

        Envia requires phone without country prefix (max 11 chars)
        and a separate phone_code field with the ISO country code.
        """
        if not phone:
            return '', ''
        digits = re.sub(r'\D', '', phone)
        # Remove country calling code prefix (e.g. 54 for AR, 1 for US)
        if country_phone_code:
            prefix = str(country_phone_code)
            if digits.startswith(prefix):
                digits = digits[len(prefix):]
        return digits[:11], ''

    ENVIA_ADDRESS_TYPE_ORIGIN = 1
    ENVIA_ADDRESS_TYPE_DESTINATION = 2

    def _prepare_address_book_payload(self, partner, address_type=2):
        """Build payload for Envia address book API (POST /user-address).
        address_type: 1=origin, 2=destination
        """
        country_code = partner.country_id.code or ''
        phone_code = country_code
        phone, _ = self._prepare_phone(
            partner.phone or partner.mobile or '',
            partner.country_id.phone_code if partner.country_id else '',
        )

        geo = self._normalize_address_via_geocodes(country_code, partner.zip or '', partner.city or '')

        return {
            'type': address_type,
            'name': partner.name or '',
            'company': partner.commercial_company_name or '',
            'email': partner.email or '',
            'phone': phone,
            'phone_code': phone_code,
            'street': partner.street or '',
            'city': partner.city or '',
            'state': geo.get('state', partner.state_id.code or ''),
            'country': country_code,
            'postal_code': partner.zip or '',
            'identification_number': partner.vat or '',
            'reference': partner.street2 or '',
        }

    def _address_needs_update(self, partner, existing_addresses):
        """Check if partner's address differs from what's stored in Envia."""
        for addr in existing_addresses:
            if addr.get('address_id') == partner.envia_address_id:
                return (
                    addr.get('name', '') != (partner.name or '') or
                    addr.get('street', '') != (partner.street or '') or
                    addr.get('postal_code', '') != (partner.zip or '') or
                    addr.get('country', '') != (partner.country_id.code or '')
                )
        # Address not found in Envia - needs re-creation
        return True

    def sync_address(self, partner, address_type=2):
        """Sync partner address to Envia address book. Returns address_id.
        address_type: 1=origin, 2=destination
        """
        payload = self._prepare_address_book_payload(partner, address_type)
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }

        existing_id = partner.envia_address_id if hasattr(partner, 'envia_address_id') else 0

        try:
            if existing_id:
                # Try to update existing address
                url = f"{self.queries_url}/user-address/{existing_id}"
                resp = requests.put(url, json=payload, headers=headers, timeout=15)
                if resp.status_code == 200:
                    _logger.info("Envia address updated: id=%s for partner %s", existing_id, partner.name)
                    return existing_id
                else:
                    # Address no longer exists in Envia, recreate
                    _logger.info("Envia address %s not found (status %s), recreating for partner %s",
                                 existing_id, resp.status_code, partner.name)
                    existing_id = 0  # Fall through to create

            # Create new address
            url = f"{self.queries_url}/user-address"
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            address_id = result.get('id', 0)
            _logger.info("Envia address created: id=%s for partner %s", address_id, partner.name)
            return address_id
        except Exception as e:
            _logger.warning("Envia address sync failed for partner %s: %s", partner.name, e)
            return 0

    @staticmethod
    def _prepare_packages(packages, weight_override=0, length_override=0,
                          width_override=0, height_override=0, declared_value_override=0):
        result = []
        for pkg in packages:
            weight = weight_override if weight_override > 0 else pkg.weight
            # Odoo stores package dimensions in mm, Envia expects cm
            length = length_override if length_override > 0 else pkg.dimension.get('length', 0) / 10
            width = width_override if width_override > 0 else pkg.dimension.get('width', 0) / 10
            height = height_override if height_override > 0 else pkg.dimension.get('height', 0) / 10
            declared_value = declared_value_override if declared_value_override > 0 else pkg.total_cost

            result.append({
                'content': pkg.name or 'Package',
                'amount': 1,
                'type': 'box',
                'weight': weight,
                'insurance': declared_value,
                'declaredValue': declared_value,
                'weightUnit': 'KG',
                'lengthUnit': 'CM',
                'dimensions': {
                    'length': length,
                    'width': width,
                    'height': height,
                },
            })

        # ``declared_value_override`` is the declared value of the *whole*
        # shipment (order insurance total / picking override), so putting it on
        # every box would declare — and insure — it once per box. Spread it over
        # the boxes proportionally to their weight instead; the total stays
        # exact and each box carries its own share.
        if declared_value_override > 0 and len(result) > 1:
            total_weight = sum(p['weight'] for p in result)
            running = 0.0
            for pkg_payload in result[:-1]:
                share = (round(declared_value_override * pkg_payload['weight'] / total_weight, 2)
                         if total_weight > 0
                         else round(declared_value_override / len(result), 2))
                pkg_payload['insurance'] = pkg_payload['declaredValue'] = share
                running += share
            last = result[-1]
            last['insurance'] = last['declaredValue'] = round(declared_value_override - running, 2)

        return result

    # ----------------
    # Shipping API
    # ----------------

    def _rate_payload(self, origin, destination, packages, carrier_code,
                      service_code=None, customs_settings=None):
        payload = {
            'origin': origin,
            'destination': destination,
            'packages': packages,
            'shipment': {
                'carrier': carrier_code,
                'type': 1,
            },
        }
        if service_code:
            payload['shipment']['service'] = service_code
        # customsSettings controls who pays duties/taxes (DDP). Only relevant for
        # international shipments; envia ignores it for domestic routes.
        if customs_settings:
            payload['customsSettings'] = customs_settings
        return payload

    def rate(self, origin, destination, packages, carrier_code, service_code=None,
             customs_settings=None):
        payload = self._rate_payload(origin, destination, packages, carrier_code,
                                     service_code, customs_settings)

        result = self._make_request(self.shipping_url, '/ship/rate/', payload)

        # A carrier that cannot take a multi-package array answers with a bare
        # 'Internal error'. Callers screen the known ones beforehand; if a new
        # one shows up, leave a pointer in the log instead of a dead end.
        if (_is_error_payload(result) and len(packages) > 1
                and carrier_code not in MULTI_PACKAGE_UNSUPPORTED):
            _logger.warning(
                "Envia rate failed for carrier '%s' with %d packages (%s). "
                "Carriers that cannot take a multi-package request fail exactly "
                "like this; if '%s' is one of them, add it to "
                "MULTI_PACKAGE_UNSUPPORTED so the user gets a clear message.",
                carrier_code, len(packages), (result or {}).get('message'),
                carrier_code)

        if isinstance(result, dict):
            if _is_error_payload(result):
                _raise_envia_error(result, default='Unknown rate error')
            # Response can be {"meta": "rate", "data": [...]} or a flat rate dict
            if 'data' in result and isinstance(result['data'], list):
                if not result['data']:
                    raise Exception("No rates returned from Envia.com for this route/carrier.")
                return result['data']
            return [result]
        if isinstance(result, list):
            if not result:
                raise Exception("No rates returned from Envia.com for this route/carrier.")
            return result
        raise Exception(f"Unexpected rate response format: {result}")

    def generate_shipment(self, origin, destination, packages, carrier_code,
                          service_code=None, label_format='PDF', label_size='PAPER_4X6',
                          customs_settings=None):
        payload = {
            'origin': origin,
            'destination': destination,
            'packages': packages,
            'shipment': {
                'carrier': carrier_code,
                'type': 1,
            },
            'settings': {
                'printFormat': label_format,
                'printSize': label_size,
            },
        }
        if service_code:
            payload['shipment']['service'] = service_code
        # customsSettings controls who pays duties/taxes (DDP). Only relevant for
        # international shipments; envia ignores it for domestic routes.
        if customs_settings:
            payload['customsSettings'] = customs_settings

        result = self._make_request(self.shipping_url, '/ship/generate/', payload)

        if _is_error_payload(result):
            _raise_envia_error(result, default='Shipment generation failed')
        return result

    def track(self, carrier_code, tracking_number):
        payload = {
            'carrier': carrier_code,
            'trackingNumbers': [tracking_number],
        }
        return self._make_request(self.shipping_url, '/ship/generaltrack/', payload)

    def cancel(self, carrier_code, tracking_number):
        payload = {
            'carrier': carrier_code,
            'trackingNumber': tracking_number,
        }
        result = self._make_request(self.shipping_url, '/ship/cancel/', payload)
        if isinstance(result, dict) and (result.get('meta') == 'error' or result.get('error')):
            _raise_envia_error(result, default='Cancellation failed')
        return result

    # ----------------
    # Queries API
    # ----------------

    def get_available_carriers(self):
        return self._make_request(self.queries_url, '/available-carriers', {})
