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
from urllib.parse import quote

import requests
import urllib3
from django.conf import settings

from einvoice.mapping import gstin as _gstin
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

    Known company DBs (OIL / BEVERAGE / MART) go through SAPServiceLayerManager,
    whose session cache is keyed PER COMPANY — so an OIL session is never handed
    to a BEVERAGE caller. Any other company DB (a TEST_* copy, say) gets a
    dedicated, uncached login.
    """
    known = {
        settings.HANA_OIL_COMPANY_DB: "OIL",
        getattr(settings, "HANA_BEVERAGE_COMPANY_DB", None): "BEVERAGE",
        getattr(settings, "HANA_MART_COMPANY_DB", None): "MART",
    }
    known.pop(None, None)
    if not company_db:
        return SAPServiceLayerManager.get_session("OIL")
    if company_db in known:
        return SAPServiceLayerManager.get_session(known[company_db])

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


def company_choices() -> list[dict]:
    """Selectable companies: [{label, company_db}] — the OIL / BEVERAGE / MART
    company DBs actually configured in settings. Drives the UI's company picker
    so the user can choose which DB an IRN is generated against and mirrored
    into. A company with no DB configured simply doesn't appear."""
    pairs = [
        ("OIL", getattr(settings, "HANA_OIL_COMPANY_DB", "")),
        ("BEVERAGE", getattr(settings, "HANA_BEVERAGE_COMPANY_DB", "")),
        ("MART", getattr(settings, "HANA_MART_COMPANY_DB", "")),
    ]
    out, seen = [], set()
    for label, db in pairs:
        db = (db or "").strip()
        if db and db not in seen:
            seen.add(db)
            out.append({"label": label, "company_db": db})
    return out


def _known_company_dbs() -> list[str]:
    """The company DBs a DocNum search may scan. Configurable via
    settings.EINV_COMPANY_DBS; the configured default is always tried first."""
    dbs = list(getattr(settings, "EINV_COMPANY_DBS", None)
               or [c["company_db"] for c in company_choices()])
    default = settings.HANA_OIL_COMPANY_DB
    if default:
        dbs = [default] + [d for d in dbs if d != default]
    return dbs


def _docentry_for_docnum(docnum, company_db, session) -> int | None:
    """Return the DocEntry for a DocNum in one company DB, or None if absent.
    Best-effort: login / query failures return None (so a scan can continue)."""
    try:
        resp = session.get(
            f"{_base()}/Invoices?$select=DocEntry&$filter=DocNum eq {int(docnum)}&$top=1",
            verify=_verify(), timeout=_timeout(),
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    rows = resp.json().get("value", [])
    return int(rows[0]["DocEntry"]) if rows else None


def find_invoice_by_docnum(docnum, prefer_db: str | None = None):
    """Search company DBs for an invoice DocNum. Tries `prefer_db` first, then the
    other known DBs. Returns (docentry, company_db) of the first match, or raises
    SapFetchError listing everywhere it looked."""
    order: list[str] = []
    for db in ([prefer_db] if prefer_db else []) + _known_company_dbs():
        if db and db not in order:
            order.append(db)

    searched = []
    for db in order:
        try:
            session = get_session(db)
        except SapFetchError:
            searched.append(f"{db} (login failed)")
            continue
        de = _docentry_for_docnum(docnum, db, session)
        if de is not None:
            return de, db
        searched.append(db)

    raise SapFetchError(
        f"No invoice found with DocNum {docnum}. Searched: {', '.join(searched)}."
    )


def resolve_invoice_ref(identifier, id_type: str = "docentry", company_db: str | None = None):
    """Resolve (docentry, company_db) from a DocEntry or a visible DocNum.

    id_type="docentry" (default) -> identifier IS the DocEntry, in `company_db`.
    id_type="docnum"             -> search for the DocNum (prefer `company_db`,
                                    then scan the other known company DBs) and
                                    return where it was actually found.
    """
    if (id_type or "docentry").lower() != "docnum":
        return int(identifier), company_db
    return find_invoice_by_docnum(identifier, prefer_db=company_db)


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


def _get_json(session, path):
    """GET a Service Layer path, returning the JSON body or None. Best-effort:
    these are enrichment lookups and must never fail an invoice fetch."""
    try:
        resp = session.get(f"{_base()}{path}", verify=_verify(), timeout=_timeout())
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _joined(*parts):
    """Join address fragments, dropping empties. None when nothing is left."""
    joined = " ".join(str(p).strip() for p in parts if str(p or "").strip())
    return joined or None


def _branch_dispatch_address(invoice: dict, bpl_id, session) -> dict:
    """Where the branch that issued this invoice actually dispatches from.

    The goods-issuing warehouse is tried first — it is what SAP itself uses to
    build DispatchFrom* on a correctly-populated document, and it carries a real
    street/city, whereas the BusinessPlaces record here holds little more than a
    PIN. Only a warehouse belonging to that same branch is accepted. Returns
    {"addr1", "loc", "pin"} with whatever could not be resolved left as None.
    """
    seen, codes = set(), []
    for ln in (invoice.get("DocumentLines") or []):
        code = str(ln.get("WarehouseCode") or "").strip()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)

    for code in codes:
        wh = _get_json(session, f"/Warehouses('{quote(code)}')")
        if not wh:
            continue
        if bpl_id is not None and wh.get("BusinessPlaceID") != bpl_id:
            continue    # a warehouse of a different branch would be the wrong state
        addr1 = _joined(wh.get("Block"), wh.get("BuildingFloorRoom"), wh.get("Street"))
        if addr1 or wh.get("City"):
            return {"addr1": addr1, "loc": wh.get("City"), "pin": wh.get("ZipCode")}

    bp = _get_json(session, f"/BusinessPlaces({int(bpl_id)})") if bpl_id is not None else None
    if bp:
        return {"addr1": _joined(bp.get("Block"), bp.get("Building"), bp.get("Street")),
                "loc": bp.get("City"), "pin": bp.get("ZipCode")}
    return {"addr1": None, "loc": None, "pin": None}


