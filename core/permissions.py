"""Project-wide authorization primitives.

The single definition of "who is this user allowed to be" for the whole
project. Everything else — `devices`, `uilabels`, `approvals`, `payments`,
`tracker`, `users` — resolves through here.

Why this module exists
----------------------
Before it, FIVE modules each answered "is this user an admin" and they did not
agree:

    devices/permissions.py:is_admin_user   role only  (superuser NOT admin)
    uilabels/permissions.py:is_admin_user  role + is_superuser
    approvals/permissions.py:is_admin      role + is_superuser + is_staff
    payments/permissions.py:is_admin       role + is_superuser + is_staff
    users.views.PagePermissionsView        role only  (superuser NOT admin)

Each one carried a docstring claiming it mirrored the others "so there is
exactly one definition of admin across the codebase". None of them did. A
superuser could edit UI labels but not device policy, for reasons no one chose.

All five also read `User.role` alone, while `users/models.py` states the rule
explicitly:

    "A user's roles are the primary FK plus any extra_roles. Every 'does this
     user hold role X' check must consult BOTH, or a user granted a function
     role through extra_roles would be invisible to half the codebase."

This module is that rule, implemented once.

What changed in behaviour
-------------------------
Adopting this is a deliberate WIDENING in two directions, both of them the
behaviour the model layer already promised:

1. `extra_roles` now counts. A user whose primary role is `manager` and who
   holds `admin` in `extra_roles` is an admin here. Previously they were not,
   anywhere.
2. `is_superuser` / `is_staff` now count everywhere, not in two apps out of
   five.

Neither widening can grant access to someone an administrator did not already
name — both fields are admin-assigned. See `users/tests.py`.

What this module is NOT
-----------------------
It is not a role -> capability map, and one must not be added here. `payments`
argues the case in full (`payments/permissions.py`, the NOTE block): a role is
IDENTITY, authority comes from `extra_pages` grants and from workflow
assignment. A map in this module would become a third source of authority that
silently overrides the admin's ticked boxes.

`tracker` is the one intentional exception, and it owns its own map
(`tracker/permissions.py:ROLE_PAGE_MAP`) because its pages ARE its roles.
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

# ---------------------------------------------------------------------------
# Role identity
# ---------------------------------------------------------------------------

ADMIN_ROLE = 'admin'

#: Roles that confer administrative authority over other accounts. Assigning
#: one of these to a user is equivalent to handing over the admin account, so
#: only an admin may do it — see `users.serializers.assert_may_assign_roles`.
#:
#: `tracker_admin` is in this set even though tracker access is otherwise
#: unprivileged: a tracker admin edits stage configuration, which decides which
#: documents every other tracker user can see and move.
PRIVILEGED_ROLE_NAMES = frozenset({ADMIN_ROLE, 'tracker_admin'})


def role_names(user) -> frozenset:
    """Every role name `user` holds — the primary FK plus `extra_roles`.

    Lowercased and stripped, so callers compare against literals safely.
    Returns an empty set for anonymous users and for anything that is not a
    `users.User` (AnonymousUser has no `role`).

    Prefers `User.all_role_names()` — the model owns this rule — and falls back
    to the `role` FK alone only when the M2M is unavailable, which happens
    during migrations and in tests that build a bare user object.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return frozenset()

    all_names = getattr(user, 'all_role_names', None)
    if callable(all_names):
        try:
            return frozenset(str(n).strip().lower() for n in all_names() if n)
        except Exception:  # noqa: BLE001 — unmigrated DB / detached instance
            pass

    role = getattr(user, 'role', None)
    name = str(getattr(role, 'name', '') or '').strip().lower()
    return frozenset({name}) if name else frozenset()


def has_role(user, *names) -> bool:
    """True when `user` holds any of `names`, as a primary OR extra role."""
    wanted = {str(n).strip().lower() for n in names if n}
    return bool(wanted & role_names(user))


def is_admin(user) -> bool:
    """The project's one definition of "administrator".

    Three ways in, and they are equivalent by design:

    * the `admin` role, held as primary or via `extra_roles`;
    * `is_superuser` — full Django admin access already implies everything;
    * `is_staff` — same, and `approvals`/`payments` have always honoured it.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    return ADMIN_ROLE in role_names(user)


def holds_privileged_role(user) -> bool:
    """True when `user` holds any role from `PRIVILEGED_ROLE_NAMES`."""
    return bool(PRIVILEGED_ROLE_NAMES & role_names(user))


# ---------------------------------------------------------------------------
# Permission classes
# ---------------------------------------------------------------------------


class IsAdminRole(BasePermission):
    """Administrators only.

    Use for anything that reads or writes across users: account management,
    role and party assignment, org-wide configuration, device inventory.
    """

    message = 'Administrator access required.'

    def has_permission(self, request, view):
        return is_admin(request.user)


class IsAuthenticatedReadAdminWrite(BasePermission):
    """Any authenticated user may read; only an administrator may write.

    The right default for master data — states, companies, categories, roles,
    UI labels. Every logged-in client needs to render these; nobody but an
    admin should be editing them.
    """

    message = 'Administrator access required to modify this resource.'

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return is_admin(request.user)


class IsSelfOrAdmin(BasePermission):
    """The account holder, or an administrator.

    Object-level: pair with `IsAuthenticated` and call
    `check_object_permissions(request, user_obj)` in the view, because DRF only
    runs object checks for `get_object()` on generic views. `APIView` subclasses
    that fetch the row themselves must invoke it explicitly or this class does
    nothing at all.
    """

    message = 'You may only access your own account.'

    def has_object_permission(self, request, view, obj):
        if is_admin(request.user):
            return True
        # `obj` may be a User or something owning one (assignment rows).
        target = obj if hasattr(obj, 'is_authenticated') else getattr(obj, 'user', None)
        return bool(target is not None and target.pk == request.user.pk)
