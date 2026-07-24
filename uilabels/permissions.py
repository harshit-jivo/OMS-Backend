"""Permissions for the UI-label admin API.

Authorization in this project is by the `User.role` relation (a `users.UserRole`
row), NOT Django's `is_staff` / groups / permissions framework. This mirrors the
existing check in `users.views.PagePermissionsView._is_admin` and
`devices.permissions.IsAdminRole` so there is exactly one definition of "admin"
behaviour across the codebase.
"""
from rest_framework.permissions import BasePermission


def is_admin_user(user) -> bool:
    """True when `user` holds the 'admin' role (or is a Django superuser)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    return bool(role and str(getattr(role, "name", "")).strip().lower() == "admin")


class IsAdminRole(BasePermission):
    """Allow only admins to create/update/delete UI labels.

    Reading labels is open to any authenticated user (they drive the UI for
    everyone); only editing is admin-gated.
    """

    message = "Admin access required."

    def has_permission(self, request, view):
        return is_admin_user(request.user)
