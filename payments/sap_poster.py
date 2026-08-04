"""Synchronous SAP posting, called the moment the final approval lands.

No queue, no worker, no delay: the approver's request carries the SAP call, so
they see the real outcome — a DocEntry or SAP's own error — instead of a
"queued" state they then have to chase.

ONE OMS receipt -> ONE SAP payment. The guarantee rests on a single check: the
document is reloaded under `select_for_update()` immediately before the call and
refused if it already carries a `sap_doc_entry` or is already POSTED.

THE ONE CASE THAT CANNOT BE ANSWERED SYNCHRONOUSLY. If SAP never replies
(timeout, dropped connection), the document may or may not have been committed
there. Guessing either way risks a duplicate payment or a lost one, so the
document goes to SAP_UNKNOWN and resubmission is blocked until
`reconcile_unknown()` — or a human — establishes what really happened.
"""
import logging
import time

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from .models import PaymentReceipt, SapCallLog
from .sap_client import SapError, fetch_document
from .sap_client import post_deposit as sap_post_deposit
from .sap_client import post_incoming_payment as sap_post_payment
from .sap_payloads import redact

logger = logging.getLogger(__name__)

ALREADY_POSTED = 'This payment has already been posted to SAP.'


def _history(document, **kwargs):
    """Append a posting-history row, for receipts only.

    Deposits have no history table, so they are skipped rather than special
    cased at each call site. Wrapped defensively: an audit-trail failure must
    never abort a real SAP post that already succeeded.
    """
    if not isinstance(document, PaymentReceipt):
        return None
    from .services import record_sap_history

    try:
        return record_sap_history(document, **kwargs)
    except Exception:                                    # noqa: BLE001
        logger.exception('Could not write SAP posting history for %s',
                         document.pk)
        return None


def _entity_for(document):
    return ('IncomingPayments' if isinstance(document, PaymentReceipt)
            else 'Deposits')


def _sap_keys(body):
    """(doc_entry, doc_num) from a Service Layer response.

    The two entities name these differently:
        IncomingPayments -> DocEntry / DocNum      (ORCT)
        Deposits         -> DeposId  / DeposNum    (ODPS)
    Several spellings are accepted because the Service Layer version decides
    which it echoes; the table column names are authoritative.
    """
    doc_entry = (body.get('DocEntry') or body.get('DeposId')
                 or body.get('AbsoluteEntry') or body.get('AbsEntry'))
    doc_num = (body.get('DocNum') or body.get('DeposNum')
               or body.get('DepositNumber'))
    return doc_entry, doc_num


def _success_text(doc_entry, doc_num):
    return (f'Payment created successfully.\n'
            f'DocEntry : {doc_entry}\n'
            f'DocNum : {doc_num}')


def _error_text(exc):
    """SAP's own words, kept intact — this is what the UI shows."""
    text = str(exc).strip()
    if exc.sap_code:
        text = f'{text}\n(SAP code {exc.sap_code})'
    return text


def already_posted(document):
    """True when this document is already in SAP."""
    return bool(document.sap_doc_entry) or (
        document.status == document.__class__.Status.POSTED)


def _log_start(document, payload):
    return SapCallLog.objects.create(
        content_type=ContentType.objects.get_for_model(document.__class__),
        object_id=document.pk,
        company_db=document.company_db,
        endpoint=f'/{_entity_for(document)}',
        request_data=redact(payload),
        status=SapCallLog.Status.STARTED,
    )


