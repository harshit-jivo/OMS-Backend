"""
Map a SAP Business One invoice (OINV) to NIC e-Way Bill payloads.

Two shapes are produced, both starting from the IRN payload that
`einvoice.mapping.build_irn` already derives from the same invoice (so the EWB
stays consistent with the e-Invoice):

  * `ewb_by_irn_payload(irn, sap_invoice, overrides, exp_ship)` — the small
    transport-only payload for the **EWB-by-IRN** API (PascalCase; the IRP fills
    the rest from the registered invoice). This is the preferred path.
  * `genewaybill_from_irn(irn_payload, overrides)` — the full **GENEWAYBILL**
    payload (camelCase) for the standalone EWB system, used when the invoice has
    no IRN. Field derivation follows docs §10 (IRN payload → EWB).

Transport details (vehicle, transporter, distance, mode) come from SAP
`EWayBillDetails`; they are usually filled at dispatch time, so the endpoint lets
the caller pass overrides that win over whatever SAP has.
"""
from __future__ import annotations

# SAP TransportationMode -> NIC transMode (1-Road, 2-Rail, 3-Air, 4-Ship).
_TRANSMODE = {
    "tm_road": "1", "tm_rail": "2", "tm_air": "3", "tm_ship": "4",
    "road": "1", "rail": "2", "air": "3", "ship": "4",
    "1": "1", "2": "2", "3": "3", "4": "4",
}


def _clean(s):
    return " ".join(str(s).replace("\r", " ").replace("\n", " ").split()) if s else s


def _ddmmyyyy(iso):
    if not iso:
        return None
    d = str(iso)[:10]
    try:
        y, m, dd = d.split("-")
        return f"{dd}/{m}/{y}"
    except ValueError:
        return str(iso)  # already dd/mm/yyyy


def _int(v, default=None):
    try:
        return int(float(str(v).strip())) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _trans_mode(raw):
    if raw in (None, ""):
        return None
    return _TRANSMODE.get(str(raw).strip().lower())


def _veh_type(raw, has_vehicle):
    if raw:
        return "O" if "odc" in str(raw).lower() else "R"
    return "R" if has_vehicle else None


