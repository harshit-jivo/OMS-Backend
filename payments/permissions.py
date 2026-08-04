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

# ---------------------------------------------------------------------------
# Keys — must match ASSIGNABLE_PAGES in the web/mobile clients EXACTLY.
# ---------------------------------------------------------------------------

PAYMENTS_CREATE = 'Payments_Create'
PAYMENTS_APPROVE = 'Payments_Approve'
DEPOSIT_CREATE = 'Deposit_Create'
DEPOSIT_APPROVE = 'Deposit_Approve'

ACTION_PERMISSION_KEYS = [
    PAYMENTS_CREATE,
    PAYMENTS_APPROVE,
    DEPOSIT_CREATE,
    DEPOSIT_APPROVE,
]

# Human labels, surfaced by the /api/payments/my-permissions/ endpoint so the
# clients never hardcode their own copy of this wording.
ACTION_PERMISSION_LABELS = {
    PAYMENTS_CREATE: 'Payments — Create',
    PAYMENTS_APPROVE: 'Payments — Approve',
    DEPOSIT_CREATE: 'Deposit — Create',
    DEPOSIT_APPROVE: 'Deposit — Approve',
}


def is_admin(user):
    """Admin by role name, Django staff, or superuser.

    Same three-way rule as approvals/permissions.py:is_admin — kept identical so
    "admin" cannot mean one thing in one module and something else in another.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    role = getattr(getattr(user, 'role', None), 'name', '')
    return str(role).strip().lower() == 'admin'


# Roles that confer an action permission just by being held. Assigning
# "Payment Approver" to a user is enough — an admin does not have to also tick
# the matching checkbox on the Permissions page, which would be two places to
# get right and one to forget.
ROLE_PERMISSION_MAP = {
    'payment_creator': PAYMENTS_CREATE,
    'payment_approver': PAYMENTS_APPROVE,
    'deposit_creator': DEPOSIT_CREATE,
    'deposit_approver': DEPOSIT_APPROVE,
}


def granted_keys(user):
    """The permission keys this user holds.

    Three sources, unioned: admin (all), the roles they hold (primary or extra),
    and explicit per-user grants in `extra_pages`.
    """
    if not user or not user.is_authenticated:
        return set()
    if is_admin(user):
        return set(ACTION_PERMISSION_KEYS)

    keys = {str(k).strip() for k in (user.extra_pages or []) if str(k).strip()}

    # Roles → permissions. `all_role_names` covers the primary FK and the M2M;
    # guarded with getattr so this still works for any user-like object that
    # predates the helper (e.g. a stub in a test).
    names = user.all_role_names() if hasattr(user, 'all_role_names') else set()
    for name in names:
        key = ROLE_PERMISSION_MAP.get(name)
        if key:
            keys.add(key)
    return keys


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
