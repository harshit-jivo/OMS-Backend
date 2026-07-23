"""
Service layer for e-Invoice generate/cancel: pre-submit validation, the NIC call,
and persistence of the response into einvoice_irn (best practice #3 — the IRP
purges data after 24h, so we keep the authenticated record).

Persistence is best-effort: if a NIC call SUCCEEDS but the DB write fails, we log
and still return the NIC result to the caller (never lose an IRN over a DB blip),
signalling the failure via the returned `record is None`.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from .client import EInvoiceClient, EInvoiceError
from .models import IrnRecord, EwayBill, IrnGenerationLog
from . import validation
from .errors import normalize_error_details

logger = logging.getLogger(__name__)

_IST = dt_timezone(timedelta(hours=5, minutes=30))


class PayloadInvalid(Exception):
    """Raised when the invoice fails pre-submit validation (never reaches NIC)."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("Invoice failed pre-submit validation")


# ---- helpers --------------------------------------------------------------

def current_environment() -> str:
    base = (settings.EINV.get("BASE_URL") or "").lower()
    return "sandbox" if ("sandbox" in base or "gstsandbox" in base) else "production"


def is_test_company(company_db) -> bool:
    """True if company_db (or the configured default) is a non-production test DB."""
    db = company_db or settings.HANA_DB_OIL_NAME
    return db in getattr(settings, "EINV_TEST_COMPANY_DBS", ["TEST_OIL_15122025"])


def test_irn_warning(company_db, irn=None):
    """Warning to CANCEL immediately when an IRN was generated from a test company
    against NIC production (a real live e-invoice for test data). None otherwise."""
    if is_test_company(company_db) and current_environment() == "production":
        db = company_db or settings.HANA_COMPANY_DB
        msg = (f"⚠ Generated from TEST company {db} against NIC PRODUCTION — this is a "
               f"REAL, live e-invoice for test data. CANCEL THIS IRN IMMEDIATELY "
               f"(within 24 hours).")
        return f"{msg} IRN: {irn}" if irn else msg
    return None


