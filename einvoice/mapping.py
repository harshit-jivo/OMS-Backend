"""
Map a SAP Business One Service Layer Invoice (OINV / `Invoices` entity) into the
NIC e-Invoice (IRN) request JSON, schema 1.1.

Pure/among-Python: `build_irn(sap_invoice, hsn_map)` takes an already-fetched
invoice dict plus a resolved {HSNEntry: hsn_code} map and returns the IRN payload.
The SAP fetch + HSN resolution live in `einvoice.sap` so this stays testable.

Field sources (see docs/NIC_EINVOICE_EWAYBILL_REFERENCE.md §5, §10):
  SellerDtls  <- VATRegNum (issuing branch GSTIN) + EWayBillDetails.DispatchFrom*
  BuyerDtls   <- EWayBillDetails.BillTo*  + AddressExtension.BillTo* / PlaceOfSupply
  ShipDtls    <- EWayBillDetails.ShipTo*  (only when Ship-to GSTIN differs from buyer)
  ItemList    <- DocumentLines (HSN via IndiaHsn, IGST vs CGST+SGST per SAP tax code)
  ValDtls     <- summed over items

The seller GSTIN comes from the document's own `VATRegNum` — the registration of
the branch (BPL) that issued the invoice — NOT from EWayBillDetails.BillFromGSTIN.
SAP fills the BillFrom* block from the MAIN business place, so on a branch-to-branch
document (customer = another registration of the same legal entity) it echoes the
RECIPIENT's GSTIN and the invoice is rejected as NIC 2211 "supplier and recipient
GSTIN must not be the same". einvoice.sap.normalize_seller_branch repairs the whole
block at fetch time; the fallbacks here keep a raw SAP dict mapping correctly too.

NIC caps Addr1/Addr2 at 100 chars each, so long dispatch addresses are split
across Addr1 + Addr2 (error 5002 otherwise).
"""
from __future__ import annotations

# GST state code -> state name. Used as a fallback for the mandatory `Loc`
# (Place) field when SAP has no city captured (BillToCity empty) — NIC rejects a
# missing Location with error 5002.
STATE_NAMES = {
    "01": "JAMMU AND KASHMIR", "02": "HIMACHAL PRADESH", "03": "PUNJAB", "04": "CHANDIGARH",
    "05": "UTTARAKHAND", "06": "HARYANA", "07": "DELHI", "08": "RAJASTHAN",
    "09": "UTTAR PRADESH", "10": "BIHAR", "11": "SIKKIM", "12": "ARUNACHAL PRADESH",
    "13": "NAGALAND", "14": "MANIPUR", "15": "MIZORAM", "16": "TRIPURA", "17": "MEGHALAYA",
    "18": "ASSAM", "19": "WEST BENGAL", "20": "JHARKHAND", "21": "ODISHA", "22": "CHHATTISGARH",
    "23": "MADHYA PRADESH", "24": "GUJARAT", "25": "DAMAN AND DIU",
    "26": "DADRA AND NAGAR HAVELI AND DAMAN AND DIU", "27": "MAHARASHTRA", "28": "ANDHRA PRADESH",
    "29": "KARNATAKA", "30": "GOA", "31": "LAKSHADWEEP", "32": "KERALA", "33": "TAMIL NADU",
    "34": "PUDUCHERRY", "35": "ANDAMAN AND NICOBAR ISLANDS", "36": "TELANGANA",
    "37": "ANDHRA PRADESH", "38": "LADAKH", "96": "OTHER COUNTRY", "97": "OTHER TERRITORY",
}


def _loc(*candidates, state_code=None):
    """Pick the first non-empty place name; fall back to the GST state name so the
    mandatory NIC `Loc` field is never empty (error 5002)."""
    for c in candidates:
        c = _clean(c)
        if c:
            return c
    return STATE_NAMES.get(str(state_code or "").zfill(2))


