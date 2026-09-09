"""Payments' notification events + publishing helper (Phase 3.5).

Payments is the FIRST consumer of the reusable notification framework. This
module owns the PAYMENTS-SPECIFIC parts:

* the event names,
* who receives each event (recipient resolution),
* the business wording (title/message),

and delegates ALL persistence/delivery to ``notifications.notify``. It does not
reimplement the notification framework.

Dependency direction: payments -> notifications (never the reverse). The
framework knows nothing about payments.

Company note: ``PaymentReceipt.company`` is a SAP *category* string
(CATEGORY_CHOICES), not a ``users.Company`` FK, so it cannot be passed as the
framework's ``company`` (a ``users.Company``). Company is therefore derived
per-recipient inside ``notify()`` from the recipient's own ``users.Company``
(server-side) — a receipt raised by a user in company A notifies that user in
company A, never a company-B user.
"""

import logging

logger = logging.getLogger("payments")

# Event names OWNED BY PAYMENTS. The framework validates the name format only.
#
# Lifecycle split (see docs/NOTIFICATION_INTEGRATION.md):
#   *_SUBMITTED -> the eligible APPROVERS (a decision is now required of them)
#   *_APPROVED  -> the SUBMITTER (the outcome of their document)
#   *_REJECTED  -> the SUBMITTER
# "Submitted for approval" is deliberately NOT "created": a draft sitting in the
# database notifies no one; only entering the approval workflow does.
PAYMENT_SUBMITTED = "PAYMENT_SUBMITTED"
PAYMENT_APPROVED = "PAYMENT_APPROVED"
PAYMENT_REJECTED = "PAYMENT_REJECTED"
# A receipt now waits for a HANDOVER CHECK before it reaches an approver, so
# the first hand-off in its life is creator -> verifier. Without this the
# people who have to count the cash learned about it only by opening the queue.
PAYMENT_VERIFICATION_REQUIRED = "PAYMENT_VERIFICATION_REQUIRED"
# The end of the journey. Sent to the creator AND the verifier — they are the
# two people who put their name to the money, and neither otherwise learns
# that it finally reached SAP. Success only: a FAILURE is the approver's to
# retry (see `_on_receipt_approved`), and telling the creator about a posting
# problem they cannot act on is noise.
PAYMENT_POSTED = "PAYMENT_POSTED"

# Bank Deposits live inside the payments app (payments.BankDeposit), so their
# event names are owned here too. Same rule as above: the framework only
# validates the NAME FORMAT — it never learns what a "deposit" is.
DEPOSIT_SUBMITTED = "DEPOSIT_SUBMITTED"
DEPOSIT_APPROVED = "DEPOSIT_APPROVED"
DEPOSIT_REJECTED = "DEPOSIT_REJECTED"


def _display_name(user):
    """Best human name for a user, matching orders._display_user_name style."""
    if user is None:
        return "someone"
    return (getattr(user, "name", "") or getattr(user, "username", "")
            or "someone")


def _open_request_for(document):
    """The document's single OPEN (PENDING) approval request, or None.

    Filters by the exact (content_type, object_id) of THIS document, so a
    recipient is never resolved from some other document's workflow.
    """
    from approvals.models import ApprovalRequest

    return (ApprovalRequest.objects
            .filter(status=ApprovalRequest.Status.PENDING)
            .filter(content_type__app_label=document._meta.app_label,
                    content_type__model=document._meta.model_name,
                    object_id=document.pk)
            .select_related('workflow')
            .order_by('-created_at')
            .first())


def _current_level_recipients(request, submitter):
    """Approvers of ``request``'s CURRENT level, minus the submitter, de-duped.

    This is the Orders-parity rule: only the stage that now owns the document is
    notified — never every level at once. Recipient resolution stays in
    approvals (``current_level_approvers``, the single source of truth); this
    helper only removes the submitter and collapses duplicates. Returns a list of
    distinct ``User`` instances (possibly empty).
    """
    from approvals.services import current_level_approvers

    if request is None:
        return []
    submitter_id = getattr(submitter, "pk", None)
    seen = set()
    recipients = []
    for approver in current_level_approvers(request):
        if approver.pk == submitter_id:
            continue          # the submitter never approves their own document
        if approver.pk in seen:
            continue          # de-dupe (same person named on the rung twice)
        seen.add(approver.pk)
        recipients.append(approver)
    return recipients


