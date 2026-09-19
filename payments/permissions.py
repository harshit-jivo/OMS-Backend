"""Action permissions for the payments module.

The project already has a page-visibility mechanism (`User.extra_pages`, a list
of string keys granted by an admin on the Permissions page). These four keys
extend it from "which pages can you open" to "which actions can you take":

    Payments_Create   raise a payment receipt
    Payments_Approve  approve / reject a payment receipt
    Deposit_Create    raise a bank deposit
    Deposit_Approve   approve / reject a bank deposit

They are deliberately stored in the SAME `extra_pages` list rather than a new
model: the grant UI, the login payload, the mobile client and the web sidebar
all already read that field, so a new table would mean touching every one of
them and leaving two sources of truth for "what may this user do".

IMPORTANT: these are enforced here, server-side. Hiding a button in the client
is a usability affordance, not a security boundary — every one of these checks
must hold even when the request does not come from our own UI.
"""
from rest_framework.permissions import BasePermission

# The project's single definition of "admin" (was a fifth local copy here).
# Same rule this module already used — role, is_staff, is_superuser — plus the
# `extra_roles` lookup that `users/models.py` requires of every role check and
# that none of the five copies performed.
from core.permissions import is_admin

# ---------------------------------------------------------------------------
# Keys — must match ASSIGNABLE_PAGES in the web/mobile clients EXACTLY.
# ---------------------------------------------------------------------------

PAYMENTS_CREATE = 'Payments_Create'
# The handover gate: check the physical money against the entry before it may
# enter the approval chain. Registered in core/permission_registry.py too —
# BOTH are required. The registry is what `effective_keys` intersects against,
# so a key missing there is dropped from the `permissions[]` the clients read;
# this list is what `granted_keys` and /my-permissions/ answer from.
PAYMENTS_VERIFY = 'Payments_Verify'
PAYMENTS_APPROVE = 'Payments_Approve'
DEPOSIT_CREATE = 'Deposit_Create'
DEPOSIT_APPROVE = 'Deposit_Approve'

# Read-only access to the analytics dashboard — every receipt and deposit in the
# company, in aggregate. Deliberately SEPARATE from the four action keys above:
# the people who record and approve payments are not automatically the people
# who should see company-wide collection totals, and a manager who should see
# the totals has no business raising a receipt. Neither implies the other.
PAYMENTS_DASHBOARD = 'Payments_Dashboard'

ACTION_PERMISSION_KEYS = [
    PAYMENTS_CREATE,
    PAYMENTS_VERIFY,
    PAYMENTS_APPROVE,
    DEPOSIT_CREATE,
    DEPOSIT_APPROVE,
    PAYMENTS_DASHBOARD,
]

# Human labels, surfaced by the /api/payments/my-permissions/ endpoint so the
# clients never hardcode their own copy of this wording.
ACTION_PERMISSION_LABELS = {
    PAYMENTS_CREATE: 'Payments — Create',
    PAYMENTS_VERIFY: 'Payments — Verify (handover)',
    PAYMENTS_APPROVE: 'Payments — Approve',
    DEPOSIT_CREATE: 'Deposit — Create',
    DEPOSIT_APPROVE: 'Deposit — Approve',
    PAYMENTS_DASHBOARD: 'Payments Dashboard',
}


# NOTE: there is deliberately no role -> permission map.
#
# A role is IDENTITY ("this account works in payments"); it grants nothing. All
# authority comes from two places, and only these two:
#
#   1. `extra_pages`      — the boxes an admin ticks: which pages the user
#                           opens and which actions they may take.
#   2. Workflow assignment — whether they are an approver at a level, which
#                           `approvals` resolves per document.
#
# One dummy payments role is enough for every payments user, because the
# permissions decide everything. A map here would be a THIRD source of
# authority that silently overrides the admin's choices: a user ticked for one
# action would be handed four by their role, which is the opposite of what the
# Permissions page appears to promise.
#
# This was already the live behaviour — granted_keys() never consulted the map —
# so removing it changes no user's access. See tests_permissions.py.


