"""
Pre-submit validation for the standalone GENEWAYBILL payload (camelCase EWB API).

Catches the common NIC EWB rejections locally before the round-trip: missing
doc/party fields, bad GSTIN/state/pincode formats, and the transport rule
(Part-B needs a vehicle for road, or a transporter for Part-A only). Returns a
list of {field, code, message} (empty == valid); never raises.

EWB-by-IRN payloads are validated by einvoice.validation.validate_ewb_by_irn.
"""
from __future__ import annotations

from einvoice.validation import RE_DATE, RE_GSTIN, RE_HSN, RE_PIN

_TRANS_MODES = {"1", "2", "3", "4"}       # Road, Rail, Air, Ship
_SUB_SUPPLY = {"1", "2", "3", "4", "5", "6", "7", "8"}


def _s(v):
    return "" if v is None else str(v)


def validate_genewaybill(p: dict) -> list[dict]:
    errors: list[dict] = []

    def err(field, code, message):
        errors.append({"field": field, "code": code, "message": message})

    if not isinstance(p, dict) or not p:
        return [{"field": "(root)", "code": "EMPTY", "message": "GENEWAYBILL payload must be a non-empty JSON object."}]

    # ---- doc ----
    if not p.get("docNo"):
        err("docNo", "MISSING", "docNo is required.")
    if not p.get("docDate"):
        err("docDate", "MISSING", "docDate is required.")
    elif not RE_DATE.match(_s(p.get("docDate"))):
        err("docDate", "BAD_DATE", "docDate must be dd/mm/yyyy.")

    # ---- parties ----
    frm = _s(p.get("fromGstin"))
    to = _s(p.get("toGstin"))
    if frm and frm != "URP" and not RE_GSTIN.match(frm):
        err("fromGstin", "BAD_GSTIN", "fromGstin format is invalid.")
    if not to:
        err("toGstin", "MISSING", "toGstin is required (use 'URP' if unregistered).")
    elif to != "URP" and not RE_GSTIN.match(to):
        err("toGstin", "BAD_GSTIN", "toGstin format is invalid (or use 'URP').")

    for f in ("fromStateCode", "toStateCode", "actFromStateCode", "actToStateCode"):
        if p.get(f) in (None, ""):
            err(f, "MISSING", f"{f} is required (numeric GST state code).")
    for f in ("fromPincode", "toPincode"):
        if p.get(f) in (None, "") or not RE_PIN.match(_s(p.get(f))):
            err(f, "BAD_PIN", f"{f} must be 6 digits.")
    for f in ("fromPlace", "toPlace", "fromAddr1", "toAddr1"):
        if not p.get(f):
            err(f, "MISSING", f"{f} is required.")

    # ---- supply classification ----
    if _s(p.get("supplyType")) not in ("I", "O"):
        err("supplyType", "BAD_SUPPLY", "supplyType must be 'O' (outward) or 'I' (inward).")
    if _s(p.get("subSupplyType")) not in _SUB_SUPPLY:
        err("subSupplyType", "BAD_SUBSUPPLY", "subSupplyType must be 1-8 (1=Supply, 3=Export, ...).")
    if _s(p.get("transactionType")) not in ("1", "2", "3", "4"):
        err("transactionType", "BAD_TXN", "transactionType must be 1-4.")

    # ---- items ----
    items = p.get("itemList")
    if not isinstance(items, list) or not items:
        err("itemList", "MISSING_ITEMS", "itemList must have at least one item.")
    else:
        for i, it in enumerate(items):
            hsn = _s(it.get("hsnCode"))
            if not hsn:
                err(f"itemList[{i}].hsnCode", "HSN_REQUIRED", "HSN code is mandatory. [2176]")
            elif not RE_HSN.match(hsn):
                err(f"itemList[{i}].hsnCode", "BAD_HSN", "HSN must be 4-8 digits.")

    # ---- totals ----
    if p.get("totInvValue") in (None, ""):
        err("totInvValue", "MISSING", "totInvValue is required.")

    # ---- transport ----
    tmode = _s(p.get("transMode"))
    if tmode and tmode not in _TRANS_MODES:
        err("transMode", "BAD_TRANSMODE", "transMode must be 1-Road, 2-Rail, 3-Air, 4-Ship.")
    dist = p.get("transDistance")
    if dist in (None, ""):
        err("transDistance", "MISSING", "transDistance is required (send 0 to let NIC auto-compute by pincodes).")
    veh = _s(p.get("vehicleNo"))
    tid = _s(p.get("transporterId"))
    # Road movement needs a vehicle (Part B); otherwise a transporter ID (Part A) is
    # required so the EWB can be raised and Part B updated later.
    if tmode == "1" and not veh and not tid:
        err("vehicleNo", "PARTB_REQUIRED",
            "Road transport needs a vehicleNo (Part B) or a transporterId (Part A). [Movement invalid otherwise]")
    if tid and len(tid) != 15:
        err("transporterId", "BAD_TRANSID", "transporterId must be a 15-char GSTIN/Transporter ID.")
    if p.get("transDocDate") and not RE_DATE.match(_s(p.get("transDocDate"))):
        err("transDocDate", "BAD_DATE", "transDocDate must be dd/mm/yyyy.")

    return errors
