"""SAP Service Layer client — CURRENTLY UNUSED.

Nothing imports `HANAServiceLayer`. It is kept rather than deleted so the
history stays intact, but it should not be adopted: `payments/sap_client.py`
and `einvoice/sap.py` are the maintained clients, and `serviceLayer/service.py`
is the one that handles per-company sessions.

Two defects were fixed here rather than left in place, because dead code gets
revived and this pair is dangerous:

* NO REQUEST HAD A TIMEOUT. A hung SAP connection would hold the calling
  thread until the OS gave up, which for a default Linux TCP connect is minutes
  and for a stalled read is never.
* `_post_with_ssl_fallback` caught SSLError and RETRIED WITH VERIFICATION OFF,
  then left it off for the life of the object. That is worse than never
  verifying: it yields precisely when verification is doing its job, and sends
  SAP credentials over the connection it just failed to authenticate. TLS
  configuration now comes from settings, the same as every other client, and a
  certificate failure is a failure.
"""
import requests
import urllib3
from django.conf import settings


def _verify():
    bundle = getattr(settings, "HANA_SSL_CA_BUNDLE", "") or ""
    return bundle or getattr(settings, "HANA_SSL_VERIFY", True)


def _timeout():
    return (
        getattr(settings, "HANA_CONNECT_TIMEOUT", None) or 15,
        getattr(settings, "HANA_READ_TIMEOUT", None) or 120,
    )


class HANAServiceLayer:
    def __init__(self):
        self.base_url = settings.HANA_SERVICE_LAYER_URL
        self.username = settings.HANA_USERNAME
        self.password = settings.HANA_PASSWORD
        self.company_db = settings.HANA_OIL_COMPANY_DB
        self.ssl_verify = _verify()
        if self.ssl_verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session = requests.Session()
        self.session.verify = self.ssl_verify
        self.login()

    def _post(self, url, payload):
        return self.session.post(url, json=payload, verify=self.ssl_verify,
                                 timeout=_timeout())

    def login(self):
        login_data = {
            "UserName": self.username,
            "Password": self.password,
            "CompanyDB": self.company_db,
        }
        response = self._post(f"{self.base_url}/Login", login_data)
        if response.status_code != 200:
            raise Exception("HANA Login Failed")

    def post(self, endpoint, payload):
        response = self._post(f"{self.base_url}/{endpoint}", payload)
        if response.status_code not in (200, 201):
            raise Exception(response.text)
        return response.json()
