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
    """Append a posting event to the document's activity timeline.

    Receipts AND deposits. The old receipt-only guard was a limitation of the
    dropped `payment_sap_posting_history` table, whose foreign key pointed at
    PaymentReceipt — deposit posts were silently discarded here. The timeline is
    generic, so a deposit now keeps the same SAP record a receipt does.

    Wrapped defensively: an audit-trail failure must never abort a real SAP post
    that already succeeded.
    """
    from .services import record_sap_history

    try:
        return record_sap_history(document, **kwargs)
    except Exception:                                    # noqa: BLE001
        logger.exception('Could not write SAP posting history for %s',
                         document.pk)
        return None


def _reopen_approval(document, reason):
    """Return the approval to its final rung after SAP refused the document.

    Wrapped defensively for the same reason as `_history`: the SAP outcome has
    already been recorded on the document, and failing to reopen must not lose
    that or raise into the caller. A warning is enough — the document is still
    correctly in PENDING_ERROR either way.
    """
    from approvals.services import reopen_final_level

    try:
        return reopen_final_level(document, reason=reason)
    except Exception:                                    # noqa: BLE001
        logger.exception('Could not reopen the approval for %s', document.pk)
        return None


def _entity_for(document):
    # BOTH are IncomingPayments now: a receipt is DocType 'C' (customer), a
    # deposit is DocType 'A' (account). The ODPS Deposit object is unused by
    # this company — see sap_payloads.build_deposit.
    return 'IncomingPayments'


def _sap_keys(body):
    """(doc_entry, doc_num, trans_id) from a Service Layer response.

    The two entities name these differently:
        IncomingPayments -> DocEntry / DocNum      (ORCT)
        Deposits         -> DeposId  / DeposNum    (ODPS)
    Several spellings are accepted because the Service Layer version decides
    which it echoes; the table column names are authoritative.

    `trans_id` is ORCT.TransId, the journal-entry key that links the document
    to JDT1. It may be absent — the Service Layer does not always echo it — so
    callers must treat None as "not reported", not as a failure.
    """
    doc_entry = (body.get('DocEntry') or body.get('DeposId')
                 or body.get('AbsoluteEntry') or body.get('AbsEntry'))
    doc_num = (body.get('DocNum') or body.get('DeposNum')
               or body.get('DepositNumber'))
    trans_id = body.get('TransId')
    return doc_entry, doc_num, trans_id


def _success_text(doc_entry, doc_num):
    return (f'Payment created successfully.\n'
            f'DocEntry : {doc_entry}\n'
            f'DocNum : {doc_num}')


# SAP's own messages are written for a consultant reading a trace, not for the
# person who has to fix the document. "Posting period locked; specify an
# alternative date" does not say WHICH date is wrong, or what to do about it.
#
# Each entry adds a plain-language explanation and the action that clears it.
# SAP's exact words are ALWAYS kept underneath — support needs the original, and
# a paraphrase that drifted from it would be worse than no paraphrase at all.
SAP_ERROR_HELP = {
    '-4013': (
        'The accounting period for this payment date is closed in SAP.',
        'Change the payment date to one inside an open period, then post again. '
        'If the date is correct, ask your SAP team to open that period.',
    ),
    '-5002': (
        'SAP rejected one of the accounts or amounts on this payment.',
        'Check the GL account configured for each payment method, and that the '
        'amount applied does not exceed the invoice balance.',
    ),
    '-1000': (
        'SAP rejected a field on the document.',
        'The message below names the field. This usually means a master-data '
        'or configuration mismatch rather than anything wrong with the entry.',
    ),
    # -2028 is SAP's GENERIC "No matching records found". It does NOT mean the
    # business partner is missing -- naming the BP here sent people hunting for
    # a party that was present and active all along. The commonest cause by far
    # is a CHEQUE line whose bank has no House Bank Account defined (Banking >
    # Bank Statements and Reconciliations > House Bank Accounts): SAP cannot
    # resolve where the cheque is deposited and reports no matching record.
    '-2028': (
        'SAP could not match one of the records on this payment.',
        'If the payment includes a cheque, check that the bank has a House '
        'Bank Account set up in this company (Administration > Setup > '
        'Banking > House Bank Accounts). Otherwise check the party, the '
        'invoice and the G/L accounts still exist and are active.',
    ),
    '240005': (
        'The SAP user OMS posts with is not authorised to create payments.',
        'Ask your SAP team to grant that user permission for incoming payments.',
    ),
}