def normalize_seller_branch(invoice: dict, session) -> bool:
    """Repair EWayBillDetails.BillFrom*/DispatchFrom* in place when SAP populated
    them from the wrong registration. Returns True when something was changed.

    SAP fills that block from the MAIN business place rather than the branch
    (BPL) that issued the document. On a branch-to-branch invoice — customer =
    another GST registration of the same legal entity — it comes back holding the
    RECIPIENT's GSTIN and address, which makes the e-invoice self-dealing (NIC
    2211 "supplier and recipient GSTIN must not be the same") and flips the supply
    from inter- to intra-state. The document's own VATRegNum is the issuing
    branch's GSTIN and is authoritative, so it decides whether the block is stale.

    Both blocks are rewritten together: keeping the old dispatch address under a
    corrected GSTIN would just trade error 2211 for 2258/2231 (state / PIN not
    matching the GSTIN).
    """
    ewb = invoice.get("EWayBillDetails")
    doc_gstin = _gstin(invoice.get("VATRegNum"))
    if not ewb or not doc_gstin or _gstin(ewb.get("BillFromGSTIN")) == doc_gstin:
        return False

    bpl_id = invoice.get("BPL_IDAssignedToInvoice")
    addr = _branch_dispatch_address(invoice, bpl_id, session)
    logger.warning(
        "Invoice %s: SAP BillFromGSTIN %r is not the issuing branch's GSTIN %s "
        "(BPL %s) — rebuilding the seller block from the branch.",
        invoice.get("DocEntry"), ewb.get("BillFromGSTIN"), doc_gstin, bpl_id)

    ewb["BillFromGSTIN"] = doc_gstin
    ewb["BillFromStateGSTCode"] = doc_gstin[:2]
    # Always overwritten, even with None: a leftover address from the other
    # registration is worse than an absent one, which the pre-submit validator
    # reports as a missing mandatory field.
    ewb["DispatchFromAddress1"] = addr["addr1"]
    ewb["DispatchFromAddress2"] = None
    ewb["DispatchFromPlace"] = addr["loc"]
    ewb["DispatchFromZipCode"] = addr["pin"]
    ewb["DispatchFromStateGSTCode"] = doc_gstin[:2]
    return True


# BP address GST registration types that may stand in for a missing document
# GSTIN. Only a plain registered address qualifies. Measured on the live Oil
# books (CRD1."GSTType"): 9,189 addresses are type 1 "Regular/TDS/ISD" — the
# Service Layer's `gstRegularTDSISD` — and every one of them carries a
# well-formed 15-char GSTIN; 3,719 are null with no GSTIN at all (genuinely
# unregistered, where URP is the correct answer); 2 are type 6, a diplomatic
# mission's UIN. A UIN is 15 chars and would pass the format check, so it is
# deliberately NOT in this set — asserting one as a B2B recipient GSTIN is a
# call for a human, not a silent fallback.
_REGISTERED_GST_TYPES = {"gstRegularTDSISD"}


