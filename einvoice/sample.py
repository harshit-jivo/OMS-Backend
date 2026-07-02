"""
A minimal valid e-invoice payload (schema 1.1) you can use to smoke-test the
sandbox. Replace GSTINs/amounts with your authorized test data.

State codes (Stcd / Pos) are derived from the GSTINs so they always match the
first two digits — NIC rejects a mismatch. Tax is split correctly: CGST+SGST
for an intra-state supply (same state), IGST for inter-state.
"""

# A few state code -> (city, pincode) pairs so the sample looks coherent.
# Falls back to a generic valid pincode for states not listed.
_STATE_CITY = {
    "06": ("Gurugram", 122001),    # Haryana
    "07": ("New Delhi", 110001),   # Delhi
    "11": ("Gangtok", 737101),     # Sikkim
    "27": ("Mumbai", 400001),      # Maharashtra
    "29": ("Bengaluru", 560001),   # Karnataka
    "33": ("Chennai", 600001),     # Tamil Nadu
    "37": ("Guntur", 522001),      # Andhra Pradesh
}


def _place(state_code: str):
    return _STATE_CITY.get(state_code, ("Industrial Area", 110001))


def sample_invoice(seller_gstin: str, buyer_gstin: str, doc_no: str, doc_date: str) -> dict:
    seller_state = seller_gstin[:2]
    buyer_state = buyer_gstin[:2]
    intra = seller_state == buyer_state

    ass_amt = 1000.0
    gst_rt = 18.0
    # Intra-state -> CGST+SGST (rate split in half); inter-state -> IGST.
    cgst = sgst = round(ass_amt * gst_rt / 200, 2) if intra else 0.0
    igst = 0.0 if intra else round(ass_amt * gst_rt / 100, 2)
    tot_item_val = round(ass_amt + cgst + sgst + igst, 2)

    seller_loc, seller_pin = _place(seller_state)
    buyer_loc, buyer_pin = _place(buyer_state)

    return {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "B2B",
            "RegRev": "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": "INV",
            "No": doc_no,
            "Dt": doc_date,  # format: dd/mm/yyyy
        },
        "SellerDtls": {
            "Gstin": seller_gstin,
            "LglNm": "Test Seller Pvt Ltd",
            "Addr1": "Plot 1, Industrial Area",
            "Loc": seller_loc,
            "Pin": seller_pin,
            "Stcd": seller_state,
        },
        "BuyerDtls": {
            "Gstin": buyer_gstin,
            "LglNm": "Test Buyer Pvt Ltd",
            "Pos": buyer_state,
            "Addr1": "MG Road",
            "Loc": buyer_loc,
            "Pin": buyer_pin,
            "Stcd": buyer_state,
        },
        "ItemList": [
            {
                "SlNo": "1",
                "PrdDesc": "Test Item",
                "IsServc": "N",
                "HsnCd": "100190",
                "Qty": 10.0,
                "Unit": "NOS",
                "UnitPrice": 100.0,
                "TotAmt": ass_amt,
                "AssAmt": ass_amt,
                "GstRt": gst_rt,
                "CgstAmt": cgst,
                "SgstAmt": sgst,
                "IgstAmt": igst,
                "TotItemVal": tot_item_val,
            }
        ],
        "ValDtls": {
            "AssVal": ass_amt,
            "CgstVal": cgst,
            "SgstVal": sgst,
            "IgstVal": igst,
            "TotInvVal": tot_item_val,
        },
    }
