"""
NIC e-Way Bill API client (standalone system, separate from e-Invoice).

Same crypto scheme as e-Invoice (RSA/PKCS1 for the auth payload, AES-256-ECB
with the SEK for everything else); the generic crypto helpers in
einvoice/crypto.py are reused. The differences from e-Invoice:

- Own hosts (ewb1api/ewb2api) and credentials/public key (settings.EWB).
- Auth posts to {base}/Auth.
- All write operations post to ONE endpoint {base}/ewayapi with an "action"
  code (GENEWAYBILL, VEHEWB, CANEWB, ...) plus the encrypted "Data".

NOTE: confirm the auth envelope / field casing against the EWB API spec. The
format mirrors e-Invoice (base64-before-RSA auth; {action, Data} ops). The most
likely points to adjust are isolated in `_authenticate()` / `_session_from_body()`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings
from django.core.cache import cache

from einvoice import crypto  # shared crypto utilities


class EwbError(Exception):
    """Raised when the EWB API returns a business/validation error."""

    def __init__(self, message, *, error_details=None, status_code=None):
        super().__init__(message)
        self.error_details = error_details
        self.status_code = status_code


@dataclass
class EwbSession:
    auth_token: str
    sek: bytes
    expires_at: datetime


_CACHE_KEY = "ewb_session"
_EXPIRY_SAFETY = timedelta(minutes=2)


class EwbClient:
    def __init__(self):
        self.cfg = settings.EWB
        self.hosts = self.cfg["BASE_URLS"]

    # -- low level ----------------------------------------------------------

    def _request(self, method, path, headers, body=None, timeout=60):
        last_exc = None
        for host in self.hosts:
            try:
                return requests.request(method, f"{host}{path}", headers=headers,
                                        json=body, timeout=timeout)
            except requests.RequestException as exc:
                last_exc = exc
        raise EwbError(f"All e-Way Bill hosts unreachable: {self.hosts}",
                       error_details=str(last_exc))

    def _base_headers(self):
        return {
            "Content-Type": "application/json",
            "client-id": self.cfg["CLIENT_ID"],
            "client-secret": self.cfg["CLIENT_SECRET"],
            "gstin": self.cfg["GSTIN"],
        }

    @staticmethod
    def _parse(resp):
        try:
            return resp.json()
        except ValueError:
            raise EwbError(f"Non-JSON response from EWB (HTTP {resp.status_code})",
                           error_details=resp.text[:1000], status_code=resp.status_code)

    @staticmethod
    def _status(body):
        # EWB responses use "status" (lowercase); accept "Status" too.
        return str(body.get("status", body.get("Status", "")))

    @staticmethod
    def _decode_error(body):
        """EWB returns the error as a base64 string of {"errorCodes":"107,.."}.
        Decode it and annotate any codes we recognise."""
        err = body.get("error") or body.get("ErrorDetails")
        if isinstance(err, str) and err:
            try:
                err = json.loads(crypto.unb64(err))
            except Exception:
                pass
        codes = ""
        if isinstance(err, dict):
            codes = str(err.get("errorCodes", ""))
        known = {
            "107": "Invalid login credentials (username/password not valid for the e-Way Bill system)",
            "108": "Invalid client-id/client-secret or IP not registered",
            "109": "Token expired / invalid",
        }
        notes = [known[c] for c in codes.split(",") if c.strip() in known]
        return {"raw": err, "notes": notes} if notes else err

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    # -- authentication -----------------------------------------------------

    def _authenticate(self) -> EwbSession:
        app_key = crypto.generate_app_key()
        # Per the EWB reference sample: action + lowercase fields go INSIDE the
        # payload; the payload is base64-encoded then RSA-encrypted; the outer
        # body is only {"Data": ...}.
        payload = {
            "action": "ACCESSTOKEN",
            "username": self.cfg["USERNAME"],
            "password": self.cfg["PASSWORD"],
            "app_key": crypto.b64(app_key),
        }
        public_key = crypto.load_public_key(self.cfg["PUBLIC_KEY_PATH"])
        b64_payload = crypto.b64(json.dumps(payload).encode("utf-8")).encode("utf-8")
        enc = crypto.rsa_encrypt(b64_payload, public_key)

        resp = self._request("POST", self.cfg["AUTH_PATH"], self._base_headers(),
                             {"Data": enc}, timeout=30)
        body = self._parse(resp)

        if self._status(body) != "1":
            raise EwbError("EWB authentication failed",
                           error_details=self._decode_error(body),
                           status_code=resp.status_code)

        session = self._session_from_body(body, app_key)
        ttl = max(int((session.expires_at - self._now() - _EXPIRY_SAFETY).total_seconds()), 30)
        cache.set(_CACHE_KEY, {
            "auth_token": session.auth_token,
            "sek": crypto.b64(session.sek),
            "expires_at": session.expires_at.isoformat(),
        }, timeout=ttl)
        return session

    def _session_from_body(self, body: dict, app_key: bytes) -> EwbSession:
        # EWB returns authtoken + sek (sek AES-encrypted with our appkey -> raw
        # session key). Some variants nest these under "Data".
        src = body.get("Data") if isinstance(body.get("Data"), dict) else body
        auth_token = src.get("authtoken") or src.get("AuthToken")
        enc_sek = src.get("sek") or src.get("Sek")
        sek = crypto.aes_decrypt(enc_sek, app_key)
        expires_at = self._now() + timedelta(hours=6)  # EWB tokens last ~6h
        return EwbSession(auth_token=auth_token, sek=sek, expires_at=expires_at)

    def _get_session(self, force=False) -> EwbSession:
        if not force:
            c = cache.get(_CACHE_KEY)
            if c:
                exp = datetime.fromisoformat(c["expires_at"])
                if exp - _EXPIRY_SAFETY > self._now():
                    return EwbSession(c["auth_token"], crypto.unb64(c["sek"]), exp)
        return self._authenticate()

    # -- generic action -----------------------------------------------------

    def _action(self, action: str, payload: dict, *, _retry=True) -> dict:
        session = self._get_session()
        headers = self._base_headers()
        headers["authtoken"] = session.auth_token
        enc = crypto.aes_encrypt(json.dumps(payload).encode("utf-8"), session.sek)

        resp = self._request("POST", self.cfg["API_PATH"], headers,
                             {"action": action, "data": enc}, timeout=60)
        body = self._parse(resp)

        if self._status(body) != "1":
            details = json.dumps(body).lower()
            if _retry and any(k in details for k in ("token", "authtoken", "unauthor")):
                self._get_session(force=True)
                return self._action(action, payload, _retry=False)
            raise EwbError(f"EWB {action} failed",
                           error_details=self._decode_error(body),
                           status_code=resp.status_code)

        data = body.get("data") or body.get("Data")
        if not data:
            return body
        return json.loads(crypto.aes_decrypt(data, session.sek))

    # -- write operations ---------------------------------------------------

    def generate_ewb(self, payload: dict) -> dict:
        """GENEWAYBILL — full e-Way Bill from supply/transport details."""
        return self._action("GENEWAYBILL", payload)

    def update_part_b(self, payload: dict) -> dict:
        """VEHEWB — update Part B (vehicle/transport) of an existing EWB."""
        return self._action("VEHEWB", payload)

    def cancel_ewb(self, ewb_no, reason_code, reason_remarks) -> dict:
        """CANEWB — cancel an e-Way Bill (within 24h, if not verified)."""
        return self._action("CANEWB", {
            "ewbNo": int(ewb_no), "cancelRsnCode": int(reason_code),
            "CancelRmrk": reason_remarks,
        })

    def reject_ewb(self, ewb_no) -> dict:
        """REJEWB — reject an EWB raised against you."""
        return self._action("REJEWB", {"ewbNo": int(ewb_no)})

    def extend_validity(self, payload: dict) -> dict:
        """EXTENDVALIDITY — extend an EWB's validity."""
        return self._action("EXTENDVALIDITY", payload)

    def update_transporter(self, ewb_no, transporter_id) -> dict:
        """UPDATETRANSPORTER — assign/change the transporter on an EWB."""
        return self._action("UPDATETRANSPORTER", {
            "ewbNo": int(ewb_no), "transporterId": transporter_id,
        })

    def close_ewb(self, ewb_no, closure_date, remarks="Delivered") -> dict:
        """
        CLOSEEWB — voluntary closure of an EWB after delivery (GSTN advisory
        17.06.2026). Inputs: EWB number, closure date (dd/mm/yyyy), remarks.
        Voluntary; may be done by supplier/recipient/transporter.
        """
        payload = {"ewbNo": int(ewb_no), "remarks": remarks}
        if closure_date:
            payload["closureDate"] = closure_date
        return self._action("CLOSEEWB", payload)

    # -- GET-style reads ----------------------------------------------------

    def _get(self, path: str, _retry=True) -> dict:
        session = self._get_session()
        headers = self._base_headers()
        headers["authtoken"] = session.auth_token
        resp = self._request("GET", path, headers, timeout=30)
        body = self._parse(resp)
        if self._status(body) != "1":
            if _retry and "token" in json.dumps(body).lower():
                self._get_session(force=True)
                return self._get(path, _retry=False)
            raise EwbError("EWB read failed",
                           error_details=body.get("error") or body, status_code=resp.status_code)
        data = body.get("data") or body.get("Data")
        return json.loads(crypto.aes_decrypt(data, session.sek)) if data else body

    def get_ewb(self, ewb_no) -> dict:
        """GET a single e-Way Bill by number."""
        return self._get(f"{self.cfg['API_PATH']}/GetEwayBill?ewbNo={ewb_no}")

    def get_gstin_details(self, gstin) -> dict:
        return self._get(f"{self.cfg['API_PATH']}/GetGSTINDetails?gstin={gstin}")

    def get_transporter_details(self, trans_id) -> dict:
        return self._get(f"{self.cfg['API_PATH']}/GetTransporterDetails?transin={trans_id}")
