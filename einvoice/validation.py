"""
Pre-submit validation for the NIC e-Invoice (IRN) payload.

Validates the request JSON against the GSTN field regexes AND the arithmetic /
business rules the IRP enforces (with the ±1 rupee tolerance) BEFORE calling
NIC — per the "validate before requesting" best practice. Catching these locally
avoids IRN failures (errors 2172/2182/2193/2194/2211/2227/2258/2265, ...) and is
much faster than a round-trip rejection.

Returns a list of {"field", "code", "message"} dicts (empty == valid). It never
raises; the caller decides what to do with the errors.

Reference: einvoice/docs/NIC_EINVOICE_EWAYBILL_REFERENCE.md §4, §6.
"""
from __future__ import annotations

import re

# ---- tolerances -----------------------------------------------------------
RUPEE_TOL = 1.0     # IRP recomputes amounts within ±1 rupee
EQ_EPS = 0.01       # for "must be exactly equal" checks (CGST == SGST)

# ---- regexes (GSTN published) --------------------------------------------
RE_DOC_NO = re.compile(r"^[a-zA-Z1-9]{1}[a-zA-Z0-9/-]{0,15}$")
RE_DATE = re.compile(r"^[0-3][0-9]/[0-1][0-9]/20[1-9][0-9]$")   # dd/mm/yyyy
RE_GSTIN = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")
RE_STATE = re.compile(r"^[0-9]{1,2}$")
RE_PHONE = re.compile(r"^[0-9]{6,12}$")
RE_EMAIL = re.compile(r"^[a-zA-Z0-9+_.-]+@[a-zA-Z0-9.-]+$")
RE_HSN = re.compile(r"^[0-9]{4,8}$")
RE_PIN = re.compile(r"^[0-9]{6}$")

SUP_TYPES = {"B2B", "SEZWP", "SEZWOP", "EXPWP", "EXPWOP", "DEXP"}
DOC_TYPES = {"INV", "CRN", "DBN"}
EXPORT_SEZ_TYPES = {"SEZWP", "SEZWOP", "EXPWP", "EXPWOP", "DEXP"}
EXPORT_TYPES = {"EXPWP", "EXPWOP"}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---- party field length limits (NIC master codes / e-Invoice schema) ------
# (min, max) character counts. NIC rejects out-of-range strings, most commonly
# Addr1 > 100 (error 5002) when a long SAP dispatch address is not split.
_PARTY_LEN = {
    "LglNm": (3, 100),
    "TrdNm": (3, 100),
    "Addr1": (1, 100),
    "Addr2": (1, 100),
    "Loc": (2, 60),
}


def _check_party_lengths(block, prefix, err):
    """Report any party string field whose length is outside the NIC limits."""
    for field, (lo, hi) in _PARTY_LEN.items():
        v = block.get(field)
        if v in (None, ""):
            continue
        n = len(str(v))
        if n < lo or n > hi:
            err(f"{prefix}.{field}", "FIELD_LENGTH",
                f"{prefix}.{field} must be {lo}-{hi} chars (got {n}). "
                f"{'Split a long address across Addr1/Addr2. [5002]' if field.startswith('Addr') else ''}".strip())


def _check_party_required(block, prefix, fields, err):
    """Report NIC-mandatory party fields that are missing/empty (would come back
    as error 5002 — 'The X field is required')."""
    for field in fields:
        if block.get(field) in (None, ""):
            err(f"{prefix}.{field}", "FIELD_REQUIRED",
                f"{prefix}.{field} is mandatory and could not be mapped from SAP. [5002]")


