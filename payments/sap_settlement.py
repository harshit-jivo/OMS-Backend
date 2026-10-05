"""Settling a final approval against SAP, with no permanently stuck state.

THE FAILURE THIS REPLACES
-------------------------
The final approval set POSTING_TO_SAP, committed, and handed the SAP call to
`transaction.on_commit`. That callback is NOT durable: a restart between the
commit and the callback loses it outright. The document was then left in
POSTING_TO_SAP for ever, and every guard refused to touch it —
`permissions.may_act_on` and `workflow_flow._guard_postable` both read that
status as "a call is in flight, wait for it". Observed on
RCP-OIL-20260919-000003: approved 09:38 on 19 Sep, zero SAP call logs, still
stuck 43 hours later with can_decide, can_retry_sap and can_edit all false.

The guard was right to exist — two Incoming Payments for one receipt is the
worst outcome in this module — and wrong in what it could see. POSTING_TO_SAP
means two different things and nothing distinguished them.

WHAT DISTINGUISHES THEM NOW
---------------------------
Two facts, both already durable, read together:

  1. `document.status == POSTING_TO_SAP` — the INTENT, committed inside the
     approval's own transaction. It survives any crash.
  2. `SapCallLog` — the ATTEMPT. A row is written with status STARTED
     immediately BEFORE the HTTP request leaves (`sap_poster._log_start`, which
     autocommits outside any atomic block), and updated to SUCCESS or FAILED
     when SAP answers.

From that pair every case is decidable:

    intent, no log row at all      the call never left. SAP cannot have it.
                                   -> POST. No verification needed.

    intent, STARTED row, fresh     a call is genuinely running right now.
                                   -> LEAVE IT. This is the case the old
                                      guard was written for.

    intent, STARTED row, stale     the worker died mid-call. SAP MAY hold the
                                   document.
                                   -> ASK SAP before doing anything.

The stale case is the only one needing SAP, and it is the only one where a
blind retry could duplicate a payment.

IDEMPOTENCY
-----------
`sap_payloads.with_reference` guarantees every payload carries `OMS <doc_no>`
in Comments, so `hana_queries.fetch_payment_by_reference` can answer "does SAP
already have this document?" That lookup is the idempotency key. It raises
rather than returning empty when SAP is unreachable — a silent "not found"
would be read as permission to post a second payment.

THE GUARANTEE
-------------
After `settle()` the document is in exactly one of:

  * POSTED, with DocEntry and DocNum, flow complete;
  * PENDING_ERROR, at the final stage, retryable by the effective approver;
  * POSTING_TO_SAP with a LIVE call in flight, which will move it;
  * POSTING_TO_SAP with SAP unreachable — retry still offered, because
    `posting_is_in_flight` is false and the guards ask that, not the status.

There is no state in which the approver has neither a result nor a retry.
"""
import logging
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from .models import BankDeposit, PaymentReceipt, SapCallLog
from .sap_payloads import oms_reference

logger = logging.getLogger(__name__)

#: How long a STARTED call log may be the newest thing about a document before
#: the attempt is treated as dead rather than running.
#:
#: Comfortably longer than any real Service Layer call (5-7 seconds observed,
#: with the client's own timeout below this) and short enough that an approver
#: is not left waiting. Inside the window the answer is "wait"; outside it the
#: answer is "ask SAP", which is safe at any age.
POSTING_LEASE = timedelta(minutes=5)

#: What `attempt_state` can return.
NOT_OWED = 'not_owed'
IN_FLIGHT = 'in_flight'
NEVER_STARTED = 'never_started'
UNCERTAIN = 'uncertain'


def _logs_for(document):
    ct = ContentType.objects.get_for_model(document.__class__)
    return SapCallLog.objects.filter(content_type=ct, object_id=document.pk)