def granted_keys(user):
    """The permission keys this user holds.

    `extra_pages` — the boxes an admin ticks on the Permissions page — is the
    ONLY source, apart from admin, who holds everything implicitly.

    The role is IDENTITY, not authority. `payments_and_deposit` says "this
    account exists to work on payments and deposits"; it does not say which of
    the four actions they may take. Granting from the role as well meant a user
    with two boxes ticked was silently given all four — the admin's choice was
    overwritten by the role, which is the opposite of what the Permissions page
    appears to promise.

    That separation is also what lets ANY role hold a payments permission: a
    manager ticked for Payments_Create can raise a receipt without being given
    a payments role at all.
    """
    if not user or not user.is_authenticated:
        return set()
    if is_admin(user):
        return set(ACTION_PERMISSION_KEYS)

    return {
        str(k).strip() for k in (user.extra_pages or [])
        if str(k).strip() in ACTION_PERMISSION_KEYS
    }


def has_permission_key(user, key):
    """True when `user` may perform the action identified by `key`."""
    return key in granted_keys(user)


class _KeyPermission(BasePermission):
    """Base for the four action permissions."""

    key = ''
    message = 'You do not have permission to perform this action.'

    def has_permission(self, request, view):
        return has_permission_key(request.user, self.key)


class CanCreatePayment(_KeyPermission):
    key = PAYMENTS_CREATE
    message = 'You do not have permission to create payment receipts.'


class CanVerifyPayment(_KeyPermission):
    """Holders may verify (hand over) a payment receipt.

    Capability only. Two further checks live on the endpoint because they are
    per-record, not per-user: the receipt must still be PENDING verification,
    and the creator may never verify their own receipt.
    """

    key = PAYMENTS_VERIFY
    message = 'You do not have permission to verify payment receipts.'


class CanApprovePayment(_KeyPermission):
    key = PAYMENTS_APPROVE
    message = 'You do not have permission to approve payment receipts.'


class CanCreateDeposit(_KeyPermission):
    key = DEPOSIT_CREATE
    message = 'You do not have permission to create bank deposits.'


class CanApproveDeposit(_KeyPermission):
    key = DEPOSIT_APPROVE
    message = 'You do not have permission to approve bank deposits.'


class CanViewPaymentsDashboard(_KeyPermission):
    """Guards every analytics endpoint.

    The dashboard aggregates across EVERY receipt and deposit in the company,
    which is more than any individual collector sees in the operational screens.
    Hiding the menu entry is not sufficient — the endpoints answer to anyone who
    knows the URL unless the check is here.
    """

    key = PAYMENTS_DASHBOARD
    message = 'You do not have permission to view the payments dashboard.'