def _error_text(exc):
    """A message the person holding the document can act on.

    Three parts, in the order they are useful:
      1. what went wrong, in plain words
      2. what to do about it
      3. SAP's exact response, verbatim, for support and for the audit trail
    """
    raw = str(exc).strip()
    code = str(exc.sap_code or '').strip()

    help_text = SAP_ERROR_HELP.get(code)
    if not help_text:
        # Unknown code — SAP's words are all there is, so do not dress them up.
        return f'{raw}\n\nSAP code: {code}' if code else raw

    what, action = help_text
    return (
        f'{what}\n\n'
        f'What to do: {action}\n\n'
        f'SAP said: {raw}'
        + (f'\nSAP code: {code}' if code else '')
    )


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

    try:
        if isinstance(fresh, PaymentReceipt):
            body = sap_post_payment(payload, fresh.company_db)
        else:
            body = sap_post_deposit(payload, fresh.company_db)
    except SapError as exc:
        # No HTTP status means the request never completed: SAP may still have
        # committed it. That is NOT the same as SAP rejecting the document.
        ambiguous = exc.status_code is None
        message = _error_text(exc)
        # SAP's own words, kept apart from the message we compose for the
        # person holding the document. A SAP administrator needs the literal
        # string to search their notes and logs against; handing them a
        # rewritten one sends them looking for text SAP never produced.
        raw_error = str(exc).strip()
        raw_code = str(exc.sap_code or '').strip()

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
            fresh.sap_raw_error = raw_error[:4000]
            fresh.sap_raw_error_code = raw_code[:20]
            fresh.save(update_fields=['status', 'sap_response', 'sap_raw_error',
                                      'sap_raw_error_code', 'updated_at'])
            # NO separate log_status here. `_history(...)` below writes the
            # SAP outcome through `record_sap_history`, which already records
            # the document's new status, the reason and the DocEntry. The
            # extra call duplicated the event: tagged it appeared twice,
            # untagged it left an anonymous STATUS_CHANGED row between
            # SAP_POST_STARTED and the outcome. One event, one row.
            if ambiguous:
                _history(fresh, action='POST_TIMEOUT', status='UNKNOWN',
                         response=('No response received from SAP. Posting '
                                   'status could not be confirmed.\n\n'
                                   + message),
                         user=user)
            else:
                _history(fresh, action='POST_FAILED', status='FAILED',
                         response=message, user=user)
                # SAP ANSWERED "no", so nothing was committed there and the
                # final approval did not really stand. Put it back in front of
                # the last approver, who is the one who can correct and retry.
                #
                # Only for a clean rejection — an ambiguous timeout must NOT
                # reopen anything, because the document may exist in SAP and a
                # second approval could post it twice.
                _reopen_approval(fresh, message)

        log.status = SapCallLog.Status.FAILED
        log.http_status = exc.status_code
        log.sap_error_code = exc.sap_code or ''
        log.error_message = message[:2000]
        log.save()
        logger.warning('SAP post failed for %s: %s', fresh.pk, message[:200])
        return fresh

    doc_entry, doc_num, trans_id = _sap_keys(body)

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
            # See the note above: `_history` alone records this outcome.
            _history(fresh, action='POST_TIMEOUT', status='UNKNOWN',
                     response=message, user=user)
        log.status = SapCallLog.Status.FAILED
        log.response_data = body
        log.error_message = message
        log.save()
        return fresh

    with transaction.atomic():
        fresh.refresh_from_db()
        fresh.sap_doc_entry = doc_entry
        fresh.sap_doc_num = doc_num
        # The Service Layer does NOT expose TransId — confirmed against a real
        # posted document: absent from both the POST response and a later GET,
        # while ORCT held it. So fall back to reading the table directly.
        # Best-effort: the payment has already succeeded, and a failed trace
        # lookup must never turn a posted document into an error.
        # Applies to deposits too: they are account-type Incoming Payments and
        # land in the same ORCT table.
        if trans_id is None:
            from .hana_queries import fetch_payment_trans_id
            trans_id = fetch_payment_trans_id(
                company=fresh.company, doc_entry=doc_entry)
        if trans_id is not None:
            fresh.sap_trans_id = int(trans_id)
        fresh.sap_posted_at = timezone.now()
        fresh.sap_response = _success_text(doc_entry, doc_num)
        # Clear the previous attempt's error. A posted document showing an old
        # SAP error would read as though it had failed.
        fresh.sap_raw_error = ''
        fresh.sap_raw_error_code = ''
        fresh.status = fresh.__class__.Status.POSTED
        fresh.save(update_fields=['sap_doc_entry', 'sap_doc_num',
                                  'sap_trans_id', 'sap_posted_at',
                                  'sap_response', 'status',
                                  'sap_raw_error', 'sap_raw_error_code',
                                  'updated_at'])

        # Capture CheckKey per cheque so those cheques can later be deposited.
        #
        # DORMANT since cheques became transfers: we no longer send
        # PaymentChecks, so SAP returns none and this loop does nothing. Kept
        # rather than deleted because BankDeposit still reads sap_check_key,
        # and removing both belongs with the deposit rework — not here. Harmless
        # meanwhile: an empty list simply skips the loop.
        if isinstance(fresh, PaymentReceipt):
            checks = body.get('PaymentChecks') or []
            cheques = [e for e in fresh.methods.all() if e.method == 'CHEQUE']
            for entry, check in zip(cheques, checks):
                key = check.get('CheckKey')
                if key:
                    entry.sap_check_key = key
                    entry.save(update_fields=['sap_check_key'])

        # See the note above: `_history` alone records this outcome.
        _history(fresh, action='POST_SUCCESS', status='SUCCESS',
                 response=fresh.sap_response, doc_entry=doc_entry,
                 doc_num=doc_num, user=user)

        # The journey is over — tell the two people who put their name to the
        # money: the creator and the verifier. SUCCESS only; a failure goes
        # back to the approver who can retry it.
        #
        # Isolated: a notification problem must never turn a payment that SAP
        # accepted into an error, because the money has already moved.
        if isinstance(fresh, PaymentReceipt):
            try:
                from .notification_events import publish_receipt_posted
                publish_receipt_posted(fresh)
            except Exception:                                   # noqa: BLE001
                logger.exception(
                    'SAP post notification failed for receipt %s', fresh.pk)

    log.status = SapCallLog.Status.SUCCESS
    log.response_data = body
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
            doc_entry, doc_num, trans_id = _sap_keys(found)
            with transaction.atomic():
                document.sap_doc_num = doc_num or document.sap_doc_num
                if trans_id is not None:
                    document.sap_trans_id = int(trans_id)
                document.sap_posted_at = document.sap_posted_at or timezone.now()
                document.sap_response = _success_text(doc_entry, doc_num)
                document.status = document.__class__.Status.POSTED
                document.save(update_fields=['sap_doc_num', 'sap_trans_id',
                                             'sap_posted_at',
                                             'sap_response', 'status',
                                             'updated_at'])
                # See the note above: `_history` alone records this outcome.
                _history(document, action='MANUAL_RECOVERY', status='SUCCESS',
                         response=document.sap_response, doc_entry=doc_entry,
                         doc_num=doc_num)
            return True

    # No DocEntry to check against. SAP holds no OMS reference field, so this
    # cannot be resolved automatically — a human must look.
    logger.error('Document %s needs MANUAL SAP verification (no DocEntry to '
                 'query).', document.pk)
    return False
