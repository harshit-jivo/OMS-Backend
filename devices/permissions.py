"""Permissions for the device-management admin API.

Authorization in this project is by the `User.role` relation (a `users.UserRole`
row), NOT Django's `is_staff` / groups / permissions framework. This mirrors the
existing check in `users.views.PagePermissionsView._is_admin` so there is exactly
one definition of "admin" behaviour across the codebase.
"""
from rest_framework.permissions import BasePermission


def is_admin_user(user) -> bool:
    """True when `user` holds the 'admin' role."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    role = getattr(user, "role", None)
    return bool(role and str(getattr(role, "name", "")).strip().lower() == "admin")


class IsAdminRole(BasePermission):
    """Allow only users whose role is 'admin'.

    NOTE: the device tables expose every user's device names, activity and
    software inventory across the org, so these endpoints must never be
    AllowAny — unlike the older dashboard views in `orders.views`, whose
    permission choice is deliberately not copied here.
    """

    message = "Admin access required."

    def has_permission(self, request, view):
        return is_admin_user(request.user)
