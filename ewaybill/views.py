"""Views for the standalone NIC e-Way Bill system.

Phase 2.4 audit: none of these views declared `permission_classes` at all —
they relied solely on the project-wide default (`IsAuthenticated`,
OMS/settings.py). None is admin-only by current design: generating/updating/
cancelling an e-Way Bill is a per-document business action, mirroring
`einvoice`'s own (deliberately non-admin-gated) IRN actions this module reuses
SAP/validation helpers from. So the fix is to make that default explicit per
view rather than invent a role restriction that would tighten access.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from einvoice import mapping as irn_mapping, sap, services, validation as einv_validation
from einvoice.client import EInvoiceError
from einvoice.errors import EWB_ERROR_CODES, build_error_response
from einvoice.models import IrnRecord

from . import mapping, validation
from .client import EwbClient, EwbError

_KEY_MISSING = {"error": "EWB public key not found. Set EWB_PUBLIC_KEY_PATH."}


def _run(fn, *args, **kwargs):
    try:
        return Response(fn(*args, **kwargs))
    except EwbError as exc:
        return Response(build_error_response(exc, EWB_ERROR_CODES), status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)


def _ewb_error(exc):
    return Response(build_error_response(exc, EWB_ERROR_CODES), status=exc.status_code or 502)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ewb_token(request):
    """Run the EWB auth handshake; returns a masked token."""
    try:
        s = EwbClient()._get_session(force=True)
    except EwbError as exc:
        return Response({"ok": False, "error": str(exc), "details": exc.error_details},
                        status=exc.status_code or 502)
    tok = s.auth_token or ""
    return Response({"ok": True,
                     "auth_token_masked": (tok[:4] + "..." + tok[-4:]) if len(tok) > 8 else "***",
                     "sek_bytes": len(s.sek), "expires_at": s.expires_at.isoformat()})


def _lookup_irn(irn_payload):
    """Find a GENERATED IRN for this invoice (so we can prefer EWB-by-IRN)."""
    try:
        ident = services._identity(irn_payload)
        rec = IrnRecord.objects.filter(**services._key(ident), generation_status="GENERATED").first()
        return rec.irn if rec else None
    except Exception:
        return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def ewb_from_invoice(request, docentry):
    """
    Build an e-Way Bill straight from a SAP B1 invoice (OINV DocEntry).

    Prefers **EWB-by-IRN** when the invoice already has a generated IRN (only
    transport + Ship-to are sent; the IRP fills the rest). Falls back to the
    standalone **GENEWAYBILL** otherwise.

    GET  -> map + validate only (preview; no NIC call). Returns the chosen mode,
            the payload, and validation.
    POST -> the above, then generate at NIC and persist.

    Transport details are usually entered at dispatch time, so pass overrides in
    the POST/GET body (they win over SAP's EWayBillDetails):
        { "transport": { "transMode":"1", "transDistance":120, "vehicleNo":"HR26AB1234",
                          "vehicleType":"R", "transporterId":"06AAAA...", "transDocNo":"..",
                          "transDocDate":"dd/mm/yyyy" },
          "expShip": { "Gstin":"URP", "Addr1":"..", "Loc":"..", "Pin":110001, "Stcd":"07" } }
    Query params: ?company_db=..  ?mode=auto|irn|standalone  ?order_id=<id>
    """
    company_db = request.query_params.get("company_db") or None
    mode = (request.query_params.get("mode") or "auto").lower()
    body = request.data if isinstance(request.data, dict) else {}
    overrides = body.get("transport") if isinstance(body.get("transport"), dict) else \
        {k: v for k, v in body.items() if k not in ("transport", "expShip", "ExpShipDtls")}
    exp_ship = body.get("expShip") or body.get("ExpShipDtls")

    try:
        sap_invoice, hsn_map, sac_map = sap.fetch_invoice_for_irn(docentry, company_db)
    except sap.SapFetchError as exc:
        return Response({"error": str(exc)}, status=502)

    irn_payload = irn_mapping.build_irn(sap_invoice, hsn_map, sac_map)
    irn = _lookup_irn(irn_payload)

    use_irn = (mode == "irn") or (mode == "auto" and bool(irn))
    if mode == "irn" and not irn:
        return Response({"error": "mode=irn but no generated IRN found for this invoice. "
                         "Generate the IRN first (POST /api/einvoice/irn/from-invoice/<docentry>/) "
                         "or use ?mode=standalone."}, status=409)

    if use_irn:
        payload = mapping.ewb_by_irn_payload(irn, sap_invoice, overrides=overrides,
                                             exp_ship=exp_ship, irn_payload=irn_payload)
        errs = einv_validation.validate_ewb_by_irn(payload)
        chosen = "ewb_by_irn"
    else:
        payload = mapping.genewaybill_from_irn(irn_payload, overrides=overrides)
        errs = validation.validate_genewaybill(payload)
        chosen = "genewaybill"

    if request.method == "GET":
        return Response({"docentry": int(docentry), "company_db": company_db or None,
                         "mode": chosen, "irn": irn, "payload": payload,
                         "valid": not errs, "error_count": len(errs), "validation_errors": errs})

    if errs:
        return Response({"error": "EWB payload failed pre-submit validation.",
                         "mode": chosen, "payload": payload, "validation_errors": errs}, status=422)

    order_id = request.query_params.get("order_id")
    order_id = int(order_id) if order_id and order_id.isdigit() else None
    try:
        if chosen == "ewb_by_irn":
            record, result = services.ewb_by_irn_and_store(payload, order_id=order_id)
        else:
            result = EwbClient().generate_ewb(payload)
            record = services.store_standalone_ewb(result, order_id=order_id, request_payload=payload)
    except (EInvoiceError, EwbError) as exc:
        code_map = EWB_ERROR_CODES if isinstance(exc, EwbError) else None
        resp = build_error_response(exc, code_map)
        resp.update(mode=chosen, payload=payload)
        return Response(resp, status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)

    resp = {"docentry": int(docentry), "mode": chosen, "result": result}
    if record is not None:
        resp["record_id"] = record.id
    else:
        resp["persistence_warning"] = "EWB generated at NIC but could not be saved (check logs / run migrations)."
    return Response(resp)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_ewb(request):
    """Body: full GENEWAYBILL payload (supply + transport details).
    Optional query param ?order_id=<oms order id>. Persists to einvoice_ewaybill."""
    d = request.data if isinstance(request.data, dict) else {}
    if not d:
        return Response({"error": "Request body (EWB payload) is required."}, status=400)
    order_id = request.query_params.get("order_id")
    order_id = int(order_id) if order_id and order_id.isdigit() else None
    try:
        result = EwbClient().generate_ewb(d)
    except EwbError as exc:
        return _ewb_error(exc)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)
    record = services.store_standalone_ewb(result, order_id=order_id, request_payload=d)
    resp = {"result": result}
    if record is not None:
        resp["record_id"] = record.id
    else:
        resp["persistence_warning"] = "EWB generated at NIC but could not be saved (check logs / run migrations)."
    return Response(resp)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_part_b(request):
    """Body: VEHEWB payload (ewbNo, vehicleNo, fromPlace, fromState, transMode, ...)."""
    return _run(EwbClient().update_part_b, request.data if isinstance(request.data, dict) else {})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_ewb(request):
    """Body: { "ewbNo": 123, "reason_code": 2, "remarks": "..." }. Updates the stored record."""
    d = request.data if isinstance(request.data, dict) else {}
    if not d.get("ewbNo"):
        return Response({"error": "'ewbNo' is required."}, status=400)
    reason_code, remarks = d.get("reason_code", 2), d.get("remarks", "Cancelled")
    try:
        result = EwbClient().cancel_ewb(d["ewbNo"], reason_code, remarks)
    except EwbError as exc:
        return _ewb_error(exc)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)
    record = services.mark_ewb_cancelled(d["ewbNo"], reason_code, remarks, result)
    resp = {"result": result}
    if record is not None:
        resp["record_id"] = record.id
    return Response(resp)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_ewb(request):
    d = request.data if isinstance(request.data, dict) else {}
    if not d.get("ewbNo"):
        return Response({"error": "'ewbNo' is required."}, status=400)
    return _run(EwbClient().reject_ewb, d["ewbNo"])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def extend_validity(request):
    return _run(EwbClient().extend_validity, request.data if isinstance(request.data, dict) else {})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_transporter(request):
    d = request.data if isinstance(request.data, dict) else {}
    if not d.get("ewbNo") or not d.get("transporterId"):
        return Response({"error": "'ewbNo' and 'transporterId' are required."}, status=400)
    return _run(EwbClient().update_transporter, d["ewbNo"], d["transporterId"])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def close_ewb(request):
    """Voluntary closure of an EWB after delivery (GSTN advisory 17.06.2026).
    Body: { "ewbNo": 123, "closureDate": "dd/mm/yyyy", "remarks": "..." }"""
    d = request.data if isinstance(request.data, dict) else {}
    if not d.get("ewbNo"):
        return Response({"error": "'ewbNo' is required."}, status=400)
    if not d.get("closureDate"):
        return Response({"error": "'closureDate' (dd/mm/yyyy) is required."}, status=400)
    remarks = d.get("remarks", "Delivered")
    try:
        result = EwbClient().close_ewb(d["ewbNo"], d["closureDate"], remarks)
    except EwbError as exc:
        return _ewb_error(exc)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)
    record = services.record_ewb_closure(d["ewbNo"], d["closureDate"], remarks, result)
    resp = {"result": result}
    if record is not None:
        resp["record_id"] = record.id
    return Response(resp)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_ewb(request, ewb_no):
    return _run(EwbClient().get_ewb, ewb_no)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ewb_gstin_details(request, gstin):
    return _run(EwbClient().get_gstin_details, gstin)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ewb_transporter_details(request, trans_id):
    return _run(EwbClient().get_transporter_details, trans_id)