def _parse_doc_date(s):
    try:
        return datetime.strptime(str(s), "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def _parse_nic_datetime(s):
    """NIC returns IST timestamps like 'yyyy-mm-dd HH:MM:SS' (a few variants)."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(s), fmt).replace(tzinfo=_IST)
        except ValueError:
            continue
    return None


def _financial_year(d):
    if not d:
        return ""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _identity(invoice: dict) -> dict:
    doc = invoice.get("DocDtls") or {}
    seller = invoice.get("SellerDtls") or {}
    buyer = invoice.get("BuyerDtls") or {}
    doc_date = _parse_doc_date(doc.get("Dt"))
    return {
        "environment": current_environment(),
        "supplier_gstin": seller.get("Gstin", "") or "",
        "doc_type": doc.get("Typ", "") or "",
        "doc_no": doc.get("No", "") or "",
        "doc_date": doc_date,
        "financial_year": _financial_year(doc_date),
        "buyer_gstin": buyer.get("Gstin"),
    }


def _key(ident):
    return dict(
        environment=ident["environment"],
        supplier_gstin=ident["supplier_gstin"],
        financial_year=ident["financial_year"],
        doc_type=ident["doc_type"],
        doc_no=ident["doc_no"],
    )


# ---- persistence ----------------------------------------------------------

def _persist_success(invoice, ident, order_id, source, result):
    try:
        record, _ = IrnRecord.objects.update_or_create(
            **_key(ident),
            defaults=dict(
                order_id=order_id,
                source=source,
                doc_date=ident["doc_date"],
                buyer_gstin=ident["buyer_gstin"],
                generation_status="GENERATED",
                irn=result.get("Irn"),
                ack_no=str(result["AckNo"]) if result.get("AckNo") is not None else None,
                ack_date=_parse_nic_datetime(result.get("AckDt")),
                signed_invoice=result.get("SignedInvoice"),
                signed_qr_code=result.get("SignedQRCode"),
                irp_status=str(result.get("Status")) if result.get("Status") is not None else "ACT",
                ewb_no=str(result["EwbNo"]) if result.get("EwbNo") else None,
                ewb_date=_parse_nic_datetime(result.get("EwbDt")),
                request_payload=invoice,
                response_payload=result,
                error_details=None,
            ),
        )
        return record
    except Exception:
        logger.exception("Persisted IRN generation FAILED to save for doc %s", ident.get("doc_no"))
        return None


def _persist_failure(invoice, ident, order_id, source, exc):
    try:
        existing = IrnRecord.objects.filter(**_key(ident)).first()
        # Never downgrade an already-GENERATED record to FAILED (e.g. duplicate resubmit).
        if existing and existing.generation_status == "GENERATED":
            existing.error_details = normalize_error_details(getattr(exc, "error_details", None))
            existing.save(update_fields=["error_details", "updated_at"])
            return existing
        record, _ = IrnRecord.objects.update_or_create(
            **_key(ident),
            defaults=dict(
                order_id=order_id,
                source=source,
                doc_date=ident["doc_date"],
                buyer_gstin=ident["buyer_gstin"],
                generation_status="FAILED",
                request_payload=invoice,
                error_details=normalize_error_details(getattr(exc, "error_details", None)),
            ),
        )
        return record
    except Exception:
        logger.exception("Could not persist FAILED IRN attempt for doc %s", ident.get("doc_no"))
        return None


# ---- public API -----------------------------------------------------------

def generate_and_store(invoice: dict, *, order_id=None, source=None):
    """
    Validate → generate IRN at NIC → persist. Returns (record, result).
    - Raises PayloadInvalid(errors) if pre-submit validation fails (no NIC call).
    - Raises EInvoiceError on a NIC business/validation error (a FAILED record is saved first).
    - `record` may be None if the NIC call succeeded but the DB write failed.
    """
    errors = validation.validate_invoice(invoice)
    if errors:
        raise PayloadInvalid(errors)

    ident = _identity(invoice)
    try:
        seller_gstin = (invoice.get("SellerDtls") or {}).get("Gstin")
        result = EInvoiceClient(gstin=seller_gstin).generate_irn(invoice)
    except EInvoiceError as exc:
        _persist_failure(invoice, ident, order_id, source, exc)
        raise

    record = _persist_success(invoice, ident, order_id, source, result)
    return record, result


def cancel_and_store(irn: str, reason_code, remarks: str):
    """Cancel an IRN at NIC and update the stored record. Returns (record, result)."""
    # Cancel must be authenticated as the GSTIN that owns the IRN (multi-GSTIN PAN).
    record = IrnRecord.objects.filter(irn=irn).first()
    seller_gstin = record.supplier_gstin if record else None
    result = EInvoiceClient(gstin=seller_gstin).cancel_irn(irn, reason_code, remarks)  # raises on failure
    try:
        if record:
            record.generation_status = "CANCELLED"
            record.irp_status = "CNL"
            record.cancelled_at = _parse_nic_datetime(result.get("CancelDate")) or timezone.now()
            record.cancel_reason_code = str(reason_code)
            record.cancel_remarks = remarks
            record.response_payload = {**(record.response_payload or {}), "cancel": result}
            record.save()
    except Exception:
        logger.exception("IRN %s cancelled at NIC but the record update failed", irn)

    if getattr(settings, "EINV_MIRROR_HANA", False):
        try:
            from . import oms_irn_log
            oms_irn_log.mark_cancelled(irn)
        except Exception:
            logger.exception("OMS_IRN_LOG cancel-stamp failed for IRN %s", irn)
    return record, result


# ---- e-Way Bill persistence ----------------------------------------------

def ewb_environment() -> str:
    urls = " ".join(settings.EWB.get("BASE_URLS") or []).lower()
    return "sandbox" if "sandbox" in urls else "production"


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _ewb_doc_fields(irn_record, request_payload):
    if irn_record is not None:
        return dict(
            supplier_gstin=irn_record.supplier_gstin,
            buyer_gstin=irn_record.buyer_gstin,
            doc_type=irn_record.doc_type,
            doc_no=irn_record.doc_no,
            doc_date=irn_record.doc_date,
        )
    p = request_payload or {}
    return dict(
        supplier_gstin=_first(p, "fromGstin", "FromGstin"),
        buyer_gstin=_first(p, "toGstin", "ToGstin"),
        doc_type=_first(p, "docType", "DocType"),
        doc_no=_first(p, "docNo", "DocNo"),
        doc_date=_parse_doc_date(_first(p, "docDate", "DocDate")),
    )


def _ewb_transport_fields(request_payload):
    p = request_payload or {}

    def g(*keys):
        return _first(p, *keys)

    dist = g("transDistance", "Distance")
    try:
        dist = int(dist) if dist not in (None, "") else None
    except (TypeError, ValueError):
        dist = None
    veh = g("vehicleNo", "VehNo")
    return dict(
        trans_mode=str(g("transMode", "TransMode") or "") or None,
        trans_distance=dist,
        transporter_id=g("transporterId", "TransId"),
        transporter_name=g("transporterName", "TransName"),
        trans_doc_no=g("transDocNo", "TransDocNo"),
        trans_doc_date=_parse_doc_date(g("transDocDate", "TransDocDt")),
        vehicle_no=veh,
        vehicle_type=g("vehicleType", "VehType"),
        part_b_updated=bool(veh),
    )


def store_ewb(result, *, environment, irn_record=None, order_id=None, request_payload=None):
    """
    Persist an EWB generation result into einvoice_ewaybill (keyed by ewb_no).
    Handles both response shapes: EWB-by-IRN (EwbNo/EwbDt/EwbValidTill) and the
    standalone EWB system (ewayBillNo/ewayBillDate/validUpto). Best-effort.
    """
    ewb_no = _first(result, "EwbNo", "ewayBillNo", "ewbNo")
    if not ewb_no:
        logger.warning("EWB result had no e-way-bill number; not persisted")
        return None
    try:
        defaults = dict(
            environment=environment,
            irn_record=irn_record,
            order_id=order_id,
            ewb_date=_parse_nic_datetime(_first(result, "EwbDt", "ewayBillDate")),
            valid_till=_parse_nic_datetime(_first(result, "EwbValidTill", "validUpto")),
            ewb_status="ACT",
            generation_status="GENERATED",
            response_payload=result,
            request_payload=request_payload,
            error_details=None,
        )
        defaults.update(_ewb_doc_fields(irn_record, request_payload))
        defaults.update(_ewb_transport_fields(request_payload))
        record, _ = EwayBill.objects.update_or_create(ewb_no=str(ewb_no), defaults=defaults)
        return record
    except Exception:
        logger.exception("Failed to persist EWB %s", ewb_no)
        return None


def ewb_by_irn_and_store(payload: dict, *, order_id=None):
    """
    Generate an e-Way Bill from an IRN at NIC and persist it (linked to the IRN
    record). Returns (record, result). Raises EInvoiceError on a NIC failure.
    Validation of the payload (incl. ExpShipDtls.Gstin) is the caller's job.
    """
    irn_record = None
    try:
        irn = payload.get("Irn")
        if irn:
            irn_record = IrnRecord.objects.filter(irn=irn).first()
    except Exception:
        irn_record = None

    # Authenticate as the GSTIN that owns the IRN (multi-GSTIN PAN).
    seller_gstin = irn_record.supplier_gstin if irn_record else None
    result = EInvoiceClient(gstin=seller_gstin).generate_ewb_by_irn(payload)

    record = store_ewb(result, environment=current_environment(), irn_record=irn_record,
                        order_id=order_id, request_payload=payload)

    # Stamp the EWB no/date back onto the IRN record for convenience.
    try:
        ewb_no = _first(result, "EwbNo", "ewbNo")
        if irn_record is not None and ewb_no:
            irn_record.ewb_no = str(ewb_no)
            irn_record.ewb_date = _parse_nic_datetime(_first(result, "EwbDt")) or irn_record.ewb_date
            irn_record.save(update_fields=["ewb_no", "ewb_date", "updated_at"])
    except Exception:
        logger.exception("Could not stamp EWB no on IRN record")
    return record, result


def store_standalone_ewb(result, *, order_id=None, request_payload=None):
    """Persist a standalone (non-IRN) EWB generation result."""
    return store_ewb(result, environment=ewb_environment(), order_id=order_id, request_payload=request_payload)


# ---- automatic IRN generation from a SAP invoice (with audit log) ---------

def post_generate_hooks(record, result, *, company_db=None, docentry=None):
    """
    Best-effort side effects after a successful IRN generation, gated by settings:
      - EINV_QR_SAVE_DIR   -> write the QR PNG file to the shared folder
      - EINV_MIRROR_HANA   -> write the row into HANA OMS_IRN_LOG (UDO-shaped)
      - EINV_SAP_WRITEBACK -> write the IRN back onto the SAP invoice (e-Billing)
    Never raises; failures are logged so they can't break the IRN flow.
    """
    # 1. QR PNG to the shared folder (path is reused for OMS_IRN_LOG.U_UTL_QRPT).
    qr_path = None
    if record is not None and getattr(settings, "EINV_QR_SAVE_DIR", ""):
        try:
            qr_path = save_qr_to_dir(record, settings.EINV_QR_SAVE_DIR)
        except Exception:
            logger.exception("QR file-save hook failed for doc %s", getattr(record, "doc_no", None))

    # 2. Row into HANA OMS_IRN_LOG (mirrors the SAP add-on's @UTL_MDEXTH shape).
    if record is not None and getattr(settings, "EINV_MIRROR_HANA", False):
        try:
            from . import oms_irn_log
            oms_irn_log.write_success(record, docentry=docentry, qr_path=qr_path, schema=company_db)
        except Exception:
            logger.exception("OMS_IRN_LOG hook failed for doc %s", getattr(record, "doc_no", None))

    if docentry is not None and getattr(settings, "EINV_SAP_WRITEBACK", False) and result.get("Irn"):
        try:
            from . import sap
            sap.write_irn_to_invoice(docentry, result, company_db)
        except Exception:
            logger.exception("SAP write-back hook failed for DocEntry %s", docentry)


def save_qr_to_dir(record, directory: str):
    """Write the record's signed QR as a .png into `directory` — which may be a
    local path, a mapped drive, or a UNC share on another server
    (e.g. \\\\JIVO-APP\\OMS_Attachments\\Bitmap). Returns the path written or None.

    Writes to a temp name then renames, so a reader never sees a half-written file.
    Best-effort: the caller wraps this and never lets a failure break IRN generation.
    """
    import os
    from . import qr as qrgen

    if not record.signed_qr_code:
        logger.warning("QR file-save skipped: no signed QR for doc %s", record.doc_no)
        return None

    pattern = getattr(settings, "EINV_QR_FILENAME", "{doc_no}.png") or "{doc_no}.png"
    name = pattern.format(
        doc_no=record.doc_no or "unknown",
        irn=record.irn or "unknown",
        ack_no=record.ack_no or "",
        env=record.environment or "",
    )
    name = os.path.basename(name)                       # never let the pattern escape the dir
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")

    png = qrgen.make_qr_png(record.signed_qr_code)

    # If SMB credentials are configured, authenticate to the share explicitly
    # (the Django process account may not have network access on its own).
    smb_user = getattr(settings, "EINV_QR_SMB_USERNAME", "") or ""
    if smb_user:
        path = _save_png_smb(directory, name, png, smb_user,
                             getattr(settings, "EINV_QR_SMB_PASSWORD", "") or "")
    else:
        os.makedirs(directory, exist_ok=True)           # no-op if the share is already there
        path = os.path.join(directory, name)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, path)                           # atomic swap into place
    logger.info("Saved IRN QR PNG for doc %s -> %s", record.doc_no, path)
    return path


def _save_png_smb(directory: str, name: str, png: bytes, username: str, password: str) -> str:
    """Write PNG bytes to a UNC share using explicit SMB credentials
    (smbprotocol). `directory` is like \\\\JIVO-APP\\OMS_Attachments\\Bitmap.
    Username may be 'user' or 'DOMAIN\\user'. Temp-write then rename (atomic)."""
    import ntpath
    import smbclient

    server = directory.strip("\\/").replace("/", "\\").split("\\")[0]   # JIVO-APP
    smbclient.register_session(server, username=username, password=password)
    try:
        smbclient.makedirs(directory, exist_ok=True)
    except Exception:  # noqa: BLE001 — dir usually already exists
        pass
    path = ntpath.join(directory, name)
    tmp = f"{path}.tmp"
    with smbclient.open_file(tmp, mode="wb") as fh:
        fh.write(png)
    try:
        smbclient.remove(path)                          # rename won't overwrite on SMB
    except Exception:  # noqa: BLE001 — fine if it doesn't exist yet
        pass
    smbclient.rename(tmp, path)
    return path


def log_failure_to_hana(*, docentry, invoice, error, company_db=None):
    """Write an 'F' row into OMS_IRN_LOG for a failed manual/interactive attempt
    (the auto path writes its own via auto_generate_irn). Gated + best-effort."""
    if not getattr(settings, "EINV_MIRROR_HANA", False):
        return
    try:
        from . import oms_irn_log
        doc = (invoice or {}).get("DocDtls") or {}
        oms_irn_log.write_failure(docentry=docentry, doc_no=doc.get("No"),
                                  doc_type=doc.get("Typ"), error=error, schema=company_db)
    except Exception:
        logger.exception("OMS_IRN_LOG manual F-row write failed for DocEntry %s", docentry)


def _first_error(exc):
    """(code, message) from the first NIC error detail on an EInvoiceError."""
    details = normalize_error_details(getattr(exc, "error_details", None))
    if isinstance(details, list) and details and isinstance(details[0], dict):
        return str(details[0].get("code") or ""), str(details[0].get("message") or str(exc))
    return "", str(exc)


_IRN_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _duplicate_irn(invoice, exc):
    """For a 2150 (Duplicate IRN) error, return the already-registered IRN.

    First tries to read a 64-char IRN straight out of the NIC error payload
    (some responses embed it); otherwise falls back to a NIC lookup by document
    details (Typ + No + Dt). Best-effort — returns None if it can't be found.
    """
    try:
        m = _IRN_RE.search(json.dumps(getattr(exc, "error_details", None)))
        if m:
            return m.group(0)
    except Exception:  # noqa: BLE001
        pass
    try:
        doc = invoice.get("DocDtls") or {}
        seller_gstin = (invoice.get("SellerDtls") or {}).get("Gstin")
        res = EInvoiceClient(gstin=seller_gstin).get_irn_by_doc(
            str(doc.get("Typ") or "INV"), str(doc.get("No") or ""), str(doc.get("Dt") or "")
        )
        if isinstance(res, dict):
            return res.get("Irn") or (_IRN_RE.search(json.dumps(res)) or [None])[0]
    except Exception:  # noqa: BLE001
        logger.warning("Could not look up existing IRN for duplicate doc %s",
                       (invoice.get("DocDtls") or {}).get("No"))
    return None


def auto_generate_irn(docentry, *, company_db=None, trigger="manual", order_id=None):
    """
    Fetch a SAP invoice by DocEntry, map it, generate the IRN, and record the
    attempt in IrnGenerationLog — always. Best-effort: never raises, so it is
    safe to call inline from the invoice-create flow or a polling job.

    Returns the IrnGenerationLog row. Skips (logs SKIPPED) if the invoice already
    has a GENERATED IRN, so it is safe to re-run.
    """
    import time
    from . import sap, mapping  # local import avoids any app-load ordering issues

    started = time.monotonic()
    attempt_no = IrnGenerationLog.objects.filter(docentry=docentry).count() + 1

    def _log(outcome, **fields):
        # Mirror failures into HANA OMS_IRN_LOG as 'F' rows (successes are written
        # by post_generate_hooks; SKIPPED/duplicate is not a real attempt row).
        if outcome == "FAILED" and getattr(settings, "EINV_MIRROR_HANA", False):
            try:
                from . import oms_irn_log
                oms_irn_log.write_failure(docentry=docentry, doc_no=fields.get("doc_no"),
                                          error=fields.get("error_message") or "", schema=company_db)
            except Exception:
                logger.exception("OMS_IRN_LOG F-row write failed for DocEntry %s", docentry)
        try:
            return IrnGenerationLog.objects.create(
                docentry=int(docentry),
                company_db=company_db,
                environment=current_environment(),
                trigger=trigger,
                attempt_no=attempt_no,
                outcome=outcome,
                duration_ms=int((time.monotonic() - started) * 1000),
                **fields,
            )
        except Exception:
            logger.exception("Failed to write IrnGenerationLog for DocEntry %s", docentry)
            return None

    # 1. fetch + map
    try:
        sap_invoice, hsn_map = sap.fetch_invoice_for_irn(docentry, company_db)
        invoice = mapping.build_irn(sap_invoice, hsn_map)
    except Exception as exc:  # noqa: BLE001 — SAP fetch / mapping failure
        logger.exception("auto IRN: fetch/map failed for DocEntry %s", docentry)
        return _log("FAILED", error_code="FETCH", error_message=str(exc))

    doc_no = (invoice.get("DocDtls") or {}).get("No")

    # 2. skip if already generated
    try:
        ident = _identity(invoice)
        existing = IrnRecord.objects.filter(**_key(ident), generation_status="GENERATED").first()
        if existing:
            return _log("SKIPPED", doc_no=doc_no, irn=existing.irn, ack_no=existing.ack_no,
                        irn_record=existing, error_message="IRN already generated for this invoice.")
    except Exception:
        logger.exception("auto IRN: identity/skip check failed for DocEntry %s", docentry)

    # 3. generate (validation + NIC + persist)
    try:
        record, result = generate_and_store(invoice, order_id=order_id, source=f"AUTO:OINV:{docentry}")
        post_generate_hooks(record, result, company_db=company_db, docentry=docentry)
        return _log("SUCCESS", doc_no=doc_no, irn=result.get("Irn"),
                    ack_no=str(result.get("AckNo")) if result.get("AckNo") is not None else None,
                    irn_record=record, error_message=test_irn_warning(company_db, result.get("Irn")))
    except PayloadInvalid as exc:
        return _log("FAILED", doc_no=doc_no, error_code="VALIDATION",
                    error_message="Pre-submit validation failed.", validation_errors=exc.errors)
    except EInvoiceError as exc:
        code, msg = _first_error(exc)
        # Duplicate at NIC (2150): the invoice is already e-invoiced there but we
        # have no local record. Surface the existing IRN in the log rather than a
        # bare error, and treat it as SKIPPED (nothing to regenerate).
        if str(code) == "2150":
            existing = _duplicate_irn(invoice, exc)
            return _log(
                "SKIPPED", doc_no=doc_no, irn=existing, error_code="2150",
                error_message=(f"Duplicate — already registered at NIC (IRN {existing})."
                               if existing else "Duplicate IRN at NIC (existing IRN could not be retrieved)."),
            )
        return _log("FAILED", doc_no=doc_no, error_code=code or "NIC", error_message=msg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto IRN: unexpected failure for DocEntry %s", docentry)
        return _log("FAILED", doc_no=doc_no, error_code="ERROR", error_message=str(exc))


def auto_generate_irn_async(docentry, *, company_db=None, trigger="invoice_create", order_id=None):
    """Fire auto_generate_irn in a background thread (does not block the caller)."""
    import threading

    def _run():
        try:
            auto_generate_irn(docentry, company_db=company_db, trigger=trigger, order_id=order_id)
        except Exception:
            logger.exception("auto IRN async thread crashed for DocEntry %s", docentry)

    threading.Thread(target=_run, daemon=True, name=f"auto-irn-{docentry}").start()


def mark_ewb_cancelled(ewb_no, reason_code, remarks, result=None):
    """Update a stored EWB to CANCELLED. Best-effort; returns the record or None."""
    try:
        record = EwayBill.objects.filter(ewb_no=str(ewb_no)).first()
        if not record:
            return None
        record.generation_status = "CANCELLED"
        record.ewb_status = "CNL"
        record.cancelled_at = timezone.now()
        record.cancel_reason_code = str(reason_code) if reason_code is not None else None
        record.cancel_remarks = remarks
        if result:
            record.response_payload = {**(record.response_payload or {}), "cancel": result}
        record.save()
        return record
    except Exception:
        logger.exception("EWB %s cancelled at NIC but the record update failed", ewb_no)
        return None


def record_ewb_closure(ewb_no, closure_date, remarks, result=None):
    """
    Record a voluntary EWB closure in response_payload (no dedicated 'Closed'
    status/column yet — see NIC advisory 17.06.2026 §11). Best-effort.
    """
    try:
        record = EwayBill.objects.filter(ewb_no=str(ewb_no)).first()
        if not record:
            return None
        record.response_payload = {
            **(record.response_payload or {}),
            "closure": {"closureDate": closure_date, "remarks": remarks, "result": result},
        }
        record.save(update_fields=["response_payload", "updated_at"])
        return record
    except Exception:
        logger.exception("EWB %s closed at NIC but the record update failed", ewb_no)
        return None
