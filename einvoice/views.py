from django.conf import settings
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import mapping, qr as qrgen, sap
from . import services, validation
from .client import EInvoiceClient, EInvoiceError
from .errors import build_error_response
from .models import IrnRecord
from .sample import sample_invoice

_KEY_MISSING = {"error": "GST public key not found. Set EINV_PUBLIC_KEY_PATH to your .pem/.cer file."}


def _run(fn, *args, **kwargs):
    """Run a client call and convert errors into clean JSON responses."""
    try:
        return Response(fn(*args, **kwargs))
    except EInvoiceError as exc:
        return Response(build_error_response(exc), status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)


@api_view(["GET"])
def health(request):
    """Quick check that config is present (does NOT hit the NIC API)."""
    cfg = settings.EINV
    missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD", "GSTIN") if not cfg.get(k)]
    import os
    key_ok = os.path.exists(cfg["PUBLIC_KEY_PATH"])
    return Response({
        "ok": not missing and key_ok,
        "missing_credentials": missing,
        "public_key_found": key_ok,
        "public_key_path": cfg["PUBLIC_KEY_PATH"],
        "base_url": cfg["BASE_URL"],
    })


@api_view(["GET", "POST"])
def get_token(request):
    """
    Run the auth handshake against NIC and report the result. Useful for testing
    authentication on its own (e.g. from Requestly) without generating an IRN.
    Send with an empty body. The AuthToken is masked in the response.
    """
    try:
        session = EInvoiceClient()._get_session(force=True)
    except EInvoiceError as exc:
        return Response(
            {"ok": False, "error": str(exc), "details": exc.error_details},
            status=exc.status_code or 502,
        )
    tok = session.auth_token
    return Response({
        "ok": True,
        "auth_token_masked": (tok[:4] + "..." + tok[-4:]) if len(tok) > 8 else "***",
        "sek_bytes": len(session.sek),
        "expires_at": session.expires_at.isoformat(),
    })


@api_view(["POST"])
def validate_irn(request):
    """
    Validate an invoice payload against the GSTN regexes + arithmetic rules
    WITHOUT calling NIC. Body = the invoice JSON. Returns {valid, validation_errors}.
    """
    invoice = request.data
    if not isinstance(invoice, dict) or not invoice:
        return Response({"error": "Request body must be the invoice JSON object."}, status=400)
    errs = validation.validate_invoice(invoice)
    return Response({"valid": not errs, "error_count": len(errs), "validation_errors": errs})


@api_view(["POST"])
def generate_irn(request):
    """
    POST a full e-invoice JSON payload as the request body, e.g.:
        { "Version": "1.1", "TranDtls": {...}, "DocDtls": {...}, ... }
    Optional query params: ?order_id=<oms order id>&source=<label>.

    Runs pre-submit validation, generates the IRN at NIC, persists the response
    to einvoice_irn, and returns the NIC result (Irn, AckNo, SignedQRCode, ...)
    plus the stored record_id.
    """
    invoice = request.data
    if not isinstance(invoice, dict) or not invoice:
        return Response({"error": "Request body must be the invoice JSON object."}, status=400)

    order_id = request.query_params.get("order_id")
    order_id = int(order_id) if order_id and order_id.isdigit() else None
    source = request.query_params.get("source")

    try:
        record, result = services.generate_and_store(invoice, order_id=order_id, source=source)
    except services.PayloadInvalid as exc:
        return Response(
            {"error": "Payload failed pre-submit validation; fix these before submitting to NIC.",
             "validation_errors": exc.errors},
            status=422,
        )
    except EInvoiceError as exc:
        return Response(build_error_response(exc), status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)

    resp = {"result": result}
    if record is not None:
        resp["record_id"] = record.id
    else:
        resp["persistence_warning"] = (
            "IRN generated at NIC but could not be saved to the database "
            "(check logs / run migrations). The result above is authoritative — store it."
        )
    return Response(resp)