def _notify_current_approvers(document, submitter, *, event_type, title, noun,
                              request=None):
    """Shared body: notify the current level's approvers that a decision is due.

    Used by BOTH the submission path (current level = level 1) and the
    level-advanced path (current level = the new rung). The message mirrors the
    Orders wording ("… from {creator} needs your approval.").
    """
    request = request if request is not None else _open_request_for(document)
    recipients = _current_level_recipients(request, submitter)
    if not recipients:
        logger.info(
            "payments notification skipped: %s %s has no current-level approver",
            noun, getattr(document, "pk", None),
        )
        return []

    from notifications.services import notify

    number = getattr(document, "receipt_no", None) or getattr(
        document, "deposit_no", "")
    creator = _display_name(submitter)
    return notify(
        event_type=event_type,
        title=title,
        message=f"{noun} {number} from {creator} needs your approval.",
        recipients=recipients,
        entity=document,
        actor=submitter,
    )


def publish_receipt_verification_required(receipt, creator):
    """Tell the eligible VERIFIERS that a new receipt needs checking.

    The first hand-off in a receipt's life. Recipients are resolved the same
    way the progress timeline resolves them, so the people notified are exactly
    the people that screen names:

      * holders of an explicit `Payments_Verify` grant;
      * NOT administrators — they hold every key implicitly, so including them
        would notify most of the office about every payment raised;
      * NOT the creator — the server refuses their own verification.

    Resolved at send time from live grants, so adding or removing a verifier
    takes effect on the next receipt with no redeploy.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from core.permissions import ADMIN_ROLE

    from .permissions import PAYMENTS_VERIFY

    User = get_user_model()
    # One query for admins (see PaymentReceiptSerializer.get_eligible_verifiers
    # for why `core.permissions.is_admin` is not called per user here).
    admin_ids = set(
        User.objects.filter(is_active=True)
        .filter(
            Q(role__name__iexact=ADMIN_ROLE)
            | Q(extra_roles__name__iexact=ADMIN_ROLE)
            | Q(is_staff=True)
            | Q(is_superuser=True)
        )
        .values_list('id', flat=True)
    )
    creator_id = getattr(creator, "pk", None)
    recipients = [
        u for u in User.objects.filter(is_active=True).only(
            'id', 'username', 'name', 'extra_pages', 'company')
        if u.pk != creator_id
        and u.pk not in admin_ids
        and PAYMENTS_VERIFY in (u.extra_pages or [])
    ]
    if not recipients:
        logger.info(
            "payments notification skipped: receipt %s has no eligible verifier",
            getattr(receipt, "pk", None),
        )
        return []

    from notifications.services import notify

    return notify(
        event_type=PAYMENT_VERIFICATION_REQUIRED,
        title="Payment verification required",
        message=(
            f"Payment {receipt.receipt_no} from {_display_name(creator)} "
            f"needs to be verified."
        ),
        recipients=recipients,
        entity=receipt,
        actor=creator,
    )


def publish_receipt_posted(receipt):
    """Tell the creator AND the verifier that the receipt reached SAP.

    The two people who put their name to the money. Sent on SUCCESS only — a
    posting failure goes back to the approver holding it, and telling the
    creator about a GL problem they cannot fix is noise.

    Both fields can be null (a legacy receipt, a deleted account), and the two
    can be the same person on a backend where self-verification was possible,
    so recipients are de-duplicated.
    """
    seen = set()
    recipients = []
    for user in (receipt.created_by, receipt.verified_by):
        if user is None or user.pk in seen:
            continue
        seen.add(user.pk)
        recipients.append(user)
    if not recipients:
        return []

    from notifications.services import notify

    return notify(
        event_type=PAYMENT_POSTED,
        title="Payment posted to SAP",
        message=(
            f"Payment {receipt.receipt_no} has been posted to SAP"
            + (f" as document {receipt.sap_doc_num}."
               if receipt.sap_doc_num else ".")
        ),
        recipients=recipients,
        entity=receipt,
        actor=None,
    )


def publish_receipt_submitted(receipt, submitter, request=None):
    """Notify the CURRENT level's approvers that a receipt needs approval.

    Fired from the approval engine's ``submitted`` hook, so the current level is
    level 1. One notification per approver at THAT level only; the submitter is
    excluded; company isolation is inherited from ``notify`` (each approver is
    scoped to their own ``users.Company``). ``request`` may be passed by the hook
    to avoid a re-query; otherwise it is looked up by (content_type, object_id).
    """
    return _notify_current_approvers(
        receipt, submitter,
        event_type=PAYMENT_SUBMITTED, title="Payment approval required",
        noun="Payment", request=request,
    )


def publish_receipt_next_level(receipt, request, actor=None):
    """Notify the NEXT level's approvers after an intermediate approval.

    Fired from the ``level_advanced`` approval hook: the request's current_level
    has already moved to the new rung, so ``_current_level_recipients`` resolves
    exactly that rung. The original submitter (``request.submitted_by``) is
    excluded as always.
    """
    return _notify_current_approvers(
        receipt, getattr(request, "submitted_by", None),
        event_type=PAYMENT_SUBMITTED, title="Payment approval required",
        noun="Payment", request=request,
    )


def publish_deposit_submitted(deposit, submitter, request=None):
    """Notify the CURRENT level's approvers that a deposit needs approval.

    Fired from the approval engine's ``submitted`` hook. Same rules as
    :func:`publish_receipt_submitted`.
    """
    return _notify_current_approvers(
        deposit, submitter,
        event_type=DEPOSIT_SUBMITTED, title="Deposit approval required",
        noun="Deposit", request=request,
    )


def publish_deposit_next_level(deposit, request, actor=None):
    """Notify the NEXT level's approvers after an intermediate deposit approval."""
    return _notify_current_approvers(
        deposit, getattr(request, "submitted_by", None),
        event_type=DEPOSIT_SUBMITTED, title="Deposit approval required",
        noun="Deposit", request=request,
    )


