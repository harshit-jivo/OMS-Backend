"""Permission classes for the approval engine.

Modelled on tracker/permissions.py:53-86 — real BasePermission subclasses
rather than inline role-name string comparisons scattered through views.
"""
from rest_framework.permissions import BasePermission


def is_admin(user):
    """Admin by role name, Django staff, or superuser.

    Three near-identical `is_admin` helpers already exist in the project
    (devices/permissions.py:11, uilabels/permissions.py:12, users/views.py:867)
    and they disagree — two ignore is_superuser. This one honours all three.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    role = getattr(getattr(user, 'role', None), 'name', '')
    return str(role).strip().lower() == 'admin'


class IsApprovalAdmin(BasePermission):
    """Manage workflows, levels and approver grants."""

    message = 'Only an administrator may configure approval workflows.'

    def has_permission(self, request, view):
        return is_admin(request.user)