@api_view(["GET", "POST"])
def irn_from_invoice(request, docentry):
    """
    Build an IRN payload straight from a SAP B1 invoice (OINV DocEntry).

    GET  -> fetch + map + validate only (preview; does NOT call NIC). Returns
            { docentry, company_db, invoice, valid, validation_errors }.
    POST -> the above, then generate the IRN at NIC and persist it (fails with
            422 if pre-submit validation does not pass — no NIC call in that case).

    Optional query params:
      ?company_db=JIVO_OIL_HANADB   pick a specific company DB (defaults to the
                                    configured HANA_COMPANY_DB).
      ?order_id=<id>&source=<label> stamped onto the persisted record (POST).
    """
    company_db = request.query_params.get("company_db") or None
    try:
        sap_invoice, hsn_map = sap.fetch_invoice_for_irn(docentry, company_db)
    except sap.SapFetchError as exc:
        return Response({"error": str(exc)}, status=502)

    invoice = mapping.build_irn(sap_invoice, hsn_map)
    errs = validation.validate_invoice(invoice)

    if request.method == "GET":
        return Response({
            "docentry": int(docentry),
            "company_db": company_db or settings.HANA_COMPANY_DB,
            "doc_no": invoice.get("DocDtls", {}).get("No"),
            "invoice": invoice,
            "valid": not errs,
            "error_count": len(errs),
            "validation_errors": errs,
        })

    # POST -> generate + store.
    order_id = request.query_params.get("order_id")
    order_id = int(order_id) if order_id and order_id.isdigit() else None
    source = request.query_params.get("source") or f"OINV:{docentry}"
    try:
        record, result = services.generate_and_store(invoice, order_id=order_id, source=source)
    except services.PayloadInvalid as exc:
        return Response(
            {"error": "Mapped invoice failed pre-submit validation; fix these before submitting to NIC.",
             "docentry": int(docentry), "invoice": invoice, "validation_errors": exc.errors},
            status=422,
        )
    except EInvoiceError as exc:
        resp = build_error_response(exc)
        resp["invoice"] = invoice
        return Response(resp, status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)

    resp = {"docentry": int(docentry), "result": result}
    if record is not None:
        resp["record_id"] = record.id
    else:
        resp["persistence_warning"] = (
            "IRN generated at NIC but could not be saved to the database "
            "(check logs / run migrations). The result above is authoritative — store it."
        )
    return Response(resp)


@api_view(["POST"])
def generate_irn_sample(request):
    """
    Convenience endpoint: builds a minimal valid invoice from the configured
    GSTIN and generates an IRN. Body (optional):
        { "buyer_gstin": "...", "doc_no": "INV-1", "doc_date": "19/06/2026" }
    """
    cfg = settings.EINV
    data = request.data if isinstance(request.data, dict) else {}
    invoice = sample_invoice(
        seller_gstin=cfg["GSTIN"],
        buyer_gstin=data.get("buyer_gstin", cfg["GSTIN"]),
        doc_no=data.get("doc_no", "INV-1"),
        doc_date=data.get("doc_date", "19/06/2026"),
    )
    try:
        result = EInvoiceClient().generate_irn(invoice)
    except EInvoiceError as exc:
        return Response(
            {"error": str(exc), "details": exc.error_details, "sent_invoice": invoice},
            status=exc.status_code or 502,
        )
    return Response({"invoice": invoice, "result": result})


@api_view(["POST"])
def cancel_irn(request):
    """Body: { "irn": "...", "reason_code": "2", "remarks": "..." }
    reason_code: 1-Duplicate, 2-Data entry mistake, 3-Order cancelled, 4-Other.
    Cancels at NIC and updates the stored record (status CANCELLED)."""
    d = request.data if isinstance(request.data, dict) else {}
    irn = d.get("irn")
    if not irn:
        return Response({"error": "'irn' is required."}, status=400)
    try:
        record, result = services.cancel_and_store(irn, d.get("reason_code", "2"), d.get("remarks", "Cancelled"))
    except EInvoiceError as exc:
        return Response(build_error_response(exc), status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)
    resp = {"result": result}
    if record is not None:
        resp["record_id"] = record.id
    return Response(resp)


@api_view(["GET"])
def get_irn_details(request, irn):
    """GET the details of a previously generated IRN."""
    return _run(EInvoiceClient().get_irn_details, irn)


