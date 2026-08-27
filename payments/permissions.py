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
    PAYMENTS_APPROVE,
    DEPOSIT_CREATE,
    DEPOSIT_APPROVE,
    PAYMENTS_DASHBOARD,
]

# Human labels, surfaced by the /api/payments/my-permissions/ endpoint so the
# clients never hardcode their own copy of this wording.
ACTION_PERMISSION_LABELS = {
    PAYMENTS_CREATE: 'Payments — Create',
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
