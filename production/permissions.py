"""Who may see production orders, and who may decide one.

THE RULE THIS FILE EXISTS TO ENFORCE
------------------------------------
Approving is gated on TWO independent conditions, both checked on the server
from the authenticated user:

    1. the caller holds `Production_Order_Approval`   — may they approve at all?
    2. the caller IS the current effective user of    — is this THEIR stage?
       that flow's workflow stage

Neither is sufficient. A permission without an assignment approves nothing; an
assignment without the permission approves nothing.

This is also where the company question is answered, and it is answered by NOT
asking it. OMS has no user→company mapping that scopes documents — `users.User.company`
is an organisational master that `core/companies.py` explicitly says does not
scope documents, and `payments.user_companies()` returns every company to
everyone. So company scoping here comes from the STAGE: a stage exists inside
one workflow, a workflow names one company, and naming a user on that stage is
exactly "this person approves for this company". Nothing needs to read a
company off the user record.

`effective_user_id`, not the configured user: a temporary replacement
(`workflow_user_replacements`) must be able to act during its window, and the
configured user must not.
"""
from rest_framework.permissions import BasePermission

from core.permissions import HasKey, effective_keys

#: View production order requests and their history.
VIEW_KEY = 'Production_Order'
#: Open the approval desk. NOT sufficient to approve — see `may_act_on`.
APPROVAL_KEY = 'Production_Order_Approval'


def has_view_access(user):
    return VIEW_KEY in effective_keys(user)


def has_approval_access(user):
    return APPROVAL_KEY in effective_keys(user)


def CanViewRequests():
    return HasKey(VIEW_KEY)


def CanOpenApprovalDesk():
    return HasKey(APPROVAL_KEY)


class IsProductionApprover(BasePermission):
    """Holds `Production_Order_Approval`. The stage check is per-flow.

    Kept separate from `may_act_on` because a view can answer "may you see
    this desk?" before it knows which request is being acted on.
    """

    message = 'You do not have permission to approve production orders.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and has_approval_access(request.user))


def may_act_on(user, flow):
    """`(allowed, reason)` for approving or rejecting `flow`.

    The two failures give different messages on purpose: "you lack the
    permission" sends someone to an administrator, "this is not your stage"
    does not.
    """
    from production.models import FlowStatus
    from production.services import flow as flow_service

    if not (user and user.is_authenticated):
        return False, 'Not authenticated.'
    if not has_approval_access(user):
        return False, 'You do not have permission to approve production orders.'
    if flow.status != FlowStatus.PENDING:
        return False, f'This request is already {flow.get_status_display().lower()}.'
    if not flow.current_stage_id:
        return False, 'This request is not waiting at any stage.'

    effective = flow_service.effective_user_id(flow.current_stage_id)
    if effective != user.pk:
        return False, 'This request is not waiting for your decision.'
    return True, ''
