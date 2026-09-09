"""Detect payments and deposits cancelled in SAP after a successful post.

A document can post to SAP successfully and be cancelled there afterwards by a
person working in the SAP client. SAP keeps the original ORCT row, sets
`Canceled = 'Y'` and writes a reversing journal entry — it never deletes.

OMS has no way to learn of this from the posting response, which was a success
and remains historically true. This module closes that gap by READING ORCT for
every posted document and recording the divergence.

Design rules, each of which matters:

* A cancellation is NOT a posting failure. It gets its own status,
  CANCELLED_IN_SAP, and never becomes PENDING_ERROR — the post did succeed.
* `sap_response` is never overwritten. The posting response is a historical
  record; the cancellation is written to `sap_cancellation_response` instead.
* SAP identifiers are never cleared. DocEntry / DocNum / TransId still identify
  the original payment that was cancelled, and are needed to trace it.
* Only an explicit `Canceled = 'Y'` read back from ORCT changes OMS state. A
  SAP HTTP error, a timeout, or an unreadable row changes nothing.
* Nothing is auto-resubmitted, reposted or reopened. A cancellation is a
  financial event a human must handle.
* Re-running is idempotent: a document already CANCELLED_IN_SAP is skipped, so
  no duplicate history rows accumulate.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from . import hana_queries
from .models import BankDeposit, PaymentReceipt, PaymentStatusHistory
from .services import log_status

logger = logging.getLogger(__name__)

# Composed for the person holding the document. Deliberately states that the
# posting succeeded, so this is not mistaken for a posting failure.
CANCELLATION_MESSAGE = (
    'This payment was posted successfully but was later cancelled in SAP.'
)


def _mark_cancelled(document, *, sap_row):
    """Record one detected cancellation. Returns True when state changed."""
    model = document.__class__
    previous = document.status

    cancel_date = sap_row.get('cancel_date')
    if cancel_date is not None and timezone.is_naive(cancel_date):
        cancel_date = timezone.make_aware(cancel_date)

    detail = CANCELLATION_MESSAGE
    if document.sap_doc_num:
        detail = f'{detail} SAP document {document.sap_doc_num}.'

    with transaction.atomic():
        # Re-read under a lock: another run (or a posting) may have moved this
        # document since the SAP read, and only one may write the transition.
        fresh = model.objects.select_for_update().get(pk=document.pk)
        if fresh.status == model.Status.CANCELLED_IN_SAP:
            return False
        if fresh.status != model.Status.POSTED:
            # Only a POSTED document can be cancelled in SAP. Anything else
            # moved on under us and must not be overwritten.
            return False

        fresh.status = model.Status.CANCELLED_IN_SAP
        fresh.sap_cancelled_at = cancel_date or timezone.now()
        fresh.sap_cancellation_response = detail
        fresh.sap_reconciled_at = timezone.now()
        # sap_response, sap_doc_entry, sap_doc_num and sap_trans_id are
        # deliberately NOT touched.
        fresh.save(update_fields=[
            'status', 'sap_cancelled_at', 'sap_cancellation_response',
            'sap_reconciled_at', 'updated_at',
        ])

        log_status(
            fresh,
            from_status=previous,
            to_status=fresh.status,
            actor_kind='SYSTEM',
            action=PaymentStatusHistory.Action.SAP_CANCELLED,
            reason=detail,
            sap_doc_entry=fresh.sap_doc_entry,
            sap_doc_num=fresh.sap_doc_num,
        )

    logger.warning(
        '%s: cancelled in SAP (DocEntry %s, DocNum %s, TransId %s)',
        getattr(fresh, 'receipt_no', None) or getattr(fresh, 'deposit_no', ''),
        fresh.sap_doc_entry, fresh.sap_doc_num, fresh.sap_trans_id,
    )
    return True


def _touch_reconciled(document):
    """Record that SAP confirmed this document is still live."""
    document.sap_reconciled_at = timezone.now()
    document.save(update_fields=['sap_reconciled_at', 'updated_at'])


def reconcile_document(document):
    """Check ONE posted document against SAP. Returns True when cancelled.

    Uses the document's own server-side `sap_doc_entry` and `company` — never
    anything supplied by a caller — so this cannot be pointed at a different
    SAP database or document.
    """
    model = document.__class__
    if document.status != model.Status.POSTED:
        return False
    if not document.sap_doc_entry:
        return False

    sap_row = hana_queries.fetch_payment_cancellation(
        company=document.company, doc_entry=document.sap_doc_entry)

    if sap_row is None:
        # Unreadable: SAP down, network, or the row is genuinely absent. Never
        # a reason to downgrade a posted document.
        return False

    if sap_row.get('canceled') != 'Y':
        _touch_reconciled(document)
        return False

    return _mark_cancelled(document, sap_row=sap_row)


def reconcile_sap_cancellations(*, limit=None, company=None):
    """Check every POSTED receipt and deposit against SAP ORCT.

    Callable from a management command, a scheduled job or a future scheduler
    — it takes no request and touches no client input.

    Returns a summary dict: how many were checked and how many were newly
    found cancelled.
    """
    summary = {
        'receipts_checked': 0, 'receipts_cancelled': 0,
        'deposits_checked': 0, 'deposits_cancelled': 0,
        'errors': 0,
    }

    for model, checked_key, cancelled_key in (
        (PaymentReceipt, 'receipts_checked', 'receipts_cancelled'),
        (BankDeposit, 'deposits_checked', 'deposits_cancelled'),
    ):
        qs = (model.objects
              .filter(status=model.Status.POSTED)
              .exclude(sap_doc_entry__isnull=True)
              .order_by('id'))
        if company:
            qs = qs.filter(company=company)
        if limit:
            qs = qs[:limit]

        for document in qs:
            summary[checked_key] += 1
            try:
                if reconcile_document(document):
                    summary[cancelled_key] += 1
            except Exception:
                # One bad document must not stop the sweep.
                summary['errors'] += 1
                logger.exception('Reconciliation failed for %s %s',
                                 model.__name__, document.pk)

    logger.info('SAP cancellation reconciliation: %s', summary)
    return summary
