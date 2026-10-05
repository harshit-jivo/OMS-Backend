"""Post an approved invoice log to SAP — once.

This used to be the browser's job: the review screen sent its in-memory copy of
the payload to `/api/service-layer/invoice/` and then PATCHed the outcome back
onto the log. Nothing tied the two together, so the same log could be posted
twice (a double click, two reviewers, a retry after a timeout), a closed tab
left a real SAP invoice behind an APPROVED row, and what reached SAP was
whatever the browser sent rather than what was approved. Measured on live:
logs 43, 446 and 20 were posted again minutes after a successful post, and SAP
refused the repeats only because the stock or the order line was already used
up. A partial invoice (half an SO's quantity) has no such luck.

Here the server owns the whole post:

1. Lock the row, require a postable status, and mark it POSTING — committed
   before SAP is called, so a second request sees POSTING and backs off.
2. Post the STORED payload with today's date and, where the company has the
   field, `U_OMS_REF` naming this log.
3. Write the outcome in the same request.

A timeout does not mean SAP refused: the Service Layer usually finishes the
document anyway. So an unknown outcome leaves the log in POSTING, and the next
attempt looks the invoice up in SAP before it is allowed to post again.
"""
import copy
import datetime
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from hana.services.connection import HANAConnection, column_exists
from serviceLayer.service import SAPServiceLayerManager
from serviceLayer.views import _maybe_auto_irn

from ..models import CreditLimitLogs, InvocieHistory, InvoiceLog

logger = logging.getLogger(__name__)

POSTABLE_STATUSES = ('APPROVED', 'ERROR', 'CL_RAISED')

# SAP routinely takes 10-30s to add an invoice; the old 20s cut most slow posts
# off mid-flight and reported them as failures.
POST_TIMEOUT_SECONDS = 60

# How long a POSTING mark means "a request is still waiting on SAP". Past this
# the request that set it is gone (answered, timed out or killed), and the next
# attempt reconciles against SAP instead of refusing.
POSTING_STALE_AFTER = datetime.timedelta(minutes=3)

# SAP's back-date check runs on the Indian calendar; the project clock is UTC,
# which would still say "yesterday" until 05:30 IST.
BUSINESS_TZ = ZoneInfo('Asia/Kolkata')

OMS_REF_FIELD = 'U_OMS_REF'


@dataclass
class PostOutcome:
    http_status: int
    body: dict


def oms_ref_for(log):
    """The value stamped in OINV.U_OMS_REF — NVARCHAR(15) on the OIL company."""
    return f'OMSINV{log.pk}'


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def build_post_body(log, today, stamp_ref):
    """The stored payload as it goes to SAP today. The log's copy is untouched.

    DocDate/TaxDate were fixed when the invoice was BUILT, so an invoice
    approved the next day was refused as back-dated. The due date moves by the
    same number of days, keeping the payment term the biller chose.
    """
    body = copy.deepcopy(log.invoice_payload)
    built_on = _parse_date(body.get('DocDate'))
    due_on = _parse_date(body.get('DocDueDate'))
    body['DocDate'] = today.isoformat()
    body['TaxDate'] = today.isoformat()
    if built_on and due_on:
        body['DocDueDate'] = (due_on + (today - built_on)).isoformat()
    if stamp_ref:
        body[OMS_REF_FIELD] = oms_ref_for(log)
    return body


def _schema(log):
    return SAPServiceLayerManager.schema_for(log.branch)


def company_has_oms_ref(log):
    """Whether this company's OINV carries U_OMS_REF (only OIL does today).

    Sending a field the company lacks makes SAP refuse the whole document. A
    HANA failure answers False: the post goes out without the stamp and the
    reconciliation falls back to matching the lines.
    """
    try:
        with HANAConnection() as conn:
            return column_exists(conn, _schema(log), 'OINV', 'U_OMS_REF')
    except Exception:
        logger.exception('Could not check OINV.U_OMS_REF for invoice log %s', log.pk)
        return False


def _line_key(line):
    def num(value):
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    def whole(value):
        return None if value in (None, '', -1) else int(value)

    return (
        str(line.get('ItemCode') or '').strip().upper(),
        num(line.get('Quantity')),
        whole(line.get('BaseEntry')),
        whole(line.get('BaseLine')),
    )


