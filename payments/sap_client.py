"""SAP Service Layer client for payments.

Modelled on einvoice/sap.py — the cleanest SAP client in the project: module
level typed functions, _base()/_verify()/_timeout() helpers so a call can never
forget its timeout, one typed exception, truncated error bodies, and
`raise ... from exc`.

Two bugs in serviceLayer/service.py are deliberately NOT reproduced:

  * its session cache key is the global 'b1_session' with no company DB in it
    (service.py:15). This module talks to three company DBs, so a cached OIL
    session would be used for a BEVERAGES post — silently crediting the wrong
    company. Here the key is per company DB.
  * it treats SAP's SessionTimeout (minutes) as seconds (service.py:35).
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

SESSION_CACHE_PREFIX = 'payments:sap_session'


class SapError(Exception):
    """Any Service Layer failure. Carries the HTTP status when there was one."""

    def __init__(self, message, *, status_code=None, sap_code='', payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.sap_code = sap_code
        self.payload = payload


def _base():
    return settings.HANA_SERVICE_LAYER_URL.rstrip('/')


def _verify():
    """Honour the configured TLS setting, unlike serviceLayer/service.py which
    hardcodes verify=False."""
    bundle = getattr(settings, 'HANA_SSL_CA_BUNDLE', '') or ''
    if bundle:
        return bundle
    return getattr(settings, 'HANA_SSL_VERIFY', True)


def _timeout():
    return (
        getattr(settings, 'HANA_CONNECT_TIMEOUT', None) or 15,
        getattr(settings, 'HANA_READ_TIMEOUT', None) or 120,
    )


def _cache_key(company_db):
    return f'{SESSION_CACHE_PREFIX}:{company_db}'


def login(company_db):
    """Fresh Service Layer login for one company DB. Returns a Session."""
    session = requests.Session()
    session.verify = _verify()
    payload = {
        'CompanyDB': company_db,
        'UserName': settings.HANA_USERNAME,
        'Password': settings.HANA_PASSWORD,
    }
    try:
        response = requests.post(f'{_base()}/Login', json=payload,
                                 verify=_verify(), timeout=_timeout())
    except requests.RequestException as exc:
        raise SapError(f'Service Layer login to {company_db} failed: {exc}') from exc

    if response.status_code != 200:
        raise SapError(
            f'Service Layer login to {company_db} failed '
            f'({response.status_code}): {response.text[:300]}',
            status_code=response.status_code)

    data = response.json()
    session_id = data.get('SessionId')
    session.cookies.set('B1SESSION', session_id)
    route = response.cookies.get('ROUTEID')
    if route:
        session.cookies.set('ROUTEID', route)      # load-balancer affinity

    # SessionTimeout is in MINUTES. Keep a 2-minute safety margin.
    minutes = int(data.get('SessionTimeout') or 30)
    ttl = max(60, minutes * 60 - 120)
    cache.set(_cache_key(company_db), {'session_id': session_id, 'route': route}, ttl)
    return session


def get_session(company_db):
    """Cached session for a company DB, logging in on a miss."""
    cached = cache.get(_cache_key(company_db))
    if cached and cached.get('session_id'):
        session = requests.Session()
        session.verify = _verify()
        session.cookies.set('B1SESSION', cached['session_id'])
        if cached.get('route'):
            session.cookies.set('ROUTEID', cached['route'])
        return session
    return login(company_db)


def clear_session(company_db):
    cache.delete(_cache_key(company_db))


def _parse_error(response):
    """Pull SAP's own error code and message out of the body.

    SAP returns {"error": {"code": -5002, "message": {"lang": .., "value": ..}}}.
    The body is not always JSON (an HTML 502 from a proxy, for instance), so
    this never assumes it is — serviceLayer/views.py:62 calls .json() unguarded
    and turns a clean 4xx into an opaque 500.
    """
    try:
        body = response.json()
    except ValueError:
        return '', response.text[:300], None
    err = (body or {}).get('error') or {}
    code = str(err.get('code', ''))
    message = err.get('message')
    if isinstance(message, dict):
        message = message.get('value', '')
    return code, str(message or response.text[:300]), body


def request(method, path, *, company_db, json_body=None, session=None,
            retry_on_401=True):
    """Call the Service Layer, re-logging in once on a 401.

    Returns (status_code, parsed_body). Raises SapError on transport failure.

    The 401 retry re-sends the request. That is safe for GETs, and for a POST
    it is bounded: a 401 means the session was rejected, so the first attempt
    did not reach the business layer. A retry AFTER a timeout is a different
    matter and is deliberately NOT done here — see sap_poster.post_document.
    """
    session = session or get_session(company_db)
    url = f'{_base()}{path}'
    try:
        response = session.request(method, url, json=json_body,
                                   verify=_verify(), timeout=_timeout())
    except requests.RequestException as exc:
        raise SapError(f'{method} {path} failed: {exc}') from exc

    if response.status_code == 401 and retry_on_401:
        clear_session(company_db)                  # per-company key, not global
        session = login(company_db)
        try:
            response = session.request(method, url, json=json_body,
                                       verify=_verify(), timeout=_timeout())
        except requests.RequestException as exc:
            raise SapError(f'{method} {path} retry failed: {exc}') from exc

    if response.status_code >= 400:
        code, message, body = _parse_error(response)
        raise SapError(message, status_code=response.status_code,
                       sap_code=code, payload=body)

    if response.status_code == 204 or not response.content:
        return response.status_code, {}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


# ---------------------------------------------------------------------------
# Payment-specific calls
# ---------------------------------------------------------------------------

def fetch_document(doc_entry, company_db, *, entity='IncomingPayments'):
    """Read one SAP document by its DocEntry.

    `sap_doc_entry` is the permanent OMS<->SAP link, so every lookup goes
    through it. Returns the document dict, or None when SAP reports 404.

    Any other failure raises — a caller must never read "could not check" as
    "not there".
    """
    try:
        _, body = request('GET', f'/{entity}({int(doc_entry)})',
                          company_db=company_db)
    except SapError as exc:
        if exc.status_code == 404:
            return None
        raise
    return body or None


def post_incoming_payment(payload, company_db, *, session=None):
    """POST /IncomingPayments. Returns the created document."""
    _, body = request('POST', '/IncomingPayments', company_db=company_db,
                      json_body=payload, session=session)
    return body


def post_deposit(payload, company_db, *, session=None):
    """POST /Deposits. Returns the created document."""
    _, body = request('POST', '/Deposits', company_db=company_db,
                      json_body=payload, session=session)
    return body


def get_incoming_payment(doc_entry, company_db):
    """Read a posted payment back — used to capture PaymentChecks[].CheckKey."""
    _, body = request('GET', f'/IncomingPayments({int(doc_entry)})',
                      company_db=company_db)
    return body
