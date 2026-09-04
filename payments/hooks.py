"""Approval outcome -> payment/deposit state.

Registered from PaymentsConfig.ready(). The approval engine never imports
`payments` (that would be circular); it looks these up by model label instead.

Every hook runs INSIDE the approval transaction, which is the point: a receipt
cannot be marked approved without also being queued for SAP, because both
writes commit together or neither does.
"""
import logging

logger = logging.getLogger(__name__)


def _decision_detail(action):
    """(level, level_name, remarks) from an ApprovalAction, safely.

    Coerced to their real column types rather than passed through raw. The
    rung is an integer and the two labels are text, so anything that is not
    already of that shape — a missing action, or a test double standing in for
    one — becomes a null or an empty string instead of reaching the database
    as an object it cannot store.
    """
    if action is None:
        return None, '', ''

    level = getattr(action, 'level', None)
    level = level if isinstance(level, int) else None

    name = getattr(action, 'level_name', '')
    name = name if isinstance(name, str) else ''

    remarks = getattr(action, 'remarks', '')
    remarks = remarks if isinstance(remarks, str) else ''

    return level, name, remarks


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

    # Record the approval on the payment's own timeline.
    #
    # It was missing entirely: approval decisions live in `approval_action`,
    # so the payment history jumped from VERIFIED straight to SAP_POSTED with
    # nothing to say who approved it or at which rung. The decision log stays
    # the authority — this is the business-timeline projection of it, carrying
    # the rung so a multi-level ladder reads level by level.
    from .models import PaymentStatusHistory
    from .services import log_status

    last = approval_request.actions.order_by('-sequence').first()
    level, level_name, remarks = _decision_detail(last)
    log_status(receipt, from_status=receipt.status, to_status=receipt.status,
               user=approver, actor_kind='APPROVAL_ENGINE',
               action=PaymentStatusHistory.Action.APPROVED,
               level=level, level_label=level_name,
               reason=remarks or 'Payment approved.')

    transaction.on_commit(lambda: post_receipt_to_sap(receipt, user=approver))

    # Notify the submitter that their receipt was approved. Runs inside the
    # approval transaction: the Notification records commit with the approval,
    # and external push delivery is deferred to on_commit (so a rollback sends
    # nothing). Delivery failures are isolated by the framework and never break
    # the approval.
    from .notification_events import publish_receipt_decision
    publish_receipt_decision(
        receipt, approval_request.submitted_by, approved=True, actor=approver,
    )


def _on_receipt_rejected(approval_request):
    from .models import PaymentReceipt, PaymentStatusHistory
    from .services import log_status

    receipt = approval_request.document
    if receipt is None:
        return
    previous = receipt.status
    receipt.status = PaymentReceipt.Status.REJECTED
    receipt.save(update_fields=['status', 'updated_at'])
    last = approval_request.actions.order_by('-sequence').first()
    level, level_name, remarks = _decision_detail(last)
    reason = remarks or 'Rejected.'
    # REJECTED, with the rung it was refused at. Without the explicit action
    # this fell back to STATUS_CHANGED and the timeline read as an anonymous
    # transition rather than an approver's decision.
    log_status(receipt, from_status=previous, to_status=receipt.status,
               actor_kind='APPROVAL_ENGINE',
               action=PaymentStatusHistory.Action.REJECTED,
               level=level, level_label=level_name,
               user=getattr(approval_request, '_acting_user', None),
               reason=reason)

    # Notify the submitter their receipt was rejected (same transaction safety
    # as the approved path above).
    from .notification_events import publish_receipt_decision
    publish_receipt_decision(
        receipt, approval_request.submitted_by, approved=False,
        actor=getattr(approval_request, '_acting_user', None), reason=reason,
    )


def _on_receipt_submitted(approval_request):
    """Receipt entered the approval workflow — notify the CURRENT (level-1)
    approvers. Fires from the approval engine's submit, inside its transaction,
    so the notification commits with the submission and rolls back with it.
    """
    receipt = approval_request.document
    if receipt is None:
        return
    from .notification_events import publish_receipt_submitted
    publish_receipt_submitted(receipt, approval_request.submitted_by,
                              request=approval_request)


def _on_receipt_level_advanced(approval_request):
    """An intermediate level cleared — notify the NEW current level's approvers.

    The engine has already incremented current_level before firing this, so the
    publisher resolves exactly the rung that now owns the receipt (Orders-parity:
    only the current stage is notified, never every level at once).
    """
    receipt = approval_request.document
    if receipt is None:
        return
    from .notification_events import publish_receipt_next_level
    publish_receipt_next_level(
        receipt, approval_request,
        actor=getattr(approval_request, '_acting_user', None),
    )


def _on_receipt_cancelled(approval_request):
    from .models import PaymentReceipt, PaymentStatusHistory
    from .services import log_status

    receipt = approval_request.document
    if receipt is None:
        return
    previous = receipt.status
    receipt.status = PaymentReceipt.Status.CANCELLED
    receipt.save(update_fields=['status', 'updated_at'])
    log_status(receipt, from_status=previous, to_status=receipt.status,
               actor_kind='APPROVAL_ENGINE',
               action=PaymentStatusHistory.Action.CANCELLED,
               reason='Cancelled by submitter.')


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

    # Notify the submitter their deposit was approved. Same transaction safety as
    # the receipt hooks: the Notification records commit with the approval, and
    # external push delivery is deferred to on_commit (rollback → nothing sent).
    from .notification_events import publish_deposit_decision
    publish_deposit_decision(
        deposit, approval_request.submitted_by, approved=True, actor=approver,
    )


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
    reason = (last.remarks if last else '') or 'Rejected.'
    log_status(deposit, from_status=previous, to_status=deposit.status,
               actor_kind='APPROVAL_ENGINE',
               reason=reason)

    # Notify the submitter their deposit was rejected (same transaction safety).
    from .notification_events import publish_deposit_decision
    publish_deposit_decision(
        deposit, approval_request.submitted_by, approved=False,
        actor=getattr(approval_request, '_acting_user', None), reason=reason,
    )


def _on_deposit_submitted(approval_request):
    """Deposit entered the approval workflow — notify the current (level-1)
    approvers (same transaction safety as the receipt submit hook)."""
    deposit = approval_request.document
    if deposit is None:
        return
    from .notification_events import publish_deposit_submitted
    publish_deposit_submitted(deposit, approval_request.submitted_by,
                              request=approval_request)


def _on_deposit_level_advanced(approval_request):
    """An intermediate deposit level cleared — notify the NEW current level."""
    deposit = approval_request.document
    if deposit is None:
        return
    from .notification_events import publish_deposit_next_level
    publish_deposit_next_level(
        deposit, approval_request,
        actor=getattr(approval_request, '_acting_user', None),
    )


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
        on_submitted=_on_receipt_submitted,
        on_approved=_on_receipt_approved,
        on_rejected=_on_receipt_rejected,
        on_cancelled=_on_receipt_cancelled,
        on_level_advanced=_on_receipt_level_advanced,
    )
    register_hooks(
        'payments.bankdeposit',
        on_submitted=_on_deposit_submitted,
        on_approved=_on_deposit_approved,
        on_rejected=_on_deposit_rejected,
        on_cancelled=_on_deposit_cancelled,
        on_level_advanced=_on_deposit_level_advanced,
    )
