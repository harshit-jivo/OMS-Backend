"""
Invoice scenario builders covering the "Key Features" in NIC's Test Summary
Report: different document types, Bill-To/Ship-To, Export/SEZ, intra/inter-state,
service items, and multi-item invoices.

Each builder takes a `ctx` dict:
    {seller, buyer, intra_buyer, sez_buyer, doc_no, doc_date}
and returns an invoice dict, or None if a required GSTIN isn't configured
(e.g. intra-state needs a same-state buyer; SEZ needs an SEZ-unit GSTIN).
"""
from __future__ import annotations

from .sample import sample_invoice


def _recompute_val(inv: dict) -> dict:
    items = inv["ItemList"]
    f = lambda k: round(sum(i.get(k, 0) for i in items), 2)  # noqa: E731
    inv["ValDtls"] = {
        "AssVal": f("AssAmt"), "CgstVal": f("CgstAmt"), "SgstVal": f("SgstAmt"),
        "IgstVal": f("IgstAmt"), "TotInvVal": f("TotItemVal"),
    }
    return inv


# -- individual scenarios ----------------------------------------------------

def b2b_interstate(ctx):
    return sample_invoice(ctx["seller"], ctx["buyer"], ctx["doc_no"], ctx["doc_date"])


def b2b_intrastate(ctx):
    if not ctx.get("intra_buyer"):
        return None
    # Same-state buyer -> sample_invoice derives CGST/SGST automatically.
    return sample_invoice(ctx["seller"], ctx["intra_buyer"], ctx["doc_no"], ctx["doc_date"])


def multi_item(ctx):
    inv = sample_invoice(ctx["seller"], ctx["buyer"], ctx["doc_no"], ctx["doc_date"])
    base = inv["ItemList"][0]
    second = dict(base)
    second.update({"SlNo": "2", "PrdDesc": "Second Item", "HsnCd": "100610"})
    inv["ItemList"].append(second)
    return _recompute_val(inv)


def service_item(ctx):
    inv = sample_invoice(ctx["seller"], ctx["buyer"], ctx["doc_no"], ctx["doc_date"])
    inv["ItemList"][0].update({"IsServc": "Y", "HsnCd": "998314", "PrdDesc": "IT consulting service"})
    return inv


def bill_to_ship_to(ctx):
    inv = sample_invoice(ctx["seller"], ctx["buyer"], ctx["doc_no"], ctx["doc_date"])
    # Ship-To GSTIN must differ from the Buyer; use the seller's GSTIN as a
    # distinct registered ship-to party (a third GSTIN is ideal if available).
    inv["DispDtls"] = {
        "Nm": "Dispatch Warehouse", "Addr1": "Warehouse Rd", "Loc": "Gurugram",
        "Pin": 122001, "Stcd": "06",
    }
    inv["ShipDtls"] = {
        "Gstin": ctx["seller"], "LglNm": "Ship-To Party", "Addr1": "Ship Rd",
        "Loc": "Gurugram", "Pin": 122001, "Stcd": "06",
    }
    return inv


def export_with_payment(ctx):
    # Direct export: recipient URP, state 96, PIN 999999, POS 96; IGST applies.
    inv = sample_invoice(ctx["seller"], ctx["seller"], ctx["doc_no"], ctx["doc_date"])
    inv["TranDtls"].update({"SupTyp": "EXPWP"})
    item = inv["ItemList"][0]
    item.update({"CgstAmt": 0.0, "SgstAmt": 0.0, "IgstAmt": 180.0})
    inv["BuyerDtls"] = {
        "Gstin": "URP", "LglNm": "Overseas Buyer", "Pos": "96",
        "Addr1": "Foreign Address", "Loc": "Dubai", "Pin": 999999, "Stcd": "96",
    }
    inv["ExpDtls"] = {
        "ShipBNo": "SB001", "ShipBDt": ctx["doc_date"], "Port": "INNSA1",
        "RefClm": "N", "ForCur": "USD", "CntCode": "AE",
    }
    return _recompute_val(inv)


def sez_with_payment(ctx):
    if not ctx.get("sez_buyer"):
        return None
    inv = sample_invoice(ctx["seller"], ctx["sez_buyer"], ctx["doc_no"], ctx["doc_date"])
    inv["TranDtls"].update({"SupTyp": "SEZWP"})
    # SEZ supplies attract IGST regardless of state.
    item = inv["ItemList"][0]
    item.update({"CgstAmt": 0.0, "SgstAmt": 0.0, "IgstAmt": 180.0})
    return _recompute_val(inv)


def credit_note(ctx):
    inv = sample_invoice(ctx["seller"], ctx["buyer"], ctx["doc_no"], ctx["doc_date"])
    inv["DocDtls"]["Typ"] = "CRN"
    inv["RefDtls"] = {"PrecDocDtls": [{"InvNo": "ORIG-1", "InvDt": ctx["doc_date"]}]}
    return inv


def debit_note(ctx):
    inv = sample_invoice(ctx["seller"], ctx["buyer"], ctx["doc_no"], ctx["doc_date"])
    inv["DocDtls"]["Typ"] = "DBN"
    inv["RefDtls"] = {"PrecDocDtls": [{"InvNo": "ORIG-1", "InvDt": ctx["doc_date"]}]}
    return inv


# label -> builder. Builders returning None (missing GSTIN) are skipped at runtime.
SCENARIOS = [
    ("INV B2B interstate", b2b_interstate),
    ("INV B2B intrastate", b2b_intrastate),
    ("INV multi-item", multi_item),
    ("INV service item", service_item),
    ("INV Bill-To/Ship-To", bill_to_ship_to),
    ("INV export (EXPWP)", export_with_payment),
    ("INV SEZ (SEZWP)", sez_with_payment),
    ("CRN credit note", credit_note),
    ("DBN debit note", debit_note),
]
