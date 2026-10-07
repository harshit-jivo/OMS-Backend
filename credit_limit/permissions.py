"""Two keys, as in BackDate: raising a request and deciding one are different
authorities. `Credit_Limit_Approval` opens the desk; acting also requires being
the current stage's effective user (`services.flow.may_act_on`)."""
from rest_framework.permissions import BasePermission

from core.permissions import HasKey, effective_keys

REQUEST_KEY = 'Credit_Limit'
APPROVAL_KEY = 'Credit_Limit_Approval'


def has_request_access(user):
    return REQUEST_KEY in effective_keys(user)


def has_approval_access(user):
    return APPROVAL_KEY in effective_keys(user)


def CanRaiseRequests():
    return HasKey(REQUEST_KEY)


def CanOpenApprovalDesk():
    return HasKey(APPROVAL_KEY)


class CanReadRequests(BasePermission):
    """Either key. Which requests is decided per object in the view."""

    message = 'You do not have permission to view credit-limit requests.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated
                    and (has_request_access(user)
                         or has_approval_access(user)))