def attempt_state(document, *, now=None):
    """Which of the four situations this document is in. See the module doc."""
    statuses = document.__class__.Status

    # Already in SAP, or never owed a post at all.
    if getattr(document, 'sap_doc_entry', None):
        return NOT_OWED
    # SAP_UNKNOWN is owed a settlement too, and it is ALWAYS uncertain: SAP
    # never answered, so the document may or may not be there. It used to be a
    # dead end — refused by every guard, resolvable only by a reconciliation
    # sweep that scans POSTED documents and so never looked at it.
    if document.status == statuses.SAP_UNKNOWN:
        return UNCERTAIN

    if document.status != statuses.POSTING_TO_SAP:
        return NOT_OWED

    now = now or timezone.now()
    latest = _logs_for(document).order_by('-created_at', '-id').first()
    if latest is None:
        # NO ATTEMPT ROW YET. Two different things, told apart by age.
        #
        # A live request is briefly here: the intent commits, then
        # `_log_start` writes its row a few statements later. That gap is
        # milliseconds, but a second approval landing inside it would post a
        # SECOND payment — which is exactly what the old `status ==
        # POSTING_TO_SAP` guard prevented, bluntly but correctly.
        #
        # So within the lease this counts as IN FLIGHT: a double click, or two
        # approvers clicking together, is refused. Past the lease, an absent
        # row proves no request was ever made — a log row is always written
        # before the HTTP call leaves — and the document is safe to post
        # without consulting SAP.
        if now - document.updated_at < POSTING_LEASE:
            return IN_FLIGHT
        return NEVER_STARTED

    if latest.status == SapCallLog.Status.STARTED:
        return (IN_FLIGHT if now - latest.created_at < POSTING_LEASE
                else UNCERTAIN)

    # A finished log (SUCCESS or FAILED) under a POSTING_TO_SAP document means
    # the outcome was recorded but the document write did not land — a crash
    # between the two. SAP's state is not knowable from here.
    return UNCERTAIN


def posting_is_in_flight(document):
    """True only while a SAP call is genuinely running.

    THIS IS WHAT THE GUARDS MUST ASK, not `status == POSTING_TO_SAP`. The
    status alone cannot tell a live call from a dead one, and reading it as
    "live" is what stranded a receipt for 43 hours.
    """
    return attempt_state(document) == IN_FLIGHT


def find_existing_posting(document):
    """Ask SAP whether it already holds this document. May raise.

    Raising on an unreachable SAP is the point: the caller must not read a
    failed query as "SAP does not have it" and post a second payment.
    """
    from .hana_queries import fetch_payment_by_reference

    return fetch_payment_by_reference(
        company=document.company, reference=oms_reference(document))


def _adopt(document, row, user=None):
    """SAP already has this document — record that rather than post again.

    The same end state a successful post produces: DocEntry, DocNum, POSTED,
    and the approval completed in the SAME transaction, so a document can never
    be POSTED with its flow still waiting.
    """
    from . import workflow_flow
    from .services import log_status

    statuses = document.__class__.Status
    with transaction.atomic():
        fresh = (document.__class__.objects
                 .select_for_update().get(pk=document.pk))
        if fresh.sap_doc_entry:
            return fresh
        previous = fresh.status
        fresh.sap_doc_entry = row['doc_entry']
        fresh.sap_doc_num = row['doc_num']
        if row.get('trans_id') is not None:
            fresh.sap_trans_id = row['trans_id']
        fresh.status = statuses.POSTED
        fresh.sap_posted_at = timezone.now()
        fresh.sap_response = (
            f'Recovered: SAP already held this document as DocEntry '
            f'{row["doc_entry"]} (DocNum {row["doc_num"]}). The posting '
            f'succeeded; the confirmation was lost before OMS could record '
            f'it, so it was matched back by its own reference.')
        fresh.save(update_fields=['sap_doc_entry', 'sap_doc_num',
                                  'sap_trans_id', 'status', 'sap_posted_at',
                                  'sap_response', 'updated_at'])
        log_status(fresh, from_status=previous, to_status=fresh.status,
                   user=user, actor_kind='SYSTEM',
                   reason=fresh.sap_response)
        workflow_flow.complete_after_sap(fresh)
    logger.warning('Adopted existing SAP posting for %s: DocEntry %s',
                   oms_reference(fresh), row['doc_entry'])
    return fresh


