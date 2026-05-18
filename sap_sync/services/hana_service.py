import requests
import urllib3
from django.conf import settings


class HANAServiceLayer:
    def __init__(self):
        self.base_url = settings.HANA_SERVICE_LAYER_URL
        self.username = settings.HANA_USERNAME
        self.password = settings.HANA_PASSWORD
        self.company_db = settings.HANA_COMPANY_DB
        self.ssl_verify = (
            settings.HANA_SSL_CA_BUNDLE
            if getattr(settings, "HANA_SSL_CA_BUNDLE", "")
            else getattr(settings, "HANA_SSL_VERIFY", True)
        )
        if self.ssl_verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session = requests.Session()
        self.login()

    def _post_with_ssl_fallback(self, url, payload):
        try:
            return self.session.post(url, json=payload, verify=self.ssl_verify)
        except requests.exceptions.SSLError:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.ssl_verify = False
            self.session.verify = False
            return self.session.post(url, json=payload, verify=False)

    def login(self):
        login_data = {
            "UserName": self.username,
            "Password": self.password,
            "CompanyDB": self.company_db
        }

        response = self._post_with_ssl_fallback(
            f"{self.base_url}/Login",
            login_data
        )

        if response.status_code != 200:
            raise Exception("HANA Login Failed")

    def post(self, endpoint, payload):
        response = self._post_with_ssl_fallback(
            f"{self.base_url}/{endpoint}",
            payload
        )

        if response.status_code not in [200, 201]:
            raise Exception(response.text)

        return response.json()
