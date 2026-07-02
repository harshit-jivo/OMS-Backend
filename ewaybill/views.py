"""Views for the standalone NIC e-Way Bill system."""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from einvoice import services
from einvoice.errors import EWB_ERROR_CODES, build_error_response

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


@api_view(["POST"])
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
def update_part_b(request):
    """Body: VEHEWB payload (ewbNo, vehicleNo, fromPlace, fromState, transMode, ...)."""
    return _run(EwbClient().update_part_b, request.data if isinstance(request.data, dict) else {})


@api_view(["POST"])
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
def reject_ewb(request):
    d = request.data if isinstance(request.data, dict) else {}
    if not d.get("ewbNo"):
        return Response({"error": "'ewbNo' is required."}, status=400)
    return _run(EwbClient().reject_ewb, d["ewbNo"])


@api_view(["POST"])
def extend_validity(request):
    return _run(EwbClient().extend_validity, request.data if isinstance(request.data, dict) else {})


@api_view(["POST"])
def update_transporter(request):
    d = request.data if isinstance(request.data, dict) else {}
    if not d.get("ewbNo") or not d.get("transporterId"):
        return Response({"error": "'ewbNo' and 'transporterId' are required."}, status=400)
    return _run(EwbClient().update_transporter, d["ewbNo"], d["transporterId"])


@api_view(["POST"])
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
def get_ewb(request, ewb_no):
    return _run(EwbClient().get_ewb, ewb_no)


@api_view(["GET"])
def ewb_gstin_details(request, gstin):
    return _run(EwbClient().get_gstin_details, gstin)


@api_view(["GET"])
def ewb_transporter_details(request, trans_id):
    return _run(EwbClient().get_transporter_details, trans_id)