def find_posted_invoice(log, body):
    """The SAP invoice an earlier attempt created from `body`, or None.

    By U_OMS_REF when the body carried it; otherwise an invoice for the same
    customer on the same DocDate whose lines match exactly, and which no other
    log already claims. Raises when SAP cannot be read: an unknown answer must
    never be taken as "not posted".
    """
    schema = _schema(log)
    with HANAConnection() as conn:
        if body.get(OMS_REF_FIELD):
            rows = conn.execute(
                f'SELECT "DocEntry", "DocNum" FROM "{schema}"."OINV" '
                f'WHERE "U_OMS_REF" = ? AND "CANCELED" = \'N\'',
                [body[OMS_REF_FIELD]],
            )
            return rows[0] if rows else None

        wanted = sorted(_line_key(line) for line in body.get('DocumentLines') or [])
        candidates = conn.execute(
            f'SELECT "DocEntry", "DocNum" FROM "{schema}"."OINV" '
            f'WHERE "CardCode" = ? AND "DocDate" = ? AND "CANCELED" = \'N\'',
            [body.get('CardCode'), body.get('DocDate')],
        )
        claimed = set(
            InvoiceLog.objects.exclude(pk=log.pk)
            .filter(branch=log.branch, sap_doc_entry__isnull=False)
            .values_list('sap_doc_entry', flat=True)
        )
        for candidate in candidates:
            if str(candidate['DocEntry']) in claimed:
                continue
            lines = conn.execute(
                f'SELECT "ItemCode", "Quantity", "BaseEntry", "BaseLine" '
                f'FROM "{schema}"."INV1" WHERE "DocEntry" = ?',
                [candidate['DocEntry']],
            )
            if sorted(_line_key(line) for line in lines) == wanted:
                return candidate
    return None