def _refuse(document, message, user=None):
    """Park the document at its final stage with an explanation.

    PENDING_ERROR, not SAP_UNKNOWN: the approver can see it, read why, and
    retry when the cause is fixed. SAP_UNKNOWN would lock it again, which is
    the state this whole module exists to abolish.
    """
    from .services import log_status

    statuses = document.__class__.Status
    with transaction.atomic():
        fresh = (document.__class__.objects
                 .select_for_update().get(pk=document.pk))
        if fresh.sap_doc_entry:
            return fresh
        previous = fresh.status
        fresh.status = statuses.PENDING_ERROR
        fresh.sap_response = message[:4000]
        fresh.save(update_fields=['status', 'sap_response', 'updated_at'])
        log_status(fresh, from_status=previous, to_status=fresh.status,
                   user=user, actor_kind='SYSTEM', reason=message[:500])
    return fresh


def settle(document, *, user=None, claimed=False):
    """Bring a document owing a SAP post to a decided state. Idempotent.

    `claimed` is for the caller that JUST created the intent — the approve
    request itself. It has already passed `_guard_postable`, which refuses when
    a call is genuinely in flight, so it holds the claim on this posting and
    must not then be told to wait for itself. Every other caller (a retry from
    a stranded state, the recovery sweep) leaves it False and yields to a live
    call.

    It does NOT skip the uncertainty check: if a previous attempt left a stale
    log row, even the approving caller asks SAP before posting.

    Safe to call as often as anything likes: from the approve request, from an
    approver's retry, from the recovery sweep, from all three at once. Every
    path through it either posts exactly once, adopts a posting SAP already
    made, or leaves the document retryable with the reason on it.
    """
    from .services import post_deposit_to_sap, post_receipt_to_sap

    # RE-READ FIRST. Callers reach this holding whatever they had — often a
    # cached relation off the flow, whose `status` still says what it said
    # BEFORE the approval wrote POSTING_TO_SAP. Classifying a stale copy makes
    # `attempt_state` answer about a document that no longer exists, and the
    # post is then silently skipped.
    document = document.__class__.objects.get(pk=document.pk)

    state = attempt_state(document)

    if state == NOT_OWED:
        return document

    if state == IN_FLIGHT and not claimed:
        # Somebody else's call is running. Touching it now is how duplicates
        # happen; it will reach its own outcome.
        logger.info('%s: a SAP call is already in flight, leaving it',
                    oms_reference(document))
        return document

    if state == UNCERTAIN:
        # The only branch that must consult SAP. A blind retry here is exactly
        # the duplicate payment every guard in this module exists to prevent.
        existing = find_existing_posting(document)   # raises if unreachable

        if len(existing) > 1:
            return _refuse(
                document,
                'SAP holds MORE THAN ONE Incoming Payment for this document '
                '(DocEntry ' + ', '.join(str(r['doc_entry']) for r in existing)
                + '). That is a duplicate and OMS will not add to it. A '
                'person must cancel the extra one in SAP before this can be '
                'settled.',
                user=user)

        if existing:
            row = existing[0]
            if row['canceled']:
                return _refuse(
                    document,
                    f'SAP holds this document as DocEntry {row["doc_entry"]} '
                    f'but it has been CANCELLED there. OMS will not post it '
                    f'again automatically — re-posting a cancelled payment is '
                    f'a decision for a person. Raise a new document if the '
                    f'money still needs recording.',
                    user=user)
            return _adopt(document, row, user=user)

        # SAP was asked and does not have it. Posting now cannot duplicate.
        logger.warning('%s: interrupted mid-post, SAP does not hold it — '
                       'posting', oms_reference(document))

    # NEVER_STARTED, or UNCERTAIN proven absent from SAP.
    if isinstance(document, PaymentReceipt):
        return post_receipt_to_sap(document, user=user)
    if isinstance(document, BankDeposit):
        return post_deposit_to_sap(document, user=user)
    raise TypeError(f'Cannot settle {document.__class__.__name__}')
