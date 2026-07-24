"""
NIC e-Invoice API client: authenticate, cache the session key, generate IRN.

Usage:
    client = EInvoiceClient()
    result = client.generate_irn(invoice_dict)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from . import crypto


class EInvoiceError(Exception):
    """Raised when the NIC API returns a business/validation error."""

    def __init__(self, message: str, *, error_details: Any = None, status_code: int | None = None):
        super().__init__(message)
        self.error_details = error_details
        self.status_code = status_code


@dataclass
class Session:
    auth_token: str
    sek: bytes          # raw session key bytes for AES
    expires_at: datetime


_CACHE_KEY = "einv_session"
# Refresh a little before the real expiry to avoid edge-of-expiry failures.
_EXPIRY_SAFETY = timedelta(minutes=2)


class EInvoiceClient:
    def __init__(self, gstin: str | None = None):
        """Build a client for a specific seller GSTIN.

        Defaults to settings.EINV (the primary GSTIN). If `gstin` matches an entry
        in settings.EINV_CREDENTIALS (multi-GSTIN, same PAN), that GSTIN's
        USERNAME/PASSWORD/CLIENT_* override the defaults, so the NIC call is
        authenticated as the GSTIN that actually owns the invoice.
        """
        cfg = dict(settings.EINV)
        creds = (getattr(settings, "EINV_CREDENTIALS", {}) or {}).get(gstin) if gstin else None
        if creds:
            cfg.update({k: v for k, v in creds.items() if v})
        self.cfg = cfg
        self.gstin = self.cfg.get("GSTIN") or ""
        self.hosts = self.cfg.get("BASE_URLS") or [self.cfg["BASE_URL"].rstrip("/")]
        # Cache the auth session per GSTIN so identities don't collide.
        self._cache_key = f"{_CACHE_KEY}:{self.gstin}"

    # -- low level ----------------------------------------------------------

    def _request(self, method: str, path: str, headers: dict,
                 body: dict | None = None, timeout: int = 60) -> requests.Response:
        """
        Send a request, failing over across the configured hosts on a
        *connection* error (NIC runs a load-balanced pair). An HTTP response
        (even an error status) is returned as-is — only network failures fail
        over, so a business error like 5001 is not retried against the twin.
        """
        last_exc = None
        for host in self.hosts:
            try:
                return requests.request(
                    method, f"{host}{path}", headers=headers, json=body, timeout=timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
                continue
        raise EInvoiceError(
            f"All e-invoice hosts unreachable: {self.hosts}",
            error_details=str(last_exc),
        )

    def _post(self, path: str, headers: dict, body: dict, timeout: int) -> requests.Response:
        return self._request("POST", path, headers, body, timeout)

    def _base_headers(self) -> dict[str, str]:
        # NIC header names are underscore-style and case-sensitive.
        return {
            "Content-Type": "application/json",
            "client_id": self.cfg["CLIENT_ID"],
            "client_secret": self.cfg["CLIENT_SECRET"],
            "Gstin": self.cfg["GSTIN"],
        }

    # -- authentication -----------------------------------------------------

    def _authenticate(self) -> Session:
        """Run the auth handshake and return a fresh Session."""
        app_key = crypto.generate_app_key()

        payload = {
            "UserName": self.cfg["USERNAME"],
            "Password": self.cfg["PASSWORD"],
            "AppKey": crypto.b64(app_key),
            "ForceRefreshAccessToken": False,
        }

        # Per NIC's reference code, the auth JSON must be Base64-encoded BEFORE
        # RSA encryption. NIC's server RSA-decrypts, then Base64-decodes to get
        # the JSON. Skipping the Base64 step makes that decode fail server-side
        # and yields a generic "Application Error in Auth" (5001).
        public_key = crypto.load_public_key(self.cfg["PUBLIC_KEY_PATH"])
        b64_payload = crypto.b64(json.dumps(payload).encode("utf-8")).encode("utf-8")
        encrypted_data = crypto.rsa_encrypt(b64_payload, public_key)

        resp = self._post(
            self.cfg["AUTH_PATH"],
            headers=self._base_headers(),
            body={"Data": encrypted_data},
            timeout=30,
        )
        body = self._parse(resp)

        if str(body.get("Status")) != "1":
            raise EInvoiceError(
                "Authentication failed",
                error_details=body.get("ErrorDetails") or body,
                status_code=resp.status_code,
            )

        session = self._session_from_body(body, app_key)

        ttl = max(int((session.expires_at - self._now() - _EXPIRY_SAFETY).total_seconds()), 30)
        cache.set(
            self._cache_key,
            {
                "auth_token": session.auth_token,
                "sek": crypto.b64(session.sek),
                "expires_at": session.expires_at.isoformat(),
            },
            timeout=ttl,
        )
        return session

    def _session_from_body(self, body: dict, app_key: bytes) -> Session:
        """
        Recover the Session (AuthToken + raw SEK) from an auth response body.

        The SEK must be AES-decrypted with our AppKey EXACTLY ONCE; decrypting it
        twice yields a wrong key and the server then fails with "Padding is
        invalid" on the first encrypted payload we send.

        - Standard wire format: Data is a base64 string = AES(authJSON, AppKey).
          Decrypt Data once -> JSON whose "Sek" is the plaintext base64 session
          key (just base64-decode it; do NOT decrypt again).
        - Portal/display format: Data is already a JSON object whose "Sek" field
          is itself AES-encrypted with the AppKey -> decrypt that one field.
        """
        data = body["Data"]
        if isinstance(data, str):
            # Whole Data is one AES blob; inside, "Sek" is the base64 of the raw
            # session key (plaintext) -> base64-decode it.
            auth = json.loads(crypto.aes_decrypt(data, app_key))
            sek = crypto.unb64(auth["Sek"])
        else:
            # Data is an object; its "Sek" field is AES-encrypted with our
            # AppKey and decrypts directly to the raw 32-byte session key.
            auth = data
            sek = crypto.aes_decrypt(auth["Sek"], app_key)

        expires_at = self._parse_expiry(auth.get("TokenExpiry"))
        return Session(auth_token=auth["AuthToken"], sek=sek, expires_at=expires_at)

    def _get_session(self, force: bool = False) -> Session:
        if not force:
            cached = cache.get(self._cache_key)
            if cached:
                expires_at = datetime.fromisoformat(cached["expires_at"])
                if expires_at - _EXPIRY_SAFETY > self._now():
                    return Session(
                        auth_token=cached["auth_token"],
                        sek=crypto.unb64(cached["sek"]),
                        expires_at=expires_at,
                    )
        return self._authenticate()

    # -- generic signed request --------------------------------------------

    def _signed_request(self, method: str, path: str, *, payload: dict | None = None,
                        what: str = "request", _retry: bool = True):
        """
        Make an authenticated, encrypted call and return the decrypted Data.

        - If `payload` is given it is AES-encrypted with the SEK and sent as the
          request body {"Data": ...} (POST endpoints).
        - GET endpoints pass no payload; the encrypted result still comes back
          in the response "Data" and is decrypted with the SEK.
        Re-authenticates once and retries if the token looks expired/invalid.
        """
        session = self._get_session()

        headers = self._base_headers()
        headers["user_name"] = self.cfg["USERNAME"]
        headers["AuthToken"] = session.auth_token

        body = None
        if payload is not None:
            body = {"Data": crypto.aes_encrypt(json.dumps(payload).encode("utf-8"), session.sek)}

        resp = self._request(method, path, headers, body, timeout=60)
        rbody = self._parse(resp)

        if str(rbody.get("Status")) != "1":
            if _retry and self._looks_like_auth_failure(rbody):
                self._get_session(force=True)
                return self._signed_request(method, path, payload=payload, what=what, _retry=False)
            raise EInvoiceError(
                f"{what} failed",
                error_details=self._decode_error_details(rbody),
                status_code=resp.status_code,
            )

        data = rbody.get("Data")
        if not data:
            # Some endpoints return only InfoDtls / no encrypted body.
            return {"Status": "1", "InfoDtls": rbody.get("InfoDtls")}
        return json.loads(crypto.aes_decrypt(data, session.sek))

    # -- e-Invoice operations ----------------------------------------------

    def generate_irn(self, invoice: dict) -> dict:
        """Register an invoice -> Irn, AckNo, AckDt, SignedInvoice, SignedQRCode."""
        return self._signed_request("POST", self.cfg["IRN_PATH"], payload=invoice,
                                    what="IRN generation")

    def cancel_irn(self, irn: str, reason_code: str, remarks: str) -> dict:
        """
        Cancel an IRN (within 24h). reason_code: 1-Duplicate, 2-Data entry
        mistake, 3-Order cancelled, 4-Other.
        """
        payload = {"Irn": irn, "CnlRsn": str(reason_code), "CnlRem": remarks}
        return self._signed_request("POST", self.cfg["CANCEL_PATH"], payload=payload,
                                    what="IRN cancellation")

    def get_irn_details(self, irn: str) -> dict:
        """Fetch a previously generated IRN's details by IRN."""
        return self._signed_request("GET", f"{self.cfg['IRN_PATH']}/irn/{irn}",
                                    what="Get IRN details")

    def get_irn_by_doc(self, doc_type: str, doc_no: str, doc_date: str) -> dict:
        """Fetch IRN by document details. doc_date format: dd/mm/yyyy."""
        path = (f"{self.cfg['IRN_PATH']}/irnbydocdetails"
                f"?doctype={doc_type}&docnum={doc_no}&docdate={doc_date}")
        return self._signed_request("GET", path, what="Get IRN by doc details")

    def get_rejected_irns(self, date: str) -> dict:
        """List IRNs rejected on a given date (dd/mm/yyyy)."""
        return self._signed_request("GET", f"{self.cfg['IRN_PATH']}/rejectedirns?date={date}",
                                    what="Get rejected IRNs")

    def get_gstin_details(self, gstin: str) -> dict:
        """Fetch GSTIN master details."""
        return self._signed_request("GET", f"{self.cfg['MASTER_GSTIN_PATH']}/{gstin}",
                                    what="Get GSTIN details")

    def sync_gstin(self, gstin: str) -> dict:
        """Force a fresh sync of a GSTIN from the GST common portal."""
        return self._signed_request("GET", f"{self.cfg['SYNC_GSTIN_PATH']}/{gstin}",
                                    what="Sync GSTIN")

    def generate_ewb_by_irn(self, ewb_payload: dict) -> dict:
        """
        Generate an e-Way Bill for an existing IRN. ewb_payload carries Irn +
        transport details (Distance, TransMode, TransId, VehNo, etc.).
        """
        return self._signed_request("POST", self.cfg["EWB_PATH"], payload=ewb_payload,
                                    what="Generate e-Way Bill by IRN")

    def get_ewb_by_irn(self, irn: str) -> dict:
        """Fetch the e-Way Bill linked to an IRN."""
        return self._signed_request("GET", f"{self.cfg['EWB_PATH']}/irn/{irn}",
                                    what="Get e-Way Bill by IRN")

    def health_ping(self) -> dict:
        """Unencrypted heartbeat — no auth required."""
        resp = self._request("GET", self.cfg["HEARTBEAT_PATH"], self._base_headers(), timeout=15)
        try:
            return resp.json()
        except ValueError:
            return {"status_code": resp.status_code, "text": resp.text[:300]}

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            raise EInvoiceError(
                f"Non-JSON response from NIC (HTTP {resp.status_code})",
                error_details=resp.text[:1000],
                status_code=resp.status_code,
            )

    @staticmethod
    def _decode_error_details(body: dict):
        """
        The IRN endpoint returns ErrorDetails as a base64 string that decodes to
        a JSON array of {ErrorCode, ErrorMessage}; the auth endpoint returns it
        as a plain array. Handle both, falling back to InfoDtls/whole body.
        """
        ed = body.get("ErrorDetails")
        if isinstance(ed, str) and ed:
            try:
                return json.loads(crypto.unb64(ed))
            except Exception:
                return ed
        return ed or body.get("InfoDtls") or body

    @staticmethod
    def _looks_like_auth_failure(body: dict) -> bool:
        details = json.dumps(body.get("ErrorDetails") or body).lower()
        return any(k in details for k in ("authtoken", "token", "unauthor", "1005", "1006"))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _parse_expiry(self, raw: str | None) -> datetime:
        """
        TokenExpiry is local IST like '2024-01-01 12:34:56'. If we can't parse
        it, fall back to a conservative 6-hour lifetime.
        """
        if raw:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    naive = datetime.strptime(raw, fmt)
                    # Treat as IST (UTC+5:30) and convert to UTC.
                    ist = timezone(timedelta(hours=5, minutes=30))
                    return naive.replace(tzinfo=ist).astimezone(timezone.utc)
                except ValueError:
                    continue
        return self._now() + timedelta(hours=6)
