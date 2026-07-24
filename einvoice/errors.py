"""
Clean surfacing of NIC error responses.

The NIC APIs return errors as a (base64-decoded) list of {ErrorCode, ErrorMessage}
for e-Invoice, or {"errorCodes": "107,108"} for e-Way Bill. This module maps the
codes to plain-language meanings and normalises the many shapes into one list of
{code, message, hint}. See einvoice/docs/NIC_EINVOICE_EWAYBILL_REFERENCE.md.
"""
from __future__ import annotations


# Common e-Invoice / IRN error codes (verify against einvoice1.gst.gov.in/others/geterrorcodes/INV).
IRN_ERROR_CODES = {
    "1002": "Invalid login / credentials.",
    "1005": "Invalid or expired AuthToken — re-authenticate.",
    "2150": "Duplicate IRN: an IRN already exists for this GSTIN + DocType + DocNo + FY.",
    "2172": "Intra-state supply: IGST is not applicable, only CGST + SGST.",
    "2174": "Inter-state supply: CGST/SGST must be 0 (IGST applies).",
    "2176": "HSN code is invalid.",
    "2182": "Sum of item taxable values does not match invoice AssVal.",
    "2189": "Total invoice value does not match the calculated value.",
    "2193": "Item taxable value (AssAmt) != (TotAmt - Discount).",
    "2194": "Item total value (TotItemVal) does not match the calculated value.",
    "2211": "Supplier and recipient GSTIN must not be the same.",
    "2212": "Recipient GSTIN cannot be URP for this supply type.",
    "2227": "Intra-state supply: CGST amount must equal SGST amount.",
    "2258": "Supplier GSTIN's first two digits must match the supplier state code.",
    "2265": "Recipient GSTIN's first two digits must match the recipient state code.",
    "2294": "IgstOnIntra = Y requires reverse charge (RegRev = Y).",
    # Ship-to GSTIN advisory (17.06.2026, effective 01/08/2026):
    "2323": "Bill-to GSTIN and Ship-to GSTIN must not be the same (Bill-to/Ship-to transactions).",
    "2324": "B2B/SEZ: Ship details provided during IRN generation cannot be replaced at EWB-by-IRN.",
    "2325": "Ship-to state code must match the Ship-to GSTIN state code (Generate IRN + EWB).",
    "3028": "GSTIN is invalid or not registered.",
    "3029": "GSTIN is inactive or cancelled — sync from the GST common portal and retry.",
    "3039": "Ship-to PIN code must belong to the Ship-to state code.",
    "4074": "Ship-to state code must match the Ship-to GSTIN state code (EWB-by-IRN).",
    "5001": "Auth: application error (malformed/encrypted payload). In EWB-by-IRN: ExpShipDtls.Gstin is mandatory.",
    "5002": "Ship-to GSTIN is mandatory when Ship details are provided (Generate IRN + EWB).",
}

# Common e-Way Bill error codes (full list retrievable at runtime via the GET Error List API).
EWB_ERROR_CODES = {
    "100": "Invalid JSON.",
    "102": "Invalid password / login.",
    "106": "Token expired.",
    "108": "Invalid login credentials.",
    "109": "Decryption of data failed (wrong SEK/AppKey).",
    "111": "GSTIN is not registered to this GSP.",
    "238": "Invalid auth token.",
    "315": "Validity period lapsed — cannot cancel.",
    "316": "Cannot cancel a verified e-way bill.",
    "325": "Could not retrieve data.",
    "342": "Cannot reject — you are not the other party.",
    "343": "This e-way bill is already cancelled.",
    "702": "Distance between the given PIN codes is too high.",
    "4011": "Vehicle number is required when transport mode is Road.",
    "4014": "Invalid vehicle number format.",
}


def normalize_error_details(details, code_map=None):
    """
    Turn NIC's varied ErrorDetails shapes into a list of
    {"code": str|None, "message": str, "hint": str|None}.
    """
    code_map = code_map or IRN_ERROR_CODES

    def one(code, message):
        code = str(code).strip() if code not in (None, "") else None
        return {"code": code, "message": message or "", "hint": code_map.get(code) if code else None}

    if not details:
        return []

    # e-Way Bill style: {"errorCodes": "107,108"} (optionally nested under "raw").
    if isinstance(details, dict):
        raw = details.get("raw", details)
        if isinstance(raw, dict) and "errorCodes" in raw:
            return [one(c, code_map.get(str(c).strip(), "")) for c in str(raw["errorCodes"]).split(",") if c.strip()]
        # single {ErrorCode, ErrorMessage} object
        code = details.get("ErrorCode") or details.get("errorCode") or details.get("code")
        msg = details.get("ErrorMessage") or details.get("errorMessage") or details.get("message")
        if code or msg:
            return [one(code, msg)]
        return [one(None, str(details))]

    # e-Invoice style: list of {ErrorCode, ErrorMessage}.
    if isinstance(details, (list, tuple)):
        out = []
        for it in details:
            if isinstance(it, dict):
                out.append(one(
                    it.get("ErrorCode") or it.get("errorCode") or it.get("code"),
                    it.get("ErrorMessage") or it.get("errorMessage") or it.get("message"),
                ))
            else:
                out.append(one(None, str(it)))
        return out

    # plain string
    return [one(None, str(details))]


def build_error_response(exc, code_map=None):
    """Build a clean JSON body for a NIC client error (EInvoiceError / EwbError)."""
    return {
        "error": str(exc),
        "errors": normalize_error_details(getattr(exc, "error_details", None), code_map),
    }
