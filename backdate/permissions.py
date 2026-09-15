"""Who may raise a BackDate request, and who may decide one.

THE RULE THIS FILE EXISTS TO ENFORCE
------------------------------------
Approving is gated on TWO independent conditions, and both are checked on the
server from the authenticated user:

    1. the caller holds `BackDate_Approval`            — may they approve at all?
    2. the caller IS the current effective user of      — is this THEIR stage?
       that task's workflow stage

Neither is sufficient. A permission without an assignment approves nothing; an
assignment without the permission approves nothing. This is the A/B/C matrix in
the migration plan (§14), and it is the whole reason approval is not just a
`HasKey`.

WHY IT IS NOT INHERITED FROM JSAP
---------------------------------
JSAP took the approver's id from the request body and passed it to a stored
procedure. The procedure did check stage assignment, so the exposure was not
"approve as anybody" — it was "approve as any *legitimate approver*", which for
a rights-granting module is the same thing in practice. Nothing here reads an
identity from the client.

`effective_user_id`, not the configured user: a temporary replacement
(`workflow_user_replacements`) must be able to act during its window, and the
configured user must not. Using the effective id is what makes delegation work
with no code in this module.
"""
from rest_framework.permissions import BasePermission

from core.permissions import HasKey, effective_keys

#: Raise and view your own BackDate requests.
REQUEST_KEY = 'BackDate'
#: Open the approval desk. NOT sufficient to approve — see `may_act_on_task`.
APPROVAL_KEY = 'BackDate_Approval'


def has_request_access(user):
    return REQUEST_KEY in effective_keys(user)


def has_approval_access(user):
    return APPROVAL_KEY in effective_keys(user)


def CanRaiseRequests():
    return HasKey(REQUEST_KEY)


def CanOpenApprovalDesk():
    return HasKey(APPROVAL_KEY)


class CanReadRequests(BasePermission):
    """May the caller READ a BackDate request at all — either key will do.

    Reading and deciding are different questions. An approver holds
    `BackDate_Approval` and often NOT `BackDate`, because they never raise a
    request of their own; requiring the requester key to open a detail or a
    history would leave them approving documents they are not allowed to look
    at. WHICH requests they may read is then decided per object in the view:
    your own, or anything if you run the approval desk.
    """

    message = 'You do not have permission to view BackDate requests.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated
                    and (has_request_access(user)
                         or has_approval_access(user)))


class IsBackDateApprover(BasePermission):
    """Holds `BackDate_Approval`. The stage check is per-flow, not per-view.

    Kept separate from `may_act_on` because a view can answer "may you see this
    desk?" before it knows which request is being acted on.
    """

    message = 'You do not have permission to approve BackDate requests.'

    def has_permission(self, request, view):
        return has_approval_access(request.user)


#: Why an edit was refused. Two different answers are needed — "this is not
#: yours" and "it is too late" — so the caller is told which, in its own words
#: rather than in an HTTP number this layer has no business knowing.
NOT_YOURS, TOO_LATE = 'NOT_YOURS', 'TOO_LATE'


def may_edit(user, backdate):
    """May this user edit this request RIGHT NOW? `(allowed, reason, why)`.

    The rule lives here, not in the view, because the UI has to ask the same
    question: an Edit control offered to somebody the server will refuse is
    worse than no control at all.

    Normally the requester, and only while nothing has been decided — an
    approver agreed to the request as it read in front of them.

    The exception is a SAP refusal. Nothing is approved in that state (the
    final approval calls SAP first and is only written if SAP accepted), the
    request is stuck at its last stage, and the person LOOKING at the database's
    error is the approver holding it. They may correct it and try again rather
    than relay it back through the requester.
    """
    from backdate.models import FlowStatus, HanaStatus, LogAction

    if not user or not getattr(user, 'is_authenticated', False):
        return False, 'Authentication required.', NOT_YOURS

    flow = getattr(backdate, 'flow', None)
    sap_refused = flow is not None and flow.hana_status == HanaStatus.FAILED

    if backdate.created_by_id != user.pk:
        if not sap_refused:
            return False, 'Only the requester can edit this request.', NOT_YOURS
        allowed, _reason = may_act_on(user, flow)
        if not allowed:
            return False, ('Only the requester or the approver holding this '
                           'request can edit it.'), NOT_YOURS

    if flow is not None and flow.status != FlowStatus.PENDING:
        return False, (f'This request is already '
                       f'{flow.get_status_display().lower()} and can no '
                       f'longer be edited.'), TOO_LATE

    # An approval already given was given to the request AS IT READ THEN. A SAP
    # refusal is the exception: nothing is approved in that state — the WRITE is
    # what failed — so correcting it is not rewriting history.
    if (flow is not None and not sap_refused
            and backdate.action_logs.filter(
                action=LogAction.APPROVE).exists()):
        return False, ('This request has already been approved at one of its '
                       'stages and can no longer be edited.'), TOO_LATE

    return True, '', ''


def may_act_on(user, flow):
    """Both conditions. Returns `(allowed, reason)`.

    `reason` distinguishes the two failures because they need different
    answers: one is "ask an administrator for the permission", the other is
    "this is not your stage". A single 403 message would send people to the
    wrong place.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False, 'Authentication required.'

    if not has_approval_access(user):
        return False, 'You do not have permission to approve BackDate requests.'

    # Resolved from the stage's CURRENT configuration on every call. That is
    # deliberate: an administrator changing a stage's user re-routes every
    # request already waiting there, with nothing in this module updated.
    # `flow.current_user` is NOT consulted — it is display state, and trusting
    # it here would quietly reintroduce the reassignment problem the design
    # exists to avoid.
    from backdate.services.flow import effective_user_id

    if not flow.current_stage_id:
        return False, 'This request is not awaiting approval.'

    effective_id = effective_user_id(flow.current_stage_id)
    if effective_id is None:
        return False, (
            'This stage is no longer configured in the workflow. Ask a '
            'workflow administrator to check the configuration.'
        )
    if effective_id != user.pk:
        return False, 'This request is not awaiting your approval.'

    return True, ''