@api_view(["GET"])
def get_irn_by_doc(request):
    """Query params: ?doctype=INV&docnum=...&docdate=dd/mm/yyyy"""
    q = request.query_params
    missing = [k for k in ("doctype", "docnum", "docdate") if not q.get(k)]
    if missing:
        return Response({"error": f"Missing query params: {', '.join(missing)}"}, status=400)
    return _run(EInvoiceClient().get_irn_by_doc, q["doctype"], q["docnum"], q["docdate"])


@api_view(["GET"])
def get_rejected_irns(request):
    """Query param: ?date=dd/mm/yyyy"""
    date = request.query_params.get("date")
    if not date:
        return Response({"error": "Query param 'date' (dd/mm/yyyy) is required."}, status=400)
    return _run(EInvoiceClient().get_rejected_irns, date)


@api_view(["GET"])
def get_gstin_details(request, gstin):
    """GET GSTIN master details."""
    return _run(EInvoiceClient().get_gstin_details, gstin)


@api_view(["GET"])
def sync_gstin(request, gstin):
    """Force a fresh sync of a GSTIN from the GST common portal."""
    return _run(EInvoiceClient().sync_gstin, gstin)


@api_view(["POST"])
def generate_ewb_by_irn(request):
    """Body: e-Way Bill payload incl. Irn + transport details
    (Distance, TransMode, TransId, TransName, TransDocNo, TransDocDt, VehNo, VehType) and,
    per the 17.06.2026 advisory, ExpShipDtls.Gstin (mandatory — a valid GSTIN or 'URP')."""
    d = request.data if isinstance(request.data, dict) else {}
    errs = validation.validate_ewb_by_irn(d)
    if errs:
        return Response(
            {"error": "Payload failed pre-submit validation; fix these before submitting to NIC.",
             "validation_errors": errs},
            status=422,
        )
    order_id = request.query_params.get("order_id")
    order_id = int(order_id) if order_id and order_id.isdigit() else None
    try:
        record, result = services.ewb_by_irn_and_store(d, order_id=order_id)
    except EInvoiceError as exc:
        return Response(build_error_response(exc), status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)
    resp = {"result": result}
    if record is not None:
        resp["record_id"] = record.id
    else:
        resp["persistence_warning"] = "EWB generated at NIC but could not be saved (check logs / run migrations)."
    return Response(resp)


@api_view(["GET"])
def get_ewb_by_irn(request, irn):
    """GET the e-Way Bill linked to an IRN."""
    return _run(EInvoiceClient().get_ewb_by_irn, irn)


@api_view(["GET"])
def heartbeat(request):
    """Unencrypted NIC heartbeat/ping (no auth)."""
    return _run(EInvoiceClient().health_ping)


# ---- Signed QR code -> printable image ------------------------------------

def irn_qr_png(request, irn):
    """
    GET the NIC signed QR of a stored IRN as a PNG image (for print / <img src>).
    Renders IrnRecord.signed_qr_code verbatim so the NIC verifier app can validate it.
    """
    record = IrnRecord.objects.filter(irn=irn).first()
    if not record or not record.signed_qr_code:
        return HttpResponse("No signed QR stored for this IRN.", status=404, content_type="text/plain")
    try:
        png = qrgen.make_qr_png(record.signed_qr_code)
    except qrgen.QrUnavailable as exc:
        return HttpResponse(str(exc), status=500, content_type="text/plain")
    resp = HttpResponse(png, content_type="image/png")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"  # QR never changes for an IRN
    return resp


@api_view(["POST"])
def render_qr(request):
    """
    Render any SignedQRCode string into a QR image without a DB lookup.
    Body: { "data": "<SignedQRCode string>" } -> { "data_uri": "data:image/png;base64,..." }.
    Useful right after generation (frontend already has result.SignedQRCode).
    """
    d = request.data if isinstance(request.data, dict) else {}
    data = d.get("data") or d.get("signed_qr_code")
    if not data:
        return Response({"error": "'data' (the SignedQRCode string) is required."}, status=400)
    try:
        return Response({"data_uri": qrgen.make_qr_data_uri(data)})
    except qrgen.QrUnavailable as exc:
        return Response({"error": str(exc)}, status=500)
