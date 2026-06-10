import requests
from django.core.cache import cache
from django.conf import settings


import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SAPServiceLayerManager():
    
    @classmethod
    def get_session(cls):
        session = requests.Session()
        session.verify = False
        b1_session = cache.get('b1_session')
        route_id = cache.get('route_id')

        if b1_session and route_id:
            session.cookies.set('B1SESSION', b1_session)
            session.cookies.set('ROUTEID', route_id)

            return session

        login_url = f"{settings.HANA_SERVICE_LAYER_URL}/Login"
        login_payload = {
            "CompanyDB": settings.HANA_COMPANY_DB,
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
                cache.set('b1_session', new_b1_session, timeout=timeout)
                cache.set('route_id', new_route_id, timeout=timeout)
                
                session.cookies.set('B1SESSION', new_b1_session)
                session.cookies.set('ROUTEID', new_route_id)
                return session
            else:
                raise Exception(f"Login failed with status code {response.status_code}: {response.text}")
        except requests.RequestException as e:
            raise Exception(f"Login request failed: {str(e)}")
        
        
    @classmethod
    def clear_session(cls):
        cache.delete('b1_session')
        cache.delete('route_id') 
            
    
        
    