def _addr_key(name):
    """Normalise an address name for matching. SAP stores PayToCode/ShipToCode as
    the address name verbatim, so this only guards against stray whitespace/case."""
    return " ".join(str(name or "").split()).upper() or None


def _bp_addresses(card_code, session) -> list:
    """The BP's address rows (CRD1) with their GSTINs. Best-effort: [] on failure."""
    if not card_code:
        return []
    bp = _get_json(session, f"/BusinessPartners('{quote(str(card_code))}')?$select=BPAddresses")
    return (bp or {}).get("BPAddresses") or []


def _master_gstin(addresses, address_name, address_type, expected_state, *, docentry, role):
    """The master GSTIN for EXACTLY the address this document used, or None.

    Matched on the document's own PayToCode / ShipToCode against
    BPAddresses.AddressName + AddressType — never "some GSTIN on this card". A
    customer registered in several states has one address per registration, and
    picking the wrong one produces a valid-looking but wrong IRN.

    Three gates, all of which must pass:
      * the address carries a well-formed GSTIN,
      * its GstType is a registered one (see _REGISTERED_GST_TYPES),
      * its state prefix agrees with the state code SAP put on the document —
        a disagreement means the address match went wrong, and sending it would
        earn NIC 2265 (recipient GSTIN state != recipient state code) anyway.
    """
    wanted = _addr_key(address_name)
    if not wanted:
        return None

    for addr in addresses:
        if addr.get("AddressType") != address_type or _addr_key(addr.get("AddressName")) != wanted:
            continue

        found = _gstin(addr.get("GSTIN"))
        if not found:
            return None                      # unregistered address: URP is correct

        gst_type = addr.get("GstType")
        if gst_type not in _REGISTERED_GST_TYPES:
            logger.warning(
                "Invoice %s: %s address %r holds GSTIN %s but its GstType is %r, "
                "not a plain registered type — not substituting; resolve this on the document.",
                docentry, role, address_name, found, gst_type)
            return None

        state = str(expected_state or "").zfill(2) if expected_state else None
        if state and found[:2] != state:
            logger.warning(
                "Invoice %s: %s address %r holds GSTIN %s, whose state %s does not match "
                "the document's %s state code %s — not substituting.",
                docentry, role, address_name, found, found[:2], role, state)
            return None
        if not state:
            logger.warning(
                "Invoice %s: no %s state code on the document, so master GSTIN %s was "
                "accepted on the address match alone.", docentry, role, found)
        return found

    return None