def _clean(s):
    """Collapse SAP's embedded CR/LF and repeated spaces into a single line."""
    return " ".join(str(s).replace("\r", " ").replace("\n", " ").split()) if s else s


def _split_addr(s):
    """NIC caps Addr1/Addr2 at 100 chars. Split a long address on a word boundary
    near 100 into (Addr1, Addr2). Returns (addr1, addr2_or_None)."""
    s = _clean(s)
    if not s or len(s) <= 100:
        return s, None
    cut = s.rfind(" ", 0, 100)
    if cut <= 0:
        cut = 100
    return s[:cut].strip(), s[cut:].strip()[:100]


def _r2(x):
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _ddmmyyyy(iso):
    """'2026-07-02T00:00:00Z' -> '02/07/2026'."""
    if not iso:
        return None
    d = str(iso)[:10]
    try:
        y, m, dd = d.split("-")
        return f"{dd}/{m}/{y}"
    except ValueError:
        return None


def _int_or_none(v):
    try:
        return int(str(v).strip()) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def gstin(v):
    """Normalise a GSTIN, or None when the value is not one (15 chars, numeric
    state prefix). Lets the caller prefer one SAP source over another safely."""
    v = str(v or "").strip().upper()
    return v if len(v) == 15 and v[:2].isdigit() else None


def _sap_tax_kind(lines):
    """'INTER' when SAP posted the document under IGST, 'INTRA' when under
    CGST+SGST, None when it carries no tax code.

    SAP's own tax codes are authoritative for the jurisdiction: comparing the
    seller state against the place of supply silently produces CGST+SGST on a
    document SAP taxed as IGST whenever the seller state is misread (see the
    BillFromGSTIN note in the module docstring).
    """
    codes = []
    for ln in lines:
        codes.append(str(ln.get("TaxCode") or "").upper())
        for j in (ln.get("LineTaxJurisdictions") or []):
            codes.append(str(j.get("JurisdictionCode") or "").upper())
    if any("IGST" in c for c in codes):
        return "INTER"
    # SAP names the paired code 'CG+SG@18' and the jurisdictions 'CGST@9'/'SGST@9'.
    if any("CGST" in c or "SGST" in c or "CG+SG" in c for c in codes):
        return "INTRA"
    return None


def _line_tax_inr(ln):
    """Line tax in INR (system currency), summed from LineTaxJurisdictions.
    Used for foreign-currency export invoices where TaxTotal is in the doc currency."""
    total = 0.0
    for j in (ln.get("LineTaxJurisdictions") or []):
        total += float(j.get("TaxAmountSC") or 0)
    return total


def _party(keys, values):
    """Build a party block, dropping any key whose value is None (NIC rejects
    empty optional strings like a null Addr2)."""
    return {k: v for k, v in zip(keys, values) if v is not None}


