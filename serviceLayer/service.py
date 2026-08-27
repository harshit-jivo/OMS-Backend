"""SAP Service Layer session manager.

Three clients in this project talk to the Service Layer — this one,
`einvoice/sap.py` and `payments/sap_client.py`. The other two share a shape
(`_base()` / `_verify()` / `_timeout()` helpers so a call cannot forget its
timeout, one typed exception, truncated error bodies). This one predates that
shape; the helpers below bring it into line without changing its API, because
`serviceLayer.views`, `serviceLayer.ap_views` and `einvoice.sap` all call it.
"""
import logging

import requests
import urllib3
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


def _verify():
    """Honour the configured TLS setting.

    This module used to hardcode `verify=False` in four places, which meant SAP
    credentials and invoice payloads went over an unverified connection
    regardless of configuration — and there was no way to turn verification on.
    `einvoice/sap.py` and `payments/sap_client.py` already read these settings;
    this is the last client that did not.
    """
    bundle = getattr(settings, 'HANA_SSL_CA_BUNDLE', '') or ''
    if bundle:
        return bundle
    return getattr(settings, 'HANA_SSL_VERIFY', True)


def _timeout():
    """(connect, read) from settings, never unbounded."""
    return (
        getattr(settings, 'HANA_CONNECT_TIMEOUT', None) or 15,
        getattr(settings, 'HANA_READ_TIMEOUT', None) or 120,
    )


def _session_ttl(sap_data):
    """Cache TTL in SECONDS for a Service Layer session.

    SAP reports `SessionTimeout` in MINUTES. This module treated it as seconds
    and subtracted 60, so a typical SessionTimeout of 30 became a TTL of -30 —
    which Django reads as already-expired, so the session was never reused and
    every call logged in again. When SAP omitted the field the 3600 default
    became a 59-minute TTL against a 30-minute session, i.e. the opposite
    failure: a cached session that SAP had already dropped.

    Converted properly, with a minute of headroom and a floor so a tiny or
    missing value can never produce a negative TTL.
    """
    try:
        minutes = int(sap_data.get('SessionTimeout') or 30)
    except (TypeError, ValueError):
        minutes = 30
    return max(60, minutes * 60 - 60)


if _verify() is False:
    # Only silence the warning when verification is off BY CONFIGURATION.
    # Disabling it unconditionally, as this module did at import time, hides
    # the warning even for deployments that have verification switched on.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SAPServiceLayerManager():

    @classmethod
    def schema_for(cls, branch):
        """Company DB for a branch code — OIL, BEVERAGE(S) or MART.

        Accepts both 'BEVERAGE' and 'BEVERAGES' (both spellings are used across
        the codebase); anything else -> OIL, so a missing/unknown branch can
        never leave the schema undefined."""
        code = str(branch or '').upper()
        if code.startswith('BEVERAGE'):
            return settings.HANA_BEVERAGE_COMPANY_DB
        if code.startswith('MART'):
            return getattr(settings, 'HANA_MART_COMPANY_DB', '') \
                or settings.HANA_OIL_COMPANY_DB
        return settings.HANA_OIL_COMPANY_DB

    # Backwards-compatible alias (was private before the branch rollout).
    _schema_for = schema_for

    @classmethod
    def branch_for(cls, company_db):
        """Inverse of schema_for: company DB -> branch code. Used when a caller
        already knows the company DB (e.g. an IRN request) and needs a branch."""
        if not company_db:
            return 'OIL'
        if company_db == getattr(settings, 'HANA_BEVERAGE_COMPANY_DB', None):
            return 'BEVERAGE'
        if company_db == getattr(settings, 'HANA_MART_COMPANY_DB', None):
            return 'MART'
        return 'OIL'

    @classmethod
    def _cache_keys(cls, schema):
        """Session cache keys are PER COMPANY DB. A single shared key would hand a
        cached OIL session back to a BEVERAGE caller, silently reading/writing the
        wrong company."""
        return f'b1_session::{schema}', f'route_id::{schema}'

    @classmethod
    def get_session(cls , branch):
        session = requests.Session()
        session.verify = _verify()

        schema = cls.schema_for(branch)
        session_key, route_key = cls._cache_keys(schema)
        b1_session = cache.get(session_key)
        route_id = cache.get(route_key)

        if b1_session and route_id:
            session.cookies.set('B1SESSION', b1_session)
            session.cookies.set('ROUTEID', route_id)

            return session

        login_url = f"{settings.HANA_SERVICE_LAYER_URL}/Login"
        login_payload = {
            "CompanyDB": schema,
            "UserName": settings.HANA_USERNAME,
            "Password": settings.HANA_PASSWORD
        }

        try:
            response = requests.post(login_url, json=login_payload,
                                     verify=_verify(), timeout=_timeout())
            if response.status_code == 200:
                sap_data = response.json()
                timeout = _session_ttl(sap_data)

                new_b1_session = sap_data.get('SessionId')
                new_route_id = response.cookies.get('ROUTEID')
                cache.set(session_key, new_b1_session, timeout=timeout)
                cache.set(route_key, new_route_id, timeout=timeout)

                session.cookies.set('B1SESSION', new_b1_session)
                session.cookies.set('ROUTEID', new_route_id)
                return session
            else:
                raise Exception(f"Login failed with status code {response.status_code}: {response.text}")
        except requests.RequestException as e:
            raise Exception(f"Login request failed: {str(e)}")
        
        
    @classmethod
    def get_session_for(cls, username, password , branch):
        session = requests.Session()
        session.verify = _verify()

        schema = cls.schema_for(branch)

        login_url = f"{settings.HANA_SERVICE_LAYER_URL}/Login"
        login_payload = {
            "CompanyDB": schema,
            "UserName": username,
            "Password": password,
        }

        # NB: never log `login_payload` — it carries the approver's password.
        # A `print(login_payload)` stood here, so every approver login wrote a
        # live SAP credential to stdout, i.e. to the server log.
        logger.debug('Approver login to CompanyDB=%s as %s', schema, username)

        try:
            response = requests.post(login_url, json=login_payload,
                                     verify=_verify(), timeout=_timeout())
            if response.status_code == 200:
                sap_data = response.json()
                session.cookies.set('B1SESSION', sap_data.get('SessionId'))
                session.cookies.set('ROUTEID', response.cookies.get('ROUTEID'))
                return session
            raise Exception(f"Approver login failed with status code {response.status_code}: {response.text}")
        except requests.RequestException as e:
            raise Exception(f"Approver login request failed: {str(e)}")

    @classmethod
    def clear_session(cls, branch=None):
        """Drop the cached session for `branch` (all companies when omitted)."""
        schemas = ([cls.schema_for(branch)] if branch is not None
                   else [settings.HANA_OIL_COMPANY_DB,
                         getattr(settings, 'HANA_BEVERAGE_COMPANY_DB', ''),
                         getattr(settings, 'HANA_MART_COMPANY_DB', '')])
        for schema in filter(None, schemas):
            session_key, route_key = cls._cache_keys(schema)
            cache.delete(session_key)
            cache.delete(route_key)
            
    
        
    