def sap_transport(sap_invoice: dict) -> dict:
    """Normalised transport fields from SAP EWayBillDetails (only non-empty)."""
    ewb = sap_invoice.get("EWayBillDetails") or {}
    veh = _clean(ewb.get("VehicleNo"))
    out = {
        "transMode": _trans_mode(ewb.get("TransportationMode")),
        "transDistance": _int(ewb.get("Distance")),
        "transporterId": _clean(ewb.get("TransporterID")),
        "transporterName": _clean(ewb.get("TransporterName")),
        "transDocNo": _clean(ewb.get("TransporterDocNo")),
        "transDocDate": _ddmmyyyy(ewb.get("TransporterDocDate")),
        "vehicleNo": veh,
        "vehicleType": _veh_type(ewb.get("VehicleType"), bool(veh)),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def _merge_transport(sap_invoice, overrides):
    """SAP transport, then caller overrides on top (override keys are camelCase)."""
    t = sap_transport(sap_invoice)
    for k, v in (overrides or {}).items():
        if v not in (None, ""):
            t[k] = v
    return t


# ---- EWB-by-IRN (preferred) ----------------------------------------------

def exp_ship_from_invoice(sap_invoice: dict, irn_payload: dict) -> dict:
    """Build ExpShipDtls (mandatory Ship-to from advisory 17.06.2026). Prefer the
    IRN ShipDtls; else fall back to the buyer. Gstin = ship/buyer GSTIN or 'URP'."""
    ship = (irn_payload.get("ShipDtls") or {}).copy()
    if ship:
        ship.setdefault("Gstin", "URP")
        return ship
    # No distinct ship-to: reuse the buyer's ADDRESS but send Gstin = URP. Sending
    # the buyer's own GSTIN as the Ship-to GSTIN is rejected (NIC 4073).
    buyer = irn_payload.get("BuyerDtls") or {}
    exp = {k: buyer.get(k) for k in ("LglNm", "Addr1", "Addr2", "Loc", "Pin", "Stcd")
           if buyer.get(k) not in (None, "")}
    exp["Gstin"] = "URP"
    return exp


def ewb_by_irn_payload(irn: str, sap_invoice: dict, *, overrides=None,
                       exp_ship=None, irn_payload=None) -> dict:
    """Transport-only payload for the EWB-by-IRN API (PascalCase field names)."""
    t = _merge_transport(sap_invoice, overrides)
    payload = {"Irn": irn}
    # camelCase transport -> PascalCase EWB-by-IRN keys
    key_map = {
        "transMode": "TransMode", "transDistance": "Distance",
        "transporterId": "TransId", "transporterName": "TransName",
        "transDocNo": "TransDocNo", "transDocDate": "TransDocDt",
        "vehicleNo": "VehNo", "vehicleType": "VehType",
    }
    for ck, pk in key_map.items():
        if ck in t:
            payload[pk] = t[ck]
    payload.setdefault("Distance", 0)          # 0 -> NIC auto-computes by pincodes
    payload["ExpShipDtls"] = exp_ship or exp_ship_from_invoice(sap_invoice, irn_payload or {})
    return payload


# ---- Standalone GENEWAYBILL (no IRN) -------------------------------------

def _sub_supply_type(sup_typ):
    return "3" if sup_typ in ("EXPWP", "EXPWOP") else "1"   # 3=Export, 1=Supply


def _transaction_type(irn_payload):
    has_disp = bool(irn_payload.get("DispDtls"))
    has_ship = bool(irn_payload.get("ShipDtls"))
    return {(False, False): 1, (False, True): 2, (True, False): 3, (True, True): 4}[(has_disp, has_ship)]


def genewaybill_from_irn(irn_payload: dict, *, overrides=None) -> dict:
    """Full GENEWAYBILL payload derived from an IRN payload (docs §10)."""
    tran = irn_payload.get("TranDtls") or {}
    doc = irn_payload.get("DocDtls") or {}
    seller = irn_payload.get("SellerDtls") or {}
    buyer = irn_payload.get("BuyerDtls") or {}
    disp = irn_payload.get("DispDtls") or {}
    ship = irn_payload.get("ShipDtls") or {}
    val = irn_payload.get("ValDtls") or {}
    items = irn_payload.get("ItemList") or []

    frm = disp or seller     # dispatch-from overrides seller for address block
    to = ship or buyer

    item_list = []
    for it in items[:250]:
        gst_rt = float(it.get("GstRt") or 0)
        item_list.append({
            "productName": _clean(it.get("PrdDesc")) or "Item",
            "productDesc": (_clean(it.get("PrdDesc")) or "")[:100],
            "hsnCode": _int(it.get("HsnCd")),
            "quantity": it.get("Qty"),
            "qtyUnit": it.get("Unit"),
            "taxableAmount": it.get("AssAmt"),
            "cgstRate": round(gst_rt / 2, 3) if it.get("CgstAmt") else 0,
            "sgstRate": round(gst_rt / 2, 3) if it.get("SgstAmt") else 0,
            "igstRate": gst_rt if it.get("IgstAmt") else 0,
            "cessRate": float(it.get("CesRt") or 0),
        })

    payload = {
        "supplyType": "O",
        "subSupplyType": _sub_supply_type(tran.get("SupTyp")),
        "docType": doc.get("Typ") or "INV",
        "docNo": doc.get("No"),
        "docDate": doc.get("Dt"),

        "fromGstin": seller.get("Gstin"),
        "fromTrdName": _clean(seller.get("TrdNm") or seller.get("LglNm")),
        "fromAddr1": frm.get("Addr1"),
        "fromAddr2": frm.get("Addr2"),
        "fromPlace": frm.get("Loc"),
        "fromPincode": _int(frm.get("Pin")),
        "fromStateCode": _int(seller.get("Stcd")),
        "actFromStateCode": _int(disp.get("Stcd") or seller.get("Stcd")),

        "toGstin": buyer.get("Gstin"),
        "toTrdName": _clean(buyer.get("TrdNm") or buyer.get("LglNm")),
        "toAddr1": to.get("Addr1"),
        "toAddr2": to.get("Addr2"),
        "toPlace": to.get("Loc"),
        "toPincode": _int(to.get("Pin")),
        "toStateCode": _int(buyer.get("Stcd") or buyer.get("Pos")),
        "actToStateCode": _int(ship.get("Stcd") or buyer.get("Stcd") or buyer.get("Pos")),

        "transactionType": _transaction_type(irn_payload),
        "totalValue": val.get("AssVal"),
        "cgstValue": val.get("CgstVal", 0),
        "sgstValue": val.get("SgstVal", 0),
        "igstValue": val.get("IgstVal", 0),
        "cessValue": val.get("CesVal", 0),
        "cessNonAdvolValue": 0,
        "otherValue": round(float(val.get("OthChrg") or 0) + float(val.get("StCesVal") or 0)
                            - float(val.get("Discount") or 0) + float(val.get("RndOffAmt") or 0), 2),
        "totInvValue": val.get("TotInvVal"),
        "itemList": item_list,
    }

    # transport overrides (camelCase already matches GENEWAYBILL)
    for k, v in (overrides or {}).items():
        if v not in (None, ""):
            payload[k] = v
    payload.setdefault("transDistance", 0)     # 0 -> NIC auto-computes by pincodes
    return {k: v for k, v in payload.items() if v is not None}
