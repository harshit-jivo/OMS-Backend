"""
Fetch a SAP Business One invoice (OINV / `Invoices` entity) and its HSN codes
from the Service Layer, for mapping into an IRN payload (see einvoice.mapping).

Reuses serviceLayer.SAPServiceLayerManager (the cached shared session) for the
default company DB. When a specific `company_db` is requested (e.g. the live
JIVO_OIL_HANADB while the app default points elsewhere), it does a dedicated,
uncached login to that company so the fetch is explicit and side-effect free.
"""
from __future__ import annotations

import logging

import requests
import urllib3
from django.conf import settings

from serviceLayer.service import SAPServiceLayerManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class SapFetchError(Exception):
    """Raised when the invoice (or HSN) could not be fetched from Service Layer."""


def _base() -> str:
    return settings.HANA_SERVICE_LAYER_URL.rstrip("/")


def _verify():
    return getattr(settings, "HANA_SSL_VERIFY", False)


def _timeout():
    return (getattr(settings, "HANA_CONNECT_TIMEOUT", 15), getattr(settings, "HANA_READ_TIMEOUT", 120))


def get_session(company_db: str | None = None) -> requests.Session:
    """Return a logged-in Service Layer session for `company_db`.

    None / the app default -> the cached shared session. A non-default company_db
    -> a fresh (uncached) login, so callers can target the live company DB
    without disturbing the shared cache.
    """
    if not company_db or company_db == settings.HANA_COMPANY_DB:
        return SAPServiceLayerManager.get_session()

    session = requests.Session()
    session.verify = _verify()
    try:
        resp = requests.post(
            f"{_base()}/Login",
            json={"CompanyDB": company_db, "UserName": settings.HANA_USERNAME,
                  "Password": settings.HANA_PASSWORD},
            verify=_verify(), timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise SapFetchError(f"Service Layer login to {company_db} failed: {exc}") from exc
    if resp.status_code != 200:
        raise SapFetchError(f"Service Layer login to {company_db} failed "
                            f"({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    session.cookies.set("B1SESSION", data.get("SessionId"))
    route = resp.cookies.get("ROUTEID")
    if route:
        session.cookies.set("ROUTEID", route)
    return session


def fetch_invoice(docentry: int, company_db: str | None = None, session=None) -> dict:
    """GET Invoices(DocEntry) and return the full invoice dict."""
    session = session or get_session(company_db)
    try:
        resp = session.get(f"{_base()}/Invoices({int(docentry)})",
                           verify=_verify(), timeout=_timeout())
    except requests.RequestException as exc:
        raise SapFetchError(f"Failed to fetch invoice {docentry}: {exc}") from exc
    if resp.status_code != 200:
        raise SapFetchError(f"Invoice {docentry} fetch returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def resolve_hsn(entries, company_db: str | None = None, session=None) -> dict:
    """Map SAP line HSNEntry (OHSN AbsEntry) -> HSN code string via the IndiaHsn
    entity. The HSN code is ChapterID with the dots stripped (e.g. '1514.99.90'
    -> '15149990'). Best-effort: unresolved entries are simply absent from the map.
    """
    session = session or get_session(company_db)
    out = {}
    for ent in {e for e in entries if e not in (None, "", -1)}:
        try:
            resp = session.get(f"{_base()}/IndiaHsn({int(ent)})", verify=_verify(), timeout=_timeout())
            if resp.status_code == 200:
                j = resp.json()
                code = (str(j.get("ChapterID") or "").replace(".", "")
                        or f"{j.get('Chapter', '')}{j.get('Heading', '')}{j.get('SubHeading', '')}")
                if code:
                    out[ent] = code
        except (requests.RequestException, ValueError):
            logger.warning("Could not resolve HSN for IndiaHsn(%s)", ent)
    return out


def fetch_invoice_for_irn(docentry: int, company_db: str | None = None):
    """Fetch an invoice and resolve all its line HSN codes in one session.
    Returns (invoice_dict, hsn_map)."""
    session = get_session(company_db)
    invoice = fetch_invoice(docentry, session=session)
    entries = [ln.get("HSNEntry") for ln in (invoice.get("DocumentLines") or [])]
    hsn_map = resolve_hsn(entries, session=session)
    return invoice, hsn_map
