"""Recover documents whose approval landed but whose SAP post never ran.

THE FAILURE THIS EXISTS FOR. `_on_receipt_approved` records the approval and
then defers the SAP call to `transaction.on_commit`, so a SAP failure cannot
roll the approval back. That is right, but it leaves a window: once the
approval transaction commits, the callback is the ONLY thing that will ever
post the document, and it is not durable. If the worker is restarted between
the commit and the callback — a deploy, a recycle, an OOM kill — the callback
is simply lost. The approval says APPROVED, the document still says
PENDING_APPROVAL, and no SAP call was ever made.

Nothing else recovers it. `post_receipt_to_sap` has exactly one caller (that
callback), and `reconcile_sap_cancellations` only scans documents already
POSTED. So the document is stranded permanently and invisible to every sweep —
observed on RCP-OIL-20260905-000002, which sat approved with zero SAP call logs
while four other receipts approved minutes either side posted normally.

WHAT THIS IS NOT. It is not a retry for SAP failures. A document SAP rejected
is PENDING_ERROR and is deliberately reopened to its approver; one SAP never
answered is SAP_UNKNOWN and must NOT be reposted, because it may already exist
there. Both are excluded below. This targets only the case where SAP was never
called at all, which is provable: no sap_doc_entry AND no SapCallLog row.
"""
import logging

from django.contrib.contenttypes.models import ContentType

from approvals.models import ApprovalRequest

from .models import BankDeposit, PaymentReceipt, SapCallLog

logger = logging.getLogger(__name__)


def find_stranded(model, *, company='', limit=None):
    """Documents whose approval is APPROVED but which never reached SAP.

    Every condition here is a guard against reposting something that might
    already exist in SAP:

      * status is still PENDING_APPROVAL — POSTING_TO_SAP means a call was in
        flight and may have committed; PENDING_ERROR and SAP_UNKNOWN are
        handled by their own flows and must not be swept.
      * no sap_doc_entry — belt and braces with the status check.
      * the approval request is APPROVED — the document is genuinely finished
        with its ladder, not merely parked at a rung.
      * NO SapCallLog row exists — the decisive one. A log row is written
        before the request leaves, so its absence proves no call was made.
    """
    ct = ContentType.objects.get_for_model(model)

    approved_ids = set(
        ApprovalRequest.objects
        .filter(content_type=ct, status=ApprovalRequest.Status.APPROVED)
        .values_list('object_id', flat=True)
    )
    if not approved_ids:
        return []

    # Documents that have ever had a SAP call attempted, by id.
    attempted_ids = set(
        SapCallLog.objects
        .filter(content_type=ct, object_id__in=approved_ids)
        .values_list('object_id', flat=True)
    )

    qs = (model.objects
          .filter(pk__in=approved_ids - attempted_ids,
                  status=model.Status.PENDING_APPROVAL,
                  sap_doc_entry__isnull=True)
          .order_by('created_at'))
    if company:
        qs = qs.filter(company=company.upper())
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
    successfully leaves PENDING_APPROVAL, so it cannot be picked up twice, and
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