def validate_invoice(invoice: dict) -> list[dict]:
    errors: list[dict] = []

    def err(field, code, message):
        errors.append({"field": field, "code": code, "message": message})

    if not isinstance(invoice, dict) or not invoice:
        return [{"field": "(root)", "code": "EMPTY", "message": "Invoice payload must be a non-empty JSON object."}]

    tran = invoice.get("TranDtls") or {}
    doc = invoice.get("DocDtls") or {}
    seller = invoice.get("SellerDtls") or {}
    buyer = invoice.get("BuyerDtls") or {}
    items = invoice.get("ItemList")
    val = invoice.get("ValDtls") or {}

    # ---- mandatory blocks -------------------------------------------------
    for name, block in (("TranDtls", tran), ("DocDtls", doc), ("SellerDtls", seller),
                        ("BuyerDtls", buyer), ("ValDtls", val)):
        if not block:
            err(name, "MISSING_BLOCK", f"Mandatory block '{name}' is missing or empty.")
    if not isinstance(items, list) or not items:
        err("ItemList", "MISSING_ITEMS", "ItemList is mandatory and must have at least one item.")
        items = []
    elif len(items) > 1000:
        err("ItemList", "TOO_MANY_ITEMS", "ItemList cannot exceed 1000 items.")

    # ---- TranDtls ---------------------------------------------------------
    sup_typ = tran.get("SupTyp")
    if sup_typ and sup_typ not in SUP_TYPES:
        err("TranDtls.SupTyp", "BAD_SUPTYP", f"SupTyp '{sup_typ}' not in {sorted(SUP_TYPES)}.")
    igst_on_intra = str(tran.get("IgstOnIntra", "N")).upper() == "Y"
    if igst_on_intra and str(tran.get("RegRev", "N")).upper() != "Y":
        err("TranDtls.RegRev", "IGST_INTRA_NEEDS_RCM", "IgstOnIntra=Y requires reverse charge (RegRev=Y). [2294]")

    # ---- DocDtls ----------------------------------------------------------
    if doc.get("Typ") and doc["Typ"] not in DOC_TYPES:
        err("DocDtls.Typ", "BAD_DOCTYPE", f"Doc type '{doc['Typ']}' not in {sorted(DOC_TYPES)}.")
    if doc.get("No") and not RE_DOC_NO.match(str(doc["No"])):
        err("DocDtls.No", "BAD_DOC_NO", "Doc No: <=16 chars, alphanumeric plus / and -, cannot start with 0, / or -.")
    if doc.get("Dt") and not RE_DATE.match(str(doc["Dt"])):
        err("DocDtls.Dt", "BAD_DOC_DATE", "Doc date must be dd/mm/yyyy.")

    # ---- Seller / Buyer GSTIN + state -------------------------------------
    s_gstin = str(seller.get("Gstin", "") or "")
    if s_gstin and not RE_GSTIN.match(s_gstin):
        err("SellerDtls.Gstin", "BAD_GSTIN", "Supplier GSTIN format is invalid.")
    if seller.get("Stcd") and s_gstin[:2] and str(seller["Stcd"]).zfill(2) != s_gstin[:2]:
        err("SellerDtls.Stcd", "STATE_MISMATCH", "Supplier state code must match the first 2 digits of the GSTIN. [2258]")
    if seller.get("Pin") and not RE_PIN.match(str(seller["Pin"])):
        err("SellerDtls.Pin", "BAD_PIN", "Supplier PIN must be 6 digits.")
    _check_party_required(seller, "SellerDtls", ("Gstin", "LglNm", "Addr1", "Loc", "Pin", "Stcd"), err)
    _check_party_required(buyer, "BuyerDtls", ("Gstin", "LglNm", "Pos", "Addr1", "Loc", "Pin", "Stcd"), err)
    _check_party_lengths(seller, "SellerDtls", err)
    _check_party_lengths(buyer, "BuyerDtls", err)

    b_gstin = str(buyer.get("Gstin", "") or "")
    is_export = sup_typ in EXPORT_TYPES
    if b_gstin and b_gstin != "URP" and not RE_GSTIN.match(b_gstin):
        err("BuyerDtls.Gstin", "BAD_GSTIN", "Recipient GSTIN format is invalid (or use 'URP').")
    if b_gstin == "URP" and not is_export:
        err("BuyerDtls.Gstin", "B2C_NOT_ELIGIBLE",
            "Recipient is unregistered (URP) on a domestic supply — this is a B2C invoice and is "
            "NOT eligible for e-Invoice/IRN (the IRP handles B2B, SEZ and Export only). "
            "Skip IRN generation for this invoice. [2212]")
    if s_gstin and b_gstin and s_gstin == b_gstin:
        err("BuyerDtls.Gstin", "SAME_GSTIN", "Supplier and recipient GSTIN must not be the same. [2211]")
    if buyer.get("Pos") and not RE_STATE.match(str(buyer["Pos"])):
        err("BuyerDtls.Pos", "BAD_POS", "Place of supply must be a numeric state code.")

    # export sanity
    if is_export:
        if b_gstin and b_gstin != "URP":
            err("BuyerDtls.Gstin", "EXPORT_URP", "For exports, recipient GSTIN should be 'URP'.")
        if str(buyer.get("Pos", "")) not in ("", "96"):
            err("BuyerDtls.Pos", "EXPORT_POS", "For exports, POS should be 96 (other territory).")

    # ---- Ship-to GSTIN (GSTN advisory 17.06.2026, effective 01/08/2026) ----
    ship = invoice.get("ShipDtls") or {}
    ship_gstin = str(ship.get("Gstin", "") or "")
    ship_provided = bool(ship.get("LglNm") or ship.get("Addr1"))
    ewb_required = bool(invoice.get("EwbDtls"))
    if ship_provided and ewb_required and not ship_gstin:
        err("ShipDtls.Gstin", "SHIPTO_GSTIN_REQUIRED",
            "Ship-to GSTIN is mandatory when Ship details are provided and EWB is required "
            "(use 'URP' if unregistered). [5002]")
    if ship_gstin and ship_gstin != "URP" and not RE_GSTIN.match(ship_gstin):
        err("ShipDtls.Gstin", "BAD_GSTIN", "Ship-to GSTIN format is invalid (or use 'URP').")
    if ship_gstin and ship_gstin != "URP" and b_gstin and ship_gstin == b_gstin:
        err("ShipDtls.Gstin", "BILLTO_EQ_SHIPTO",
            "Bill-to and Ship-to GSTIN must not be the same in Bill-to/Ship-to transactions. [2323]")
    if (ship_gstin and ship_gstin != "URP" and ship.get("Stcd")
            and str(ship["Stcd"]).zfill(2) != ship_gstin[:2]):
        err("ShipDtls.Stcd", "STATE_MISMATCH", "Ship-to state code must match the Ship-to GSTIN state. [2325]")
    if ship.get("Pin") and not RE_PIN.match(str(ship["Pin"])):
        err("ShipDtls.Pin", "BAD_PIN", "Ship-to PIN must be 6 digits.")
    if ship:
        _check_party_lengths(ship, "ShipDtls", err)

    # ---- tax jurisdiction -------------------------------------------------
    seller_state = str(seller.get("Stcd") or s_gstin[:2] or "").zfill(2) if (seller.get("Stcd") or s_gstin) else ""
    pos = str(buyer.get("Pos") or "").zfill(2) if buyer.get("Pos") else ""
    is_export_sez = sup_typ in EXPORT_SEZ_TYPES
    intra = bool(seller_state) and bool(pos) and (seller_state == pos) and not igst_on_intra and not is_export_sez

    # ---- items ------------------------------------------------------------
    sum_ass = sum_cgst = sum_sgst = sum_igst = sum_cess = sum_stcess = 0.0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            err(f"ItemList[{i}]", "BAD_ITEM", "Item must be an object.")
            continue
        f = f"ItemList[{i}]"

        hsn = str(item.get("HsnCd", "") or "")
        if not hsn:
            err(f"{f}.HsnCd", "HSN_REQUIRED",
                "HSN/SAC code is mandatory and could not be resolved from SAP. "
                "Set the line's HSN Code (goods) or SAC (service invoice) in "
                "SAP and re-fetch. [2176]")
        elif not RE_HSN.match(hsn):
            err(f"{f}.HsnCd", "BAD_HSN", "HSN must be 4-8 digits (min 6; 4 allowed if turnover < Rs 5cr). [2176]")

        qty = _num(item.get("Qty"))
        unit_price = _num(item.get("UnitPrice"))
        tot_amt = _num(item.get("TotAmt"))
        discount = _num(item.get("Discount"))
        ass_amt = _num(item.get("AssAmt"))
        gst_rt = _num(item.get("GstRt"))
        cgst = _num(item.get("CgstAmt"))
        sgst = _num(item.get("SgstAmt"))
        igst = _num(item.get("IgstAmt"))
        cess = _num(item.get("CessAmt")) + _num(item.get("CesNonAdvlAmt"))
        stcess = _num(item.get("StateCesAmt")) + _num(item.get("StateCesNonAdvlAmt"))
        othchrg = _num(item.get("OthChrg"))
        tot_item_val = _num(item.get("TotItemVal"))

        for label, v in (("Qty", qty), ("UnitPrice", unit_price), ("AssAmt", ass_amt), ("TotItemVal", tot_item_val)):
            if v < 0:
                err(f"{f}.{label}", "NEGATIVE", f"{label} cannot be negative (use a credit note, not negatives).")

        # AssAmt = TotAmt - Discount  [2193]
        if abs(ass_amt - (tot_amt - discount)) > RUPEE_TOL:
            err(f"{f}.AssAmt", "ASSAMT_MISMATCH",
                f"AssAmt ({ass_amt}) must equal TotAmt-Discount ({tot_amt - discount}). [2193]")

        # tax by jurisdiction
        if intra:
            if abs(cgst - sgst) > EQ_EPS:
                err(f"{f}.CgstAmt", "CGST_NE_SGST", "Intra-state: CgstAmt must equal SgstAmt. [2227]")
            if igst > EQ_EPS:
                err(f"{f}.IgstAmt", "IGST_ON_INTRA", "Intra-state: IGST must be 0. [2172]")
            expected = ass_amt * gst_rt / 100.0
            if abs((cgst + sgst) - expected) > RUPEE_TOL:
                err(f"{f}.CgstAmt", "TAX_MISMATCH", f"CGST+SGST ({cgst + sgst}) != AssAmtxGstRt ({expected}).")
        else:
            if cgst > EQ_EPS or sgst > EQ_EPS:
                err(f"{f}.IgstAmt", "CGST_ON_INTER", "Inter-state/export: CGST & SGST must be 0. [2174]")
            expected = ass_amt * gst_rt / 100.0
            if abs(igst - expected) > RUPEE_TOL:
                err(f"{f}.IgstAmt", "TAX_MISMATCH", f"IgstAmt ({igst}) != AssAmtxGstRt ({expected}).")

        # TotItemVal  [2194]
        calc_item = ass_amt + cgst + sgst + igst + cess + stcess + othchrg
        if abs(tot_item_val - calc_item) > RUPEE_TOL:
            err(f"{f}.TotItemVal", "TOTITEMVAL_MISMATCH",
                f"TotItemVal ({tot_item_val}) != AssAmt+taxes+cess+OthChrg ({round(calc_item, 2)}). [2194]")

        sum_ass += ass_amt
        sum_cgst += cgst
        sum_sgst += sgst
        sum_igst += igst
        sum_cess += cess
        sum_stcess += stcess

    # ---- ValDtls reconciliation ------------------------------------------
    if val:
        checks = (
            ("AssVal", sum_ass), ("CgstVal", sum_cgst), ("SgstVal", sum_sgst),
            ("IgstVal", sum_igst), ("CesVal", sum_cess), ("StCesVal", sum_stcess),
        )
        for key, expected in checks:
            if key in val and abs(_num(val.get(key)) - expected) > RUPEE_TOL:
                err(f"ValDtls.{key}", "VAL_SUM_MISMATCH",
                    f"{key} ({_num(val.get(key))}) != sum over items ({round(expected, 2)}). [2182]")

        rnd = _num(val.get("RndOffAmt"))
        if not (-99.99 <= rnd <= 99.99):
            err("ValDtls.RndOffAmt", "ROUNDOFF_RANGE", "RndOffAmt must be between -99.99 and 99.99.")

        inv_othchrg = _num(val.get("OthChrg"))
        inv_disc = _num(val.get("Discount"))
        calc_tot = sum_ass + sum_cgst + sum_sgst + sum_igst + sum_cess + sum_stcess + inv_othchrg - inv_disc + rnd
        if "TotInvVal" in val and abs(_num(val.get("TotInvVal")) - calc_tot) > RUPEE_TOL:
            err("ValDtls.TotInvVal", "TOTINV_MISMATCH",
                f"TotInvVal ({_num(val.get('TotInvVal'))}) != calculated ({round(calc_tot, 2)}). [2189]")

    return errors


