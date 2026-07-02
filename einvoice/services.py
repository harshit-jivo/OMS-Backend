"""
Service layer for e-Invoice generate/cancel: pre-submit validation, the NIC call,
and persistence of the response into einvoice_irn (best practice #3 — the IRP
purges data after 24h, so we keep the authenticated record).

Persistence is best-effort: if a NIC call SUCCEEDS but the DB write fails, we log
and still return the NIC result to the caller (never lose an IRN over a DB blip),
signalling the failure via the returned `record is None`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from .client import EInvoiceClient, EInvoiceError
from .models import IrnRecord, EwayBill
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
        result = EInvoiceClient().generate_irn(invoice)
    except EInvoiceError as exc:
        _persist_failure(invoice, ident, order_id, source, exc)
        raise

    record = _persist_success(invoice, ident, order_id, source, result)
    return record, result


def cancel_and_store(irn: str, reason_code, remarks: str):
    """Cancel an IRN at NIC and update the stored record. Returns (record, result)."""
    result = EInvoiceClient().cancel_irn(irn, reason_code, remarks)  # raises EInvoiceError on failure
    record = None
    try:
        record = IrnRecord.objects.filter(irn=irn).first()
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
    result = EInvoiceClient().generate_ewb_by_irn(payload)
    irn_record = None
    try:
        irn = payload.get("Irn")
        if irn:
            irn_record = IrnRecord.objects.filter(irn=irn).first()
    except Exception:
        irn_record = None

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
