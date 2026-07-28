from django.conf import settings
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import mapping, qr as qrgen, sap
from . import services, validation
from .client import EInvoiceClient, EInvoiceError
from .errors import build_error_response
from .models import IrnRecord, IrnGenerationLog
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

    The <docentry> path segment is interpreted as a SAP DocEntry by default, or
    as a visible Doc Number when ?id_type=docnum is passed.

    Optional query params:
      ?company_db=JIVO_OIL_HANADB   pick a specific company DB (defaults to the
                                    configured HANA_COMPANY_DB). For a DocNum this
                                    is only a preference — if not found there, the
                                    other known company DBs are searched.
      ?id_type=docentry|docnum      how to interpret the path value (default docentry).
      ?order_id=<id>&source=<label> stamped onto the persisted record (POST).
    """
    company_db = request.query_params.get("company_db") or None
    id_type = (request.query_params.get("id_type") or "docentry").lower()
    try:
        docentry, company_db = sap.resolve_invoice_ref(docentry, id_type, company_db)
        session = sap.get_session(company_db)
        sap_invoice, hsn_map = sap.fetch_invoice_for_irn(docentry, company_db, session=session)
    except sap.SapFetchError as exc:
        return Response({"error": str(exc)}, status=502)

    invoice = mapping.build_irn(sap_invoice, hsn_map)
    errs = validation.validate_invoice(invoice)

    if request.method == "GET":
        return Response({
            "docentry": int(docentry),
            "company_db": company_db or settings.HANA_OIL_COMPANY_DB,
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
        services.log_failure_to_hana(docentry=docentry, invoice=invoice,
                                     error="Pre-submit validation failed.", company_db=company_db)
        return Response(
            {"error": "Mapped invoice failed pre-submit validation; fix these before submitting to NIC.",
             "docentry": int(docentry), "invoice": invoice, "validation_errors": exc.errors},
            status=422,
        )
    except EInvoiceError as exc:
        services.log_failure_to_hana(docentry=docentry, invoice=invoice,
                                     error=str(exc), company_db=company_db)
        resp = build_error_response(exc)
        resp["invoice"] = invoice
        return Response(resp, status=exc.status_code or 502)
    except FileNotFoundError:
        return Response(_KEY_MISSING, status=500)

    # Best-effort HANA mirror (+ QR PNG) and SAP write-back, if enabled.
    services.post_generate_hooks(record, result, company_db=company_db, docentry=int(docentry))

    resp = {"docentry": int(docentry), "company_db": company_db or settings.HANA_COMPANY_DB,
            "result": result}
    warning = services.test_irn_warning(company_db, (result or {}).get("Irn"))
    if warning:
        resp["test_warning"] = warning
    if record is not None:
        resp["record_id"] = record.id
    else:
        resp["persistence_warning"] = (
            "IRN generated at NIC but could not be saved to the database "
            "(check logs / run migrations). The result above is authoritative — store it."
        )
    return Response(resp)


@api_view(["GET"])
def list_invoices(request):
    """
    List recent SAP invoices that DO NOT yet have an IRN, so the user can generate
    them. An invoice is hidden once an IRN exists in ANY source — the SAP add-on
    (@UTL_MDEXTH), OMS_IRN_LOG, or the OMS Django table. Because most recent
    invoices already have an IRN, we scan a wider window from SAP and then return
    only the pending ones (up to `limit`).

    Query params: ?company_db=.. ?limit=25 (max pending returned) ?search=<DocNum|CardName>
    """
    company_db = request.query_params.get("company_db") or None
    search = (request.query_params.get("search") or "").strip()
    try:
        limit = min(int(request.query_params.get("limit", 25)), 100)
    except ValueError:
        limit = 25
    # Scan more invoices than we return, since most already have an IRN and get filtered out.
    scan = min(max(limit * 20, 200), 500)

    base = settings.HANA_SERVICE_LAYER_URL.rstrip("/")
    try:
        session = sap.get_session(company_db)
        flt = ""
        if search:
            if search.isdigit():
                flt = f"&$filter=DocNum eq {search}"
            else:
                flt = f"&$filter=contains(CardName,'{search.replace(chr(39), chr(39) * 2)}')"
        url = (f"{base}/Invoices?$select=DocEntry,DocNum,CardName,DocDate,DocTotal"
               f"&$orderby=DocEntry desc&$top={scan}{flt}")
        # B1 Service Layer caps a page at 20 by default; raise it so the scan window is real.
        resp = session.get(url, headers={"Prefer": f"odata.maxpagesize={scan}"},
                           verify=getattr(settings, "HANA_SSL_VERIFY", False), timeout=90)
        resp.raise_for_status()
        rows = resp.json().get("value", [])
    except Exception as exc:  # noqa: BLE001
        return Response({"error": f"Failed to list invoices from SAP: {exc}"}, status=502)

    docnums = [str(r.get("DocNum")) for r in rows]
    docentries = [r["DocEntry"] for r in rows]
    irn_by_docno = {
        rec.doc_no: rec for rec in
        IrnRecord.objects.filter(doc_no__in=docnums, generation_status="GENERATED")
    }
    last_log = {}
    for lg in IrnGenerationLog.objects.filter(docentry__in=docentries).order_by("docentry", "-created_at"):
        last_log.setdefault(lg.docentry, lg)  # first per docentry = most recent

    # An IRN may already exist in HANA even if the OMS Django table doesn't know it:
    # generated by the SAP add-on (@UTL_MDEXTH) or by OMS (OMS_IRN_LOG). Check both so
    # such invoices don't wrongly show as "not generated".
    from . import oms_irn_log
    hana_irn = oms_irn_log.irn_status_by_docentry(
        docentries, schema=company_db or settings.HANA_COMPANY_DB)

    out = []
    for r in rows:
        # Skip any invoice that already has an IRN anywhere (Django / @UTL_MDEXTH / OMS_IRN_LOG).
        if irn_by_docno.get(str(r.get("DocNum"))) or hana_irn.get(r["DocEntry"]):
            continue
        lg = last_log.get(r["DocEntry"])
        out.append({
            "docentry": r["DocEntry"],
            "docnum": r.get("DocNum"),
            "cardname": r.get("CardName"),
            "docdate": r.get("DocDate"),
            "doctotal": r.get("DocTotal"),
            "irn": None,
            "irn_status": (lg.outcome if lg else None),   # FAILED/SKIPPED from a prior attempt, else None
            "irn_source": None,
            "last_error": (lg.error_message if (lg and lg.outcome == "FAILED") else None),
        })
        if len(out) >= limit:
            break
    return Response({
        "company_db": company_db or settings.HANA_COMPANY_DB,
        "results": out,
        "scanned": len(rows),          # how many recent invoices were examined
        "pending_shown": len(out),
    })


@api_view(["GET"])
def generation_logs(request):
    """
    List IRN auto-generation attempts (einvoice_irn_generation_log).
    Query params: ?outcome=FAILED|SUCCESS|SKIPPED · ?docentry=<n> · ?trigger=<t> · ?limit=<n>
    """
    qs = IrnGenerationLog.objects.all()
    outcome = request.query_params.get("outcome")
    docentry = request.query_params.get("docentry")
    trigger = request.query_params.get("trigger")
    if outcome:
        qs = qs.filter(outcome=outcome.upper())
    if trigger:
        qs = qs.filter(trigger=trigger)
    if docentry and docentry.isdigit():
        qs = qs.filter(docentry=int(docentry))
    try:
        limit = min(int(request.query_params.get("limit", 100)), 500)
    except ValueError:
        limit = 100

    rows = list(qs.values(
        "id", "docentry", "company_db", "environment", "trigger", "attempt_no",
        "outcome", "doc_no", "irn", "ack_no", "error_code", "error_message",
        "validation_errors", "duration_ms", "created_at",
    )[:limit])
    counts = {
        "SUCCESS": IrnGenerationLog.objects.filter(outcome="SUCCESS").count(),
        "FAILED": IrnGenerationLog.objects.filter(outcome="FAILED").count(),
        "SKIPPED": IrnGenerationLog.objects.filter(outcome="SKIPPED").count(),
    }
    return Response({"count": len(rows), "totals": counts, "results": rows})


@api_view(["POST"])
def retry_generation(request):
    """Re-run auto IRN generation for a DocEntry. Body: { "docentry": n, "company_db": "..." }"""
    d = request.data if isinstance(request.data, dict) else {}
    docentry = d.get("docentry")
    if docentry is None:
        return Response({"error": "'docentry' is required."}, status=400)
    log = services.auto_generate_irn(int(docentry), company_db=d.get("company_db") or None, trigger="retry")
    return Response({
        "outcome": getattr(log, "outcome", None),
        "irn": getattr(log, "irn", None),
        "error_code": getattr(log, "error_code", None),
        "error_message": getattr(log, "error_message", None),
        "log_id": getattr(log, "id", None),
    })


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