def post_document(document, payload, *, user=None):
    """Post one document to SAP and record the outcome. Returns the document.

    Never raises for a SAP-side failure — the failure IS the result, and the
    caller shows `document.sap_response` to the user. Only a programming error
    propagates.
    """
    from .services import log_status

    # --- duplicate guard, under a row lock ---------------------------------
    with transaction.atomic():
        fresh = (document.__class__.objects
                 .select_for_update().get(pk=document.pk))
        if already_posted(fresh):
            logger.warning('%s: %s (DocEntry %s)',
                           fresh.pk, ALREADY_POSTED, fresh.sap_doc_entry)
            return fresh
        # Visible while the call is in flight, so a second request cannot start
        # another post and the UI can show "Posting to SAP...".
        fresh.status = fresh.__class__.Status.POSTING_TO_SAP
        fresh.save(update_fields=['status', 'updated_at'])
        _history(fresh, action='POST_STARTED', status='POSTING',
                 response='Posting request sent to SAP.', user=user)

    log = _log_start(fresh, payload)
    started = time.monotonic()

    try:
        if isinstance(fresh, PaymentReceipt):
            body = sap_post_payment(payload, fresh.company_db)
        else:
            body = sap_post_deposit(payload, fresh.company_db)
    except SapError as exc:
        duration = int((time.monotonic() - started) * 1000)
        # No HTTP status means the request never completed: SAP may still have
        # committed it. That is NOT the same as SAP rejecting the document.
        ambiguous = exc.status_code is None
        message = _error_text(exc)

        with transaction.atomic():
            fresh.refresh_from_db()
            if ambiguous:
                fresh.status = fresh.__class__.Status.SAP_UNKNOWN
                fresh.sap_response = (
                    f'{message}\n\nSAP did not respond, so it is not yet known '
                    f'whether the document was created. It will be verified '
                    f'automatically; do not resubmit in the meantime.')
            else:
                fresh.status = fresh.__class__.Status.PENDING_ERROR
                fresh.sap_response = message[:4000]
            fresh.save(update_fields=['status', 'sap_response', 'updated_at'])
            log_status(fresh, to_status=fresh.status, user=user,
                       actor_kind='SAP', reason=message[:500])
            if ambiguous:
                _history(fresh, action='POST_TIMEOUT', status='UNKNOWN',
                         response=('No response received from SAP. Posting '
                                   'status could not be confirmed.\n\n'
                                   + message),
                         user=user)
            else:
                _history(fresh, action='POST_FAILED', status='FAILED',
                         response=message, user=user)

        log.status = SapCallLog.Status.FAILED
        log.http_status = exc.status_code
        log.sap_error_code = exc.sap_code or ''
        log.error_message = message[:2000]
        log.duration_ms = duration
        log.completed_at = timezone.now()
        log.save()
        logger.warning('SAP post failed for %s: %s', fresh.pk, message[:200])
        return fresh

    duration = int((time.monotonic() - started) * 1000)
    doc_entry, doc_num = _sap_keys(body)

    if not doc_entry:
        # 2xx with no usable key. The document probably EXISTS in SAP, so this
        # must not be treated as a plain failure the creator can resubmit.
        message = ('SAP accepted the document but returned no document key. '
                   'Verify in SAP before resubmitting.')
        with transaction.atomic():
            fresh.refresh_from_db()
            fresh.status = fresh.__class__.Status.SAP_UNKNOWN
            fresh.sap_response = message
            fresh.save(update_fields=['status', 'sap_response', 'updated_at'])
            log_status(fresh, to_status=fresh.status, user=user,
                       actor_kind='SAP', reason=message)
            _history(fresh, action='POST_TIMEOUT', status='UNKNOWN',
                     response=message, user=user)
        log.status = SapCallLog.Status.FAILED
        log.response_data = body
        log.error_message = message
        log.duration_ms = duration
        log.completed_at = timezone.now()
        log.save()
        return fresh

    with transaction.atomic():
        fresh.refresh_from_db()
        fresh.sap_doc_entry = doc_entry
        fresh.sap_doc_num = doc_num
        fresh.sap_posted_at = timezone.now()
        fresh.sap_response = _success_text(doc_entry, doc_num)
        fresh.status = fresh.__class__.Status.POSTED
        fresh.save(update_fields=['sap_doc_entry', 'sap_doc_num',
                                  'sap_posted_at', 'sap_response', 'status',
                                  'updated_at'])

        # Capture CheckKey per cheque so those cheques can later be deposited.
        if isinstance(fresh, PaymentReceipt):
            checks = body.get('PaymentChecks') or []
            cheques = [e for e in fresh.methods.all() if e.method == 'CHEQUE']
            for entry, check in zip(cheques, checks):
                key = check.get('CheckKey')
                if key:
                    entry.sap_check_key = key
                    entry.save(update_fields=['sap_check_key'])

        log_status(fresh, to_status=fresh.status, user=user, actor_kind='SAP',
                   reason=f'Posted to SAP as DocEntry {doc_entry}.')
        _history(fresh, action='POST_SUCCESS', status='SUCCESS',
                 response=fresh.sap_response, doc_entry=doc_entry,
                 doc_num=doc_num, user=user)

    log.status = SapCallLog.Status.SUCCESS
    log.response_data = body
    log.sap_doc_entry = doc_entry
    log.sap_doc_num = doc_num
    log.duration_ms = duration
    log.completed_at = timezone.now()
    log.save()
    logger.info('Posted %s to SAP: DocEntry=%s DocNum=%s',
                fresh.pk, doc_entry, doc_num)
    return fresh


def reconcile_unknown(document):
    """Resolve one SAP_UNKNOWN document by asking SAP what happened.

    The only safety net kept from the old worker, and it exists solely for the
    case a synchronous call cannot answer. It never POSTs — it only reads.

    Returns True when the outcome is now known.
    """
    from .services import log_status

    if document.status != document.__class__.Status.SAP_UNKNOWN:
        return True

    if document.sap_doc_entry:
        found = fetch_document(document.sap_doc_entry, document.company_db,
                               entity=_entity_for(document))
        if found:
            doc_entry, doc_num = _sap_keys(found)
            with transaction.atomic():
                document.sap_doc_num = doc_num or document.sap_doc_num
                document.sap_posted_at = document.sap_posted_at or timezone.now()
                document.sap_response = _success_text(doc_entry, doc_num)
                document.status = document.__class__.Status.POSTED
                document.save(update_fields=['sap_doc_num', 'sap_posted_at',
                                             'sap_response', 'status',
                                             'updated_at'])
                log_status(document, to_status=document.status,
                           actor_kind='SYSTEM',
                           reason='Confirmed in SAP during reconciliation.')
                _history(document, action='MANUAL_RECOVERY', status='SUCCESS',
                         response=document.sap_response, doc_entry=doc_entry,
                         doc_num=doc_num)
            return True

    # No DocEntry to check against. SAP holds no OMS reference field, so this
    # cannot be resolved automatically — a human must look.
    logger.error('Document %s needs MANUAL SAP verification (no DocEntry to '
                 'query).', document.pk)
    return False