def validate_ewb_by_irn(payload: dict) -> list[dict]:
    """
    Validate a 'Generate e-Way Bill by IRN' payload.

    Per the GSTN advisory 17.06.2026 (effective 01/08/2026), ExpShipDtls.Gstin is
    mandatory (send a valid GSTIN or 'URP'). Returns a list of {field, code, message}.
    """
    errors: list[dict] = []

    def err(field, code, message):
        errors.append({"field": field, "code": code, "message": message})

    if not isinstance(payload, dict) or not payload:
        return [{"field": "(root)", "code": "EMPTY", "message": "EWB-by-IRN payload must be a non-empty JSON object."}]

    if not payload.get("Irn"):
        err("Irn", "MISSING_IRN", "Irn is required.")

    exp = payload.get("ExpShipDtls") or {}
    ship_gstin = str(exp.get("Gstin", "") or "")
    if not ship_gstin:
        err("ExpShipDtls.Gstin", "SHIPTO_GSTIN_REQUIRED",
            "Ship-to GSTIN is mandatory in ExpShipDtls (send a valid GSTIN or 'URP'). [5001]")
    elif ship_gstin != "URP" and not RE_GSTIN.match(ship_gstin):
        err("ExpShipDtls.Gstin", "BAD_GSTIN", "Ship-to GSTIN format is invalid (or use 'URP').")
    # NIC requires the full ship-to address in ExpShipDtls (Addr1/Loc/Pin/Stcd),
    # not just the GSTIN — missing ones come back as error 5002.
    for key in ("Addr1", "Loc", "Pin", "Stcd"):
        if not exp.get(key):
            err(f"ExpShipDtls.{key}", "SHIPTO_ADDR_REQUIRED",
                f"ExpShipDtls.{key} is required (full ship-to address must be supplied). [5002]")
    if (ship_gstin and ship_gstin != "URP" and exp.get("Stcd")
            and str(exp["Stcd"]).zfill(2) != ship_gstin[:2]):
        err("ExpShipDtls.Stcd", "STATE_MISMATCH", "Ship-to state code must match the Ship-to GSTIN state. [4074]")
    if exp.get("Pin") and not RE_PIN.match(str(exp["Pin"])):
        err("ExpShipDtls.Pin", "BAD_PIN", "Ship-to PIN must be 6 digits.")

    return errors
