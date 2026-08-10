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
    """Manage workflows, levels, approver grants and the payments masters.

    Two ways in:

    * an administrator, as before; or
    * a holder of `Payments_Dashboard`, the permission that opens the Payments
      Dashboard page. The four configuration tabs (Workflows, Levels, Approvers,
      Masters) live ON that page, so granting the page without them left the
      tabs visible but every request behind them returning 403 — a dead-end the
      user could see but not use.

    This is a deliberate widening, not an oversight. `Payments_Dashboard` now
    confers WRITE access to approval routing: a holder can edit levels and add
    approvers, including themselves. It is therefore a privileged grant, not the
    read-only analytics key it began as — assign it accordingly. The one thing
    it does NOT open is approving your own document; `services.act` blocks that
    independently of any permission, so this cannot become a self-approval path.
    """

    message = (
        'You need administrator rights or the Payments Dashboard permission to '
        'configure approvals.'
    )

    def has_permission(self, request, view):
        if is_admin(request.user):
            return True
        # Imported inside the method: payments.views imports THIS module, so a
        # module-level import of payments.permissions would close the cycle.
        from payments.permissions import PAYMENTS_DASHBOARD, has_permission_key

        return has_permission_key(request.user, PAYMENTS_DASHBOARD)