def build_irn(sap_invoice: dict, hsn_map: dict | None = None) -> dict:
    """
    Convert a SAP Service Layer invoice dict to an IRN JSON payload (schema 1.1).

    `hsn_map` maps a line's HSNEntry (int) to its HSN code string (see
    einvoice.sap.resolve_hsn). Lines whose HSN cannot be resolved get "" so the
    pre-submit validator flags them rather than silently sending a bad code.
    """
    hsn_map = hsn_map or {}
    ewb = sap_invoice.get("EWayBillDetails") or {}
    axe = sap_invoice.get("AddressExtension") or {}
    lines = sap_invoice.get("DocumentLines") or []

    # The invoice's own VATRegNum is the GSTIN of the branch that issued it and
    # wins over EWayBillDetails.BillFromGSTIN, which SAP fills from the main
    # business place (module docstring). The state always follows the GSTIN.
    seller_gstin = gstin(sap_invoice.get("VATRegNum")) or gstin(ewb.get("BillFromGSTIN"))
    seller_state = seller_gstin[:2] if seller_gstin else ewb.get("BillFromStateGSTCode")
    buyer_state = ewb.get("BillToStateGSTCode")

    # Export detection: a non-IN Bill-to country. Exports are always inter-state
    # (POS = 96 "Other Territory") and carry IGST (EXPWP) or no tax under LUT
    # (EXPWOP). For a foreign-currency export, amounts are taken from the SAP
    # system-currency (INR) fields — NIC expects INR values, with the foreign
    # currency reported in ExpDtls.ForCur. (SEZ supplies are not auto-detected.)
    buyer_country = str(axe.get("BillToCountry") or "IN").upper()
    is_export = bool(buyer_country) and buyer_country != "IN"
    doc_currency = str(sap_invoice.get("DocCurrency") or "INR").upper()
    use_sc = is_export and doc_currency != "INR"

    pos_code = "96" if is_export else (buyer_state or seller_state)
    # Follow the tax SAP actually posted; only guess from the states when the
    # lines carry no tax code at all.
    tax_kind = None if is_export else _sap_tax_kind(lines)
    if tax_kind:
        intra = tax_kind == "INTRA"
    else:
        intra = (not is_export) and str(seller_state) == str(pos_code)

    item_list = []
    ass_total = cgst_total = sgst_total = igst_total = tot_inv = 0.0
    for i, ln in enumerate(lines, start=1):
        qty = float(ln.get("Quantity") or 0)
        if use_sc:                                   # foreign-currency export -> INR fields
            ass = _r2(ln.get("RowTotalSC"))
            tax_total = _r2(_line_tax_inr(ln))
            unit_price = _r2(ass / qty) if qty else 0.0
        else:
            ass = _r2(ln.get("LineTotal"))           # taxable value (net of discount)
            tax_total = _r2(ln.get("TaxTotal"))
            unit_price = _r2(ln.get("Price"))
        gst_rt = float(ln.get("TaxPercentagePerRow") or 0)
        cgst = sgst = igst = 0.0
        if intra:
            cgst = sgst = _r2(tax_total / 2)
        else:
            igst = tax_total
        tot_item = _r2(ass + cgst + sgst + igst)
        hsn = hsn_map.get(ln.get("HSNEntry")) or ""
        item_list.append({
            "SlNo": str(i),
            "PrdDesc": _clean(ln.get("ItemDescription") or ln.get("ItemCode")),
            "IsServc": "N",
            "HsnCd": hsn,
            "Qty": qty,
            "Unit": (ln.get("MeasureUnit") or "NOS").upper(),
            "UnitPrice": unit_price,
            "TotAmt": ass,
            "AssAmt": ass,
            "GstRt": gst_rt,
            "IgstAmt": igst,
            "CgstAmt": cgst,
            "SgstAmt": sgst,
            "TotItemVal": tot_item,
        })
        ass_total += ass
        cgst_total += cgst
        sgst_total += sgst
        igst_total += igst
        tot_inv += tot_item

    # SupTyp: exports are EXPWP (tax paid) if any IGST was charged, else EXPWOP (LUT).
    if is_export:
        sup_typ = "EXPWP" if igst_total > 0 else "EXPWOP"
    else:
        sup_typ = "B2B"

    # DispatchFrom* belongs to the same registration as BillFromGSTIN, so it is
    # only usable once its state agrees with the seller GSTIN — sending a
    # wrong-state address under a correct GSTIN is rejected by NIC (2258 state
    # mismatch / 2231 PIN does not belong to the state). Dropping the address
    # instead surfaces a FIELD_REQUIRED from the pre-submit validator, which is a
    # far more useful failure than a plausible-looking wrong one.
    disp_state = str(ewb.get("DispatchFromStateGSTCode") or "").zfill(2)
    if seller_state and disp_state and disp_state != str(seller_state).zfill(2):
        seller_a1 = seller_a2 = seller_pin = None
        seller_loc = _loc(state_code=seller_state)
    else:
        seller_a1, seller_a2 = _split_addr(ewb.get("DispatchFromAddress1"))
        # No ShipToCity fallback here: that is the RECIPIENT's city and would put
        # the seller in the wrong state.
        seller_loc = _loc(ewb.get("DispatchFromPlace"), state_code=seller_state)
        seller_pin = _int_or_none(ewb.get("DispatchFromZipCode"))

    buyer_a1, buyer_a2 = _split_addr(
        axe.get("BillToBlock") or axe.get("BillToAddress2") or axe.get("BillToStreet")
        or sap_invoice.get("Address"))

    # Buyer identity differs for exports: no Indian GSTIN (URP), POS/state = 96
    # (Other Territory), and a dummy PIN when the foreign zip is not 6 numeric digits.
    if is_export:
        buyer_gstin = "URP"
        buyer_pos = "96"
        buyer_stcd = "96"
        # NIC requires a 6-digit (100000-999999) PIN; a foreign postcode won't
        # conform, so exports use the dummy 999999.
        buyer_pin = 999999
    else:
        buyer_gstin = ewb.get("BillToGSTIN")
        buyer_pos = str(pos_code) if pos_code else None
        buyer_stcd = str(buyer_state) if buyer_state else None
        buyer_pin = _int_or_none(axe.get("BillToZipCode"))

    irn = {
        "Version": "1.1",
        "TranDtls": {"TaxSch": "GST", "SupTyp": sup_typ, "RegRev": "N", "IgstOnIntra": "N"},
        "DocDtls": {
            "Typ": "INV",
            "No": str(sap_invoice.get("DocNum") or ""),
            "Dt": _ddmmyyyy(sap_invoice.get("DocDate")),
        },
        "SellerDtls": _party(
            ("Gstin", "LglNm", "Addr1", "Addr2", "Loc", "Pin", "Stcd"),
            (seller_gstin,
             _clean(ewb.get("BillFromName")),
             seller_a1, seller_a2,
             seller_loc,
             seller_pin,
             str(seller_state) if seller_state else None)),
        "BuyerDtls": _party(
            ("Gstin", "LglNm", "Pos", "Addr1", "Addr2", "Loc", "Pin", "Stcd"),
            (buyer_gstin,
             _clean(sap_invoice.get("CardName")),
             buyer_pos,
             buyer_a1, buyer_a2,
             _loc(axe.get("BillToCity"), state_code=("96" if is_export else buyer_state)),
             buyer_pin,
             buyer_stcd)),
        "ItemList": item_list,
        "ValDtls": {
            "AssVal": _r2(ass_total),
            "CgstVal": _r2(cgst_total),
            "SgstVal": _r2(sgst_total),
            "IgstVal": _r2(igst_total),
            "TotInvVal": _r2(tot_inv),
        },
    }

    # Ship-to only when it is a genuinely different party (a same-GSTIN ship-to is
    # rejected as NIC 2323 — see the 17.06.2026 advisory).
    ship_gstin = ewb.get("ShipToGSTIN")
    if ship_gstin and ship_gstin != irn["BuyerDtls"].get("Gstin"):
        ship_a1, ship_a2 = _split_addr(ewb.get("ShipToAddress1"))
        irn["ShipDtls"] = _party(
            ("Gstin", "LglNm", "Addr1", "Addr2", "Loc", "Pin", "Stcd"),
            (ship_gstin,
             _clean(ewb.get("BillToName")),
             ship_a1, ship_a2,
             _loc(ewb.get("ShipToPlace"), axe.get("ShipToCity"),
                  state_code=ewb.get("ShipToStateGSTCode") or buyer_state),
             _int_or_none(ewb.get("ShipToZipCode")),
             str(ewb.get("ShipToStateGSTCode") or buyer_state or "") or None))

    # Export details block (country code + foreign currency). Shipping-bill no/date
    # and port are not captured in SAP OINV, so they are omitted (optional at IRN time).
    if is_export:
        exp = {"CntCode": buyer_country[:2]}
        if doc_currency != "INR":
            exp["ForCur"] = doc_currency
        irn["ExpDtls"] = exp

    return irn