def sap_error_text(response):
    """SAP's own message, stored the way the review screen always stored it."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or '').strip()[:2000] or f'SAP answered HTTP {response.status_code}.', None
    error = payload.get('error') if isinstance(payload, dict) else None
    message = error.get('message') if isinstance(error, dict) else None
    if isinstance(message, dict):
        message = message.get('value')
    return (str(message).strip() if message else f'SAP answered HTTP {response.status_code}.'), payload


def _history(log, actor, *, payload=None):
    InvocieHistory.objects.create(
        invoice_log=log,
        so_number=log.so_number,
        party_name=log.party_name,
        total_amount=log.total_amount,
        status=log.status,
        rejection_reason=log.rejection_reason,
        error_message=log.error_message,
        invoice_payload=log.invoice_payload if payload is None else payload,
        created_by=actor,
    )


def _latest_posting(log):
    return (
        InvocieHistory.objects
        .filter(invoice_log=log, status='POSTING')
        .order_by('-created_at', '-id')
        .first()
    )


def _mark_posted(log, actor, doc, *, reconciled):
    log.status = 'POSTED_TO_SAP'
    log.sap_doc_num = str(doc.get('DocNum') or '')[:50] or None
    log.sap_doc_entry = str(doc.get('DocEntry') or '')[:50] or None
    # A message from an earlier failed attempt would otherwise sit on a
    # successful invoice (39 posted logs on live carry one).
    log.error_message = None
    log.save()
    _history(log, actor)
    return PostOutcome(201, {
        'status': log.status,
        'DocNum': log.sap_doc_num,
        'DocEntry': log.sap_doc_entry,
        'reconciled': reconciled,
    })


def _failed_status(log):
    # An invoice whose credit-limit request is with JSAP keeps failing the same
    # check until it clears; dropping it to ERROR would hide "Show Flow".
    return 'CL_RAISED' if CreditLimitLogs.objects.filter(invoice_log_id=log.pk).exists() else 'ERROR'


def _conflict(message, log):
    return PostOutcome(409, {
        'error': message,
        'status': log.status,
        'sap_doc_num': log.sap_doc_num,
        'sap_doc_entry': log.sap_doc_entry,
    })


def _claim(pk, actor):
    """Lock the log and mark it POSTING. Returns (log, body) or a PostOutcome."""
    with transaction.atomic():
        try:
            log = InvoiceLog.objects.select_for_update().get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return PostOutcome(404, {'error': 'Invoice log not found'})

        if log.is_deleted:
            return _conflict('This invoice has been deleted. Restore it before posting.', log)
        if log.status == 'POSTED_TO_SAP':
            return _conflict(f'This invoice is already posted to SAP as #{log.sap_doc_num or log.sap_doc_entry}.', log)

        if log.status == 'POSTING':
            posting = _latest_posting(log)
            if posting and timezone.now() - posting.created_at < POSTING_STALE_AFTER:
                return _conflict('This invoice is being posted to SAP right now. Check back in a minute.', log)
            # The attempt that set POSTING never recorded an answer. Ask SAP
            # before sending anything again.
            try:
                found = find_posted_invoice(log, posting.invoice_payload) if posting else None
            except Exception:
                logger.exception('Reconciliation failed for invoice log %s', pk)
                return PostOutcome(503, {
                    'error': 'Could not confirm with SAP whether the earlier attempt went through. '
                             'Nothing was posted; try again shortly.',
                    'status': log.status,
                })
            if found:
                return _mark_posted(log, actor, found, reconciled=True)
        elif log.status not in POSTABLE_STATUSES:
            return _conflict(f'Only an approved or failed invoice can be posted; this one is {log.get_status_display()}.', log)

        if not (log.invoice_payload or {}).get('DocumentLines'):
            return PostOutcome(400, {'error': 'The stored invoice has no document lines to post.'})

        body = build_post_body(log, timezone.now().astimezone(BUSINESS_TZ).date(),
                               stamp_ref=company_has_oms_ref(log))
        log.status = 'POSTING'
        log.save()
        # The history row keeps the body exactly as sent — it is what the
        # reconciliation matches against, and the record of what went to SAP.
        _history(log, actor, payload=body)
        return log, body


def _send(log, body):
    """POST to the Service Layer: (response, error, sent).

    `sent` separates "never left" (login failed, could not connect — safe to
    record as a failure) from "left and no answer came back" (outcome unknown).
    """
    url = f'{settings.HANA_SERVICE_LAYER_URL}/Invoices'
    try:
        # As the approver: the drafter sits under SAP's approval procedure,
        # which would turn the post into a draft.
        session = SAPServiceLayerManager.get_session_for(
            settings.SAP_APPROVER_USER, settings.SAP_APPROVER_PASSWORD, log.branch)
    except Exception as exc:
        return None, str(exc), False
    try:
        response = session.post(url, json=body, timeout=POST_TIMEOUT_SECONDS)
        return response, None, True
    except requests.exceptions.ConnectTimeout as exc:
        return None, f'Could not reach SAP: {exc}', False
    except requests.RequestException as exc:
        return None, str(exc), True
    finally:
        try:
            session.post(f'{settings.HANA_SERVICE_LAYER_URL}/Logout', timeout=10)
        except requests.RequestException:
            pass


def post_invoice_log(pk, actor):
    claimed = _claim(pk, actor)
    if isinstance(claimed, PostOutcome):
        return claimed
    log, body = claimed

    response, error, sent = _send(log, body)

    if response is not None and response.status_code in (200, 201):
        doc = response.json()
        with transaction.atomic():
            log = InvoiceLog.objects.select_for_update().get(pk=pk)
            outcome = _mark_posted(log, actor, doc, reconciled=False)
        _maybe_auto_irn(
            doc.get('DocEntry'), trigger='invoice_create',
            company_db=SAPServiceLayerManager.schema_for(log.branch),
            context=f'invoice DocNum {doc.get("DocNum")} (log {pk}, branch={log.branch})',
        )
        return outcome

    if response is None and sent:
        # The request left, no answer came back: SAP may well have created it.
        try:
            found = find_posted_invoice(log, body)
        except Exception:
            logger.exception('Post-timeout lookup failed for invoice log %s', pk)
            found = None
        with transaction.atomic():
            log = InvoiceLog.objects.select_for_update().get(pk=pk)
            if found:
                return _mark_posted(log, actor, found, reconciled=True)
            log.error_message = (
                f'SAP did not answer ({error}). It may still have created the invoice, '
                'so OMS will check SAP before this invoice can be posted again.'
            )
            log.save(update_fields=['error_message'])
        return PostOutcome(504, {'error': log.error_message, 'status': 'POSTING'})

    # SAP refused, or nothing was sent: safe to record a failure.
    if response is not None:
        message, details = sap_error_text(response)
        http_status = response.status_code if response.status_code >= 400 else 502
    else:
        message, details, http_status = error, None, 502
    with transaction.atomic():
        log = InvoiceLog.objects.select_for_update().get(pk=pk)
        log.status = _failed_status(log)
        log.error_message = message
        log.save()
        _history(log, actor)
    return PostOutcome(http_status, {
        'error': 'SAP Error',
        'details': details if details is not None else {'error': {'message': message}},
        'status': log.status,
    })
