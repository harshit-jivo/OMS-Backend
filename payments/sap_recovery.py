"""Recover documents whose approval landed but whose SAP post never ran.

THE FAILURE THIS EXISTS FOR. The final approval commits and defers the SAP call
to `transaction.on_commit`, so a 5-7 second call does not hold the approval's
row locks. But that callback is NOT durable: if the worker is restarted between
the commit and the callback — a deploy, a recycle, an OOM kill — it is simply
lost. The document is left owing a SAP post that nothing will ever make.

HOW THE STRANDED STATE IS RECOGNISED NOW. It changed with the final-stage
lifecycle. The flow no longer completes on approval, so "flow APPROVED but
document not POSTED" — the old signature — can no longer occur. What marks a
document as owing a post is its own status: the final approval sets
POSTING_TO_SAP *before* it commits, precisely so the intent survives the loss
of the callback. A document is therefore stranded when it is POSTING_TO_SAP and
no SAP call was ever logged for it.

WHY THE AGE THRESHOLD MATTERS. POSTING_TO_SAP is now set at approval time,
before the call is made, so a document is legitimately in that state with no
SapCallLog row for as long as the request takes to reach SAP. Sweeping it in
that window would post it twice. `MIN_STRANDED_AGE` keeps the sweep away from
anything recent; only documents that have sat unposted far longer than any call
could take are considered lost.

Nothing else recovers it. `post_receipt_to_sap` has exactly one caller (that
callback), and `reconcile_sap_cancellations` only scans documents already
POSTED. So the document is stranded permanently and invisible to every sweep —
observed on RCP-OIL-20260905-000002, which sat approved with zero SAP call logs
while four other receipts approved minutes either side posted normally.

WHAT THIS IS NOT. It is not a retry for SAP failures. A document SAP rejected
is PENDING_ERROR and stays with its final approver to retry; one SAP never
answered is SAP_UNKNOWN and must NOT be reposted, because it may already exist
there. Both are excluded below. This targets only the case where SAP was never
called at all, which is provable: no sap_doc_entry AND no SapCallLog row.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from .models import BankDeposit, PaymentReceipt, SapCallLog

logger = logging.getLogger(__name__)

#: Default minutes a document must have been owing a SAP post before the sweep
#: treats it as lost. Overridden by `PAYMENTS_SAP_STRANDED_MIN_AGE_MINUTES`.
DEFAULT_STRANDED_MIN_AGE_MINUTES = 30

#: How long a document must have been owing a SAP post before the sweep will
#: treat it as lost.
#:
#: HOW SMALL CAN THIS SAFELY BE? The guard protects one window: a call that is
#: genuinely in flight but has not yet written its SapCallLog row, which the
#: sweep would otherwise post a second time. That window is bounded by where
#: the log row is written, NOT by how long SAP takes to answer —
#: `sap_poster.post_document` calls `_log_start()` OUTSIDE any atomic block, so
#: the row autocommits and becomes visible BEFORE the HTTP request leaves. The
#: gap between committing POSTING_TO_SAP and committing that row is two
#: statements: milliseconds. (And if `_log_start` itself raises, it does so
#: before the request is made, so no call happens at all.)
#:
#: 30 minutes is therefore a very large margin, chosen because the cost of
#: waiting is a delayed payment while the cost of being too eager is a
#: DUPLICATE one. It is left as the default for exactly that reason.
#:
#: It is configurable because the margin is worth trading down during testing,
#: where a dev-server autoreload strands a document on almost every code change
#: and a 30-minute wait makes the recovery feel broken. Anything at or above a
#: minute stays far outside the real risk window.
MIN_STRANDED_AGE = timedelta(
    minutes=getattr(settings, 'PAYMENTS_SAP_STRANDED_MIN_AGE_MINUTES',
                    DEFAULT_STRANDED_MIN_AGE_MINUTES))


def find_stranded(model, *, company='', limit=None, min_age=None):
    """Documents whose final approval landed but which never reached SAP.

    Every condition here is a guard against reposting something that might
    already exist in SAP:

      * status is POSTING_TO_SAP — the marker the final approval commits before
        the call is made. PENDING_ERROR and SAP_UNKNOWN are handled by their
        own paths and must not be swept; PENDING_APPROVAL means no final
        approval has been given at all.
      * no sap_doc_entry — belt and braces with the status check.
      * NO SapCallLog row exists — the decisive one. A log row is written
        before the request leaves, so its absence proves no call was made.
      * it has been in that state longer than `min_age` — see the module
        docstring. Without this the sweep races every posting in flight.

    The flow is NOT required to be APPROVED. Under the current lifecycle it is
    still PENDING at the final stage while the post is owed, and only becomes
    APPROVED once SAP has accepted the document.
    """
    ct = ContentType.objects.get_for_model(model)
    cutoff = timezone.now() - (min_age if min_age is not None
                               else MIN_STRANDED_AGE)

    owing = (model.objects
             .filter(status=model.Status.POSTING_TO_SAP,
                     sap_doc_entry__isnull=True,
                     updated_at__lt=cutoff))
    if company:
        owing = owing.filter(company=company.upper())

    owing_ids = set(owing.values_list('pk', flat=True))
    if not owing_ids:
        return []

    # Documents that have ever had a SAP call attempted, by id.
    attempted_ids = set(
        SapCallLog.objects
        .filter(content_type=ct, object_id__in=owing_ids)
        .values_list('object_id', flat=True)
    )

    qs = (model.objects
          .filter(pk__in=owing_ids - attempted_ids)
          .order_by('created_at'))
    if limit:
        qs = qs[:limit]
    return list(qs)

def _post(document):
    """Post one stranded document through the NORMAL path.

    Deliberately calls the same `post_*_to_sap` the approval hook calls, so a
    recovered document goes through the identical duplicate guard, row lock,
    payload build, call log and history rows as any other post. Nothing about
    the resulting SAP document says it was recovered, because nothing about it
    IS different — only the trigger was.
    """
    from .services import post_deposit_to_sap, post_receipt_to_sap

    if isinstance(document, PaymentReceipt):
        return post_receipt_to_sap(document)
    return post_deposit_to_sap(document)


def recover_stranded_posts(*, company='', limit=None, dry_run=False):
    """Find and re-post approved documents whose SAP call never happened.

    Safe to run repeatedly and safe to run from cron: a document that posts
    successfully leaves POSTING_TO_SAP, so it cannot be picked up twice, and
    `post_document`'s own duplicate guard is a second line of defence.
    """
    summary = {
        'receipts_found': 0, 'receipts_posted': 0,
        'deposits_found': 0, 'deposits_posted': 0,
        'errors': 0, 'details': [],
    }

    for model, found_key, posted_key in (
        (PaymentReceipt, 'receipts_found', 'receipts_posted'),
        (BankDeposit, 'deposits_found', 'deposits_posted'),
    ):
        for document in find_stranded(model, company=company, limit=limit):
            summary[found_key] += 1
            number = getattr(document, 'receipt_no', None) or document.deposit_no
            if dry_run:
                summary['details'].append(f'{number}: would post')
                continue

            try:
                fresh = _post(document)
            except Exception as exc:                      # noqa: BLE001
                # One document failing must not stop the sweep — the next may
                # be fine, and a stuck payment is exactly what this fixes.
                summary['errors'] += 1
                summary['details'].append(f'{number}: ERROR {exc}')
                logger.exception('Could not recover stranded post for %s', number)
                continue

            if fresh.status == model.Status.POSTED:
                summary[posted_key] += 1
                summary['details'].append(
                    f'{number}: posted (DocNum {fresh.sap_doc_num})')
            else:
                # SAP answered, and its answer was not success. The document is
                # now correctly in PENDING_ERROR or SAP_UNKNOWN with its own
                # history — the normal flow took over, which is the point.
                summary['details'].append(
                    f'{number}: {fresh.get_status_display()} — '
                    f'{(fresh.sap_response or "")[:120]}')

    return summary