class ReadOrCreatePayment(BasePermission):
    """GET for any authenticated user; POST requires Payments_Create.

    The list and create endpoints share one view, and a user who may only
    approve still has to be able to READ the queue they are approving.
    """

    message = 'You do not have permission to create payment receipts.'

    def has_permission(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return bool(request.user and request.user.is_authenticated)
        return has_permission_key(request.user, PAYMENTS_CREATE)


class ReadOrCreateDeposit(BasePermission):
    """GET for any authenticated user; POST requires Deposit_Create."""

    message = 'You do not have permission to create bank deposits.'

    def has_permission(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return bool(request.user and request.user.is_authenticated)
        return has_permission_key(request.user, DEPOSIT_CREATE)


# ---------------------------------------------------------------------------
# Acting on a document in approval — the two-condition rule
# ---------------------------------------------------------------------------
#
# Under the Workflow Engine, holding the approve key is NOT enough and being
# the stage's user is NOT enough. Both are required, and the second is resolved
# from the engine on every check rather than from anything stored on the flow:
# a stage reassigned, or a temporary replacement started, changes who may act
# with nothing in payments updated.
#
# See docs/Approvals/WORKFLOW_MODULE_INTEGRATION.md §8 and §13 ("approving
# requires BOTH the permission key AND being that effective user").


def approve_key_for(document):
    """The permission key that lets someone approve THIS kind of document."""
    from .models import PaymentReceipt

    return (PAYMENTS_APPROVE if isinstance(document, PaymentReceipt)
            else DEPOSIT_APPROVE)


def may_act_on(user, flow, document=None):
    """(allowed, reason) — may this user decide this document right now?

    The two refusals say different things on purpose: "you may not approve
    payments at all" and "this one is not yours" are different problems with
    different fixes, and a single message would send people to the wrong one.
    """
    from .models import FlowStatus
    from .workflow_flow import effective_user_id

    if not user or not getattr(user, 'is_authenticated', False):
        return False, 'Authentication required.'

    document = document if document is not None else _document_of(flow)
    if not has_permission_key(user, approve_key_for(document)):
        return False, 'You do not have permission to approve this document.'

    if flow is None or flow.status != FlowStatus.PENDING:
        return False, 'This document is not awaiting approval.'
    if not flow.current_stage_id:
        return False, 'This document is not waiting at any stage.'

    effective = effective_user_id(flow.current_stage_id)
    if effective is None:
        return False, ('This stage is no longer configured in the workflow. '
                       'Ask a workflow administrator to check it.')
    if effective != user.pk:
        return False, 'This document is not awaiting your approval.'

    # --- SAP state -------------------------------------------------------
    #
    # The flow alone cannot answer this. Since the final approval no longer
    # completes the flow (payments/workflow_flow.py), a document whose SAP post
    # is in flight, already succeeded, or may have succeeded is still sitting
    # at its final stage with a PENDING flow — and must not be approved again.
    #
    # PENDING_ERROR is the one SAP state that deliberately stays actionable:
    # SAP answered "no", nothing was committed there, and the approver holding
    # the document is the person who corrects it and retries.
    from .workflow_flow import UNPOSTABLE_STATUSES, is_at_final_stage

    if getattr(document, 'sap_doc_entry', None):
        return False, 'This document is already posted to SAP.'

    status = getattr(document, 'status', '')
    if status in UNPOSTABLE_STATUSES:
        if status == 'POSTING_TO_SAP':
            return False, ('A SAP posting for this document is already in '
                           'progress. Wait for it to finish.')
        if status == 'SAP_UNKNOWN':
            return False, ('SAP did not answer the last posting, so it is not '
                           'yet known whether this document was created there. '
                           'It must be verified before it can be posted again.')
        return False, 'This document is already posted to SAP.'

    # A RETRY may only happen at the FINAL stage — that is the only stage whose
    # approval posts to SAP, so it is the only one a SAP failure can return to.
    # A PENDING_ERROR document parked anywhere else is a configuration problem,
    # and approving it there would advance the workflow without ever retrying
    # the posting that failed.
    if status == 'PENDING_ERROR' and not is_at_final_stage(flow):
        return False, ('This document failed to post to SAP and is not at its '
                       'final approval stage, so it cannot be retried here. '
                       'Ask a workflow administrator to check the workflow.')

    # NOBODY APPROVES THEIR OWN MONEY. The old engine enforced this with a
    # `forbid_self_approval` flag on each workflow; the new engine has no such
    # concept, so it lives here — it is a payments business rule, not routing.
    # A configuration that names the raiser as their own stage user therefore
    # blocks rather than silently self-approving.
    if getattr(document, 'created_by_id', None) == user.pk:
        return False, 'You cannot approve a document you raised yourself.'

    return True, ''


def _document_of(flow):
    from .models import PaymentReceiptFlow

    if flow is None:
        return None
    return flow.receipt if isinstance(flow, PaymentReceiptFlow) else flow.deposit


class IsPaymentsApprovalAdmin(BasePermission):
    """Who may manage payments master data and approval configuration.

    Replaces `approvals.permissions.IsApprovalAdmin`, which lived in the engine
    being retired and imported payments' own keys back out of it. Same rule:
    an administrator, or a holder of the payments dashboard key.
    """

    message = 'You do not have permission to manage payments configuration.'

    def has_permission(self, request, view):
        from core.permissions import is_admin

        user = request.user
        if not (user and user.is_authenticated):
            return False
        if is_admin(user):
            return True
        return has_permission_key(user, PAYMENTS_DASHBOARD)
