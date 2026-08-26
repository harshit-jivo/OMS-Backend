import requests
from django.core.cache import cache
from django.conf import settings


import urllib3
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
        session.verify = False

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
            response = requests.post(login_url, json=login_payload, verify=False, timeout=10)
            if response.status_code == 200:
                sap_data = response.json()
                timeout = sap_data.get('SessionTimeout', 3600) - 60

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
        session.verify = False

        schema = cls.schema_for(branch)

        login_url = f"{settings.HANA_SERVICE_LAYER_URL}/Login"
        login_payload = {
            "CompanyDB": schema,
            "UserName": username,
            "Password": password,
        }

        print(login_payload)

        try:
            response = requests.post(login_url, json=login_payload, verify=False, timeout=10)
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
            
    
        
    