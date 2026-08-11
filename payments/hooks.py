"""Approval outcome -> payment/deposit state.

Registered from PaymentsConfig.ready(). The approval engine never imports
`payments` (that would be circular); it looks these up by model label instead.

Every hook runs INSIDE the approval transaction, which is the point: a receipt
cannot be marked approved without also being queued for SAP, because both
writes commit together or neither does.
"""
import logging

logger = logging.getLogger(__name__)


def _on_receipt_approved(approval_request):
    """Final approval landed — post to SAP.

    Deferred to transaction.on_commit rather than run inline, for two reasons:
    a 5-7 second SAP call would hold the approval's row locks open for its whole
    duration, and a SAP failure must NOT roll the approval back. The approval
    really happened; a rejected document just lands in PENDING_ERROR.
    """
    from django.db import transaction

    from .services import post_receipt_to_sap

    receipt = approval_request.document
    if receipt is None:
        logger.error('Approval %s approved but its receipt is missing.',
                     approval_request.pk)
        return

    approver = getattr(approval_request, '_acting_user', None)
    transaction.on_commit(lambda: post_receipt_to_sap(receipt, user=approver))


def _on_receipt_rejected(approval_request):
    from .models import PaymentReceipt
    from .services import log_status

    receipt = approval_request.document
    if receipt is None:
        return
    previous = receipt.status
    receipt.status = PaymentReceipt.Status.REJECTED
    receipt.save(update_fields=['status', 'updated_at'])
    last = approval_request.actions.order_by('-sequence').first()
    log_status(receipt, from_status=previous, to_status=receipt.status,
               actor_kind='APPROVAL_ENGINE',
               reason=(last.remarks if last else '') or 'Rejected.')


def _on_receipt_cancelled(approval_request):
    from .models import PaymentReceipt
    from .services import log_status

    receipt = approval_request.document
    if receipt is None:
        return
    previous = receipt.status
    receipt.status = PaymentReceipt.Status.CANCELLED
    receipt.save(update_fields=['status', 'updated_at'])
    log_status(receipt, from_status=previous, to_status=receipt.status,
               actor_kind='APPROVAL_ENGINE', reason='Cancelled by submitter.')


def _on_deposit_approved(approval_request):
    """Final approval landed — post to SAP. See _on_receipt_approved."""
    from django.db import transaction

    from .services import post_deposit_to_sap

    deposit = approval_request.document
    if deposit is None:
        logger.error('Approval %s approved but its deposit is missing.',
                     approval_request.pk)
        return

    approver = getattr(approval_request, '_acting_user', None)
    transaction.on_commit(lambda: post_deposit_to_sap(deposit, user=approver))


def _on_deposit_rejected(approval_request):
    from .models import BankDeposit
    from .services import log_status

    deposit = approval_request.document
    if deposit is None:
        return
    previous = deposit.status
    deposit.status = BankDeposit.Status.REJECTED
    deposit.save(update_fields=['status', 'updated_at'])
    last = approval_request.actions.order_by('-sequence').first()
    log_status(deposit, from_status=previous, to_status=deposit.status,
               actor_kind='APPROVAL_ENGINE',
               reason=(last.remarks if last else '') or 'Rejected.')


def _on_deposit_cancelled(approval_request):
    from .models import BankDeposit
    from .services import log_status

    deposit = approval_request.document
    if deposit is None:
        return
    previous = deposit.status
    deposit.status = BankDeposit.Status.CANCELLED
    deposit.save(update_fields=['status', 'updated_at'])
    log_status(deposit, from_status=previous, to_status=deposit.status,
               actor_kind='APPROVAL_ENGINE', reason='Cancelled by submitter.')


def register():
    from approvals.services import register_hooks

    register_hooks(
        'payments.paymentreceipt',
        on_approved=_on_receipt_approved,
        on_rejected=_on_receipt_rejected,
        on_cancelled=_on_receipt_cancelled,
    )
    register_hooks(
        'payments.bankdeposit',
        on_approved=_on_deposit_approved,
        on_rejected=_on_deposit_rejected,
        on_cancelled=_on_deposit_cancelled,
    )