def normalize_buyer_gstin(invoice: dict, session) -> dict:
    """Fill EWayBillDetails.BillToGSTIN / ShipToGSTIN in place from the BP address
    master when the document itself carries none. Returns {field: gstin} for what
    was recovered — empty when nothing needed doing.

    The document is tried first and always wins; this only runs on what it left
    blank. Two things leave it blank:

    * `BillToGSTIN` — SAP derives it from `INV12."BpGSTN"`, stamped onto the
      document when it is added, and substitutes the literal "URP" when that
      column is null. A document that missed the stamp therefore reads as an
      unregistered buyer and `validation` refuses it as B2C_NOT_ELIGIBLE, even
      though the customer is registered. Seen once on these books (DocNum
      626080524): 1 of 1,608 invoices since 2026-06-01.
    * `ShipToGSTIN` — this Service Layer version has no such property on
      EWayBillDetails at all (it returns explicit nulls for fields it does have),
      so the master is the ONLY source. Without it `mapping` can never emit
      ShipDtls, which the GSTN advisory of 17.06.2026 requires from 01/08/2026
      wherever ship details accompany an e-way bill.

    Reading the master here does not put the IRN out of step with the rest of the
    stack — it is where the stack already looks. Both the Crystal bill print
    (`OMS_SP_GST_INVOICE`, which never mentions INV12) and the GSTR-1 extracts
    (`GSTR1_B2B` reports CRD1."GSTRegnNo" directly; `GSTR1_B2CS` resolves
    IFNULL(INV12."BpGSTN", CRD1."GSTRegnNo") and drops anything non-blank) join
    CRD1 on CardCode + PayToCode/ShipToCode + AdresType, exactly as this does.
    """
    ewb = invoice.get("EWayBillDetails")
    if not ewb:
        return {}

    # Exports carry no Indian GSTIN: "URP" is the right answer and mapping sets it.
    axe = invoice.get("AddressExtension") or {}
    if str(axe.get("BillToCountry") or "IN").upper() != "IN":
        return {}

    need_bill = _gstin(ewb.get("BillToGSTIN")) is None
    need_ship = _gstin(ewb.get("ShipToGSTIN")) is None
    if not (need_bill or need_ship):
        return {}

    docentry = invoice.get("DocEntry")
    addresses = _bp_addresses(invoice.get("CardCode"), session)
    if not addresses:
        logger.warning("Invoice %s: no BP addresses readable for %s — leaving the buyer "
                       "block as SAP returned it.", docentry, invoice.get("CardCode"))
        return {}

    recovered = {}
    if need_bill:
        found = _master_gstin(addresses, invoice.get("PayToCode"), "bo_BillTo",
                              ewb.get("BillToStateGSTCode"), docentry=docentry, role="bill-to")
        if found:
            logger.warning(
                "Invoice %s: SAP returned BillToGSTIN %r (the document's INV12.BpGSTN is "
                "empty) but the bill-to address %r is registered as %s — using the master "
                "GSTIN. The SAP document is still blank and should be corrected.",
                docentry, ewb.get("BillToGSTIN"), invoice.get("PayToCode"), found)
            ewb["BillToGSTIN"] = found
            recovered["BillToGSTIN"] = found

    if need_ship:
        found = _master_gstin(addresses, invoice.get("ShipToCode"), "bo_ShipTo",
                              ewb.get("ShipToStateGSTCode") or ewb.get("BillToStateGSTCode"),
                              docentry=docentry, role="ship-to")
        if found:
            logger.info("Invoice %s: ship-to GSTIN %s resolved from the address master "
                        "(EWayBillDetails carries no ShipToGSTIN).", docentry, found)
            ewb["ShipToGSTIN"] = found
            recovered["ShipToGSTIN"] = found

    return recovered


def fetch_invoice_for_irn(docentry: int, company_db: str | None = None, session=None):
    """Fetch an invoice, repair its seller and buyer blocks and resolve all its
    line HSN codes in one session. Returns (invoice_dict, hsn_map)."""
    session = session or get_session(company_db)
    invoice = fetch_invoice(docentry, session=session)
    normalize_seller_branch(invoice, session)
    normalize_buyer_gstin(invoice, session)
    entries = [ln.get("HSNEntry") for ln in (invoice.get("DocumentLines") or [])]
    hsn_map = resolve_hsn(entries, session=session)
    return invoice, hsn_map


def _ddmmyyyy_to_iso(dt):
    """NIC AckDt 'yyyy-mm-dd HH:MM:SS' -> SAP-friendly 'yyyy-mm-dd'."""
    if not dt:
        return None
    return str(dt)[:10]


def write_irn_to_invoice(docentry: int, result: dict, company_db: str | None = None) -> bool:
    """
    Option B — write the IRN response back onto the SAP invoice's e-Billing
    protocol (so SAP itself shows the invoice as e-invoiced). PATCHes
    /Invoices(docentry).ElectronicProtocols[edpc_EBilling]. Best-effort.

    NOTE: whether these fields are patchable on a posted invoice depends on the
    SAP B1 version / GST add-on config — validate on the sandbox company before
    enabling in production. Returns True on HTTP success.
    """
    session = get_session(company_db)
    ebilling = {
        "ProtocolCode": "edpc_EBilling",
        "EBillingIRN": result.get("Irn"),
        "EBillingAckNo": str(result.get("AckNo")) if result.get("AckNo") is not None else None,
        "EBillingAckDt": _ddmmyyyy_to_iso(result.get("AckDt")),
        "EBillingSignedInvoice": result.get("SignedInvoice"),
        "EBillingSignedQRCode": result.get("SignedQRCode"),
        "EBillingResponseStatus": str(result.get("Status") or "ACT"),
    }
    body = {"ElectronicProtocols": [{k: v for k, v in ebilling.items() if v is not None}]}
    try:
        resp = session.patch(f"{_base()}/Invoices({int(docentry)})",
                             json=body, verify=_verify(), timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("SAP write-back PATCH failed for DocEntry %s: %s", docentry, exc)
        return False
    if resp.status_code in (200, 204):
        return True
    logger.warning("SAP write-back for DocEntry %s returned %s: %s",
                   docentry, resp.status_code, resp.text[:300])
    return False