def publish_receipt_decision(receipt, submitter, *, approved, actor=None, reason=""):
    """Notify the receipt's submitter of an approval/rejection outcome.

    ``receipt``   : the PaymentReceipt (passed as the generic entity).
    ``submitter`` : the user who raised the receipt (the recipient).
    ``approved``  : True -> PAYMENT_APPROVED, False -> PAYMENT_REJECTED.
    ``actor``     : the approver who acted (logged, not persisted).
    ``reason``    : optional rejection reason appended to the message.

    Returns the created Notification records (possibly empty). Never raises for a
    delivery problem — the framework isolates that.
    """
    if submitter is None:
        logger.info(
            "payments notification skipped: receipt %s has no submitter",
            getattr(receipt, "pk", None),
        )
        return []

    # Local import keeps module load order clean and the direction explicit.
    from notifications.services import notify

    if approved:
        event_type = PAYMENT_APPROVED
        title = "Payment approved"
        message = f"Payment {receipt.receipt_no} has been approved by {_display_name(actor)}."
    else:
        event_type = PAYMENT_REJECTED
        title = "Payment rejected"
        message = f"Payment {receipt.receipt_no} was rejected by {_display_name(actor)}."
        if reason:
            message += f" Reason: {reason}"

    return notify(
        event_type=event_type,
        title=title,
        message=message,
        recipients=[submitter],
        entity=receipt,
        actor=actor,
    )


def publish_deposit_decision(deposit, submitter, *, approved, actor=None, reason=""):
    """Notify the deposit's submitter of an approval/rejection outcome.

    Mirrors :func:`publish_receipt_decision`. The only differences are the event
    names, the wording and the entity — the framework and its guarantees are
    identical (generic ``entity`` = the BankDeposit, no deposit FK in the
    framework; company derived per-recipient because ``BankDeposit.company`` is a
    SAP category string, not a ``users.Company`` FK).

    ``deposit``   : the BankDeposit (passed as the generic entity).
    ``submitter`` : the user who raised the deposit (the recipient).
    ``approved``  : True -> DEPOSIT_APPROVED, False -> DEPOSIT_REJECTED.
    ``actor``     : the approver who acted (logged, not persisted).
    ``reason``    : optional rejection reason appended to the message.

    Returns the created Notification records (possibly empty). Never raises for a
    delivery problem — the framework isolates that.
    """
    if submitter is None:
        logger.info(
            "payments notification skipped: deposit %s has no submitter",
            getattr(deposit, "pk", None),
        )
        return []

    from notifications.services import notify

    if approved:
        event_type = DEPOSIT_APPROVED
        title = "Deposit approved"
        message = f"Deposit {deposit.deposit_no} has been approved by {_display_name(actor)}."
    else:
        event_type = DEPOSIT_REJECTED
        title = "Deposit rejected"
        message = f"Deposit {deposit.deposit_no} was rejected by {_display_name(actor)}."
        if reason:
            message += f" Reason: {reason}"

    return notify(
        event_type=event_type,
        title=title,
        message=message,
        recipients=[submitter],
        entity=deposit,
        actor=actor,
    )
