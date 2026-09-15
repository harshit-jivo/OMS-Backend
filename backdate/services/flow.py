"""The BackDate business flow: create, update, approve, reject.

WHERE THE ENGINE STOPS AND THIS MODULE STARTS
---------------------------------------------
    Workflow Engine                     BackDate module
    ---------------                     ---------------
    which workflow applies?      -->    create the flow at its first stage
    what are its stages/users?   -->    advance, complete, reject
                                        write the history
                                        decide when SAP is written to

`selection.select_for_module()` is the only engine call in the submit path, and
it writes nothing. Everything below builds this module's own rows from the
answer it returns.

NO TASK TABLE
-------------
A flow holds the stage it is waiting at. The stages it has already passed are
in the action log; the stages ahead of it are configuration, which the engine
already stores. A row per stage per request restated all three and had to be
kept in step with them.

So "who has to act" is one resolution, done on demand:

    flow.current_stage  ->  get_stage_assignment(stage_id)  ->  effective user

WHAT IS DELIBERATELY ABSENT
---------------------------
No quorum, no approval counts, no round/attempt numbers. JSAP's engine supports
them; BKDT never used any of it — every live stage requires exactly one
approval from exactly one user. One approve completes a stage; one reject ends
the flow.

No resubmission either. JSAP has no rework path: a rejected request is terminal
and the user raises a new one, which re-runs selection against the CURRENT
configuration. That is the engine's normal path and needs no special support.
"""
import logging

from django.db import transaction

from workflow.exceptions import WorkflowError
from workflow.services import selection
from workflow.services.assignments import get_stage_assignment

from backdate.models import (
    BackDateActionLog,
    BackDateFlow,
    FlowStatus,
    LogAction,
)
from backdate.services import notify as notify_service

logger = logging.getLogger(__name__)

MODULE_CODE = 'BKDT'


class BackDateError(Exception):
    """A business rule refused the operation. Carries a safe message."""


def log(backdate, *, action, user=None, stage_id=None, remarks='',
        action_data=None):
    """Append one history row. Never updates an existing one."""
    return BackDateActionLog.objects.create(
        backdate=backdate,
        action=action,
        acted_by=user,
        stage_id=stage_id,
        remarks=remarks or '',
        action_data=action_data,
    )


def effective_user_id(stage_id):
    """Who must act on `stage_id` TODAY, replacements applied.

    The single resolution point. Everything else in this module — the queue,
    the permission check, `flow.current_user` — goes through it, so there is
    exactly one definition of "current responsibility".
    """
    if not stage_id:
        return None
    assignment = get_stage_assignment(stage_id)
    return assignment.effective_user_id if assignment else None


def _point_at(flow, stage_id):
    """Move the flow to one stage and refresh its denormalised user."""
    flow.current_stage_id = stage_id
    flow.current_user_id = effective_user_id(stage_id)


@transaction.atomic
def submit(backdate, *, user):
    """Create the flow: select a workflow and open its first stage.

    Atomic with the caller's request creation, so a selection failure rolls the
    business row back too — a request that could never be routed should not
    exist. JSAP instead left such rows behind with no flow at all, silently.

    Raises `BackDateError` when the engine cannot choose exactly one workflow;
    the engine's own message says which case it was (`WorkflowNotConfigured`,
    `AmbiguousWorkflowSelection`) and is safe to surface.
    """
    if BackDateFlow.objects.filter(backdate=backdate).exists():
        raise BackDateError('This request has already been submitted.')

    try:
        chosen = selection.select_for_module(
            module_code=MODULE_CODE,
            document_id=backdate.pk,
            company=backdate.company,
        )
    except WorkflowError as exc:
        # The engine's messages are written for an operator and carry no SQL
        # or connection detail, so they pass straight through.
        raise BackDateError(str(exc)) from exc

    if not chosen.stages:
        raise BackDateError(
            f'Workflow "{chosen.workflow.code}" has no active stages '
            f'configured, so this request cannot be approved by anyone.')

    first = chosen.stages[0]
    flow = BackDateFlow(
        backdate=backdate,
        status=FlowStatus.PENDING,
        workflow=chosen.workflow,
        total_stage=len(chosen.stages),
    )
    _point_at(flow, first.id)
    flow.save()

    # CREATE, not SUBMIT: creation and submission are the same transaction, so
    # two rows would record one instant twice. `stage_id` is NULL because
    # nothing has been decided yet — the stage is where it now waits, which the
    # flow already says.
    log(backdate, action=LogAction.CREATE, user=user,
        remarks=backdate.remarks)
    notify_service.stage_awaiting(flow, backdate)
    return flow


def stages_for(flow):
    """The workflow's active stages, in order, as the engine has them NOW."""
    return selection.stages_for(flow.workflow)


def _locked(flow):
    """Re-read the flow FOR UPDATE so two approvers cannot both complete it."""
    return (BackDateFlow.objects
            .select_for_update().select_related('backdate').get(pk=flow.pk))


def _guard(flow):
    if flow.status != FlowStatus.PENDING:
        raise BackDateError(
            f'This request is already {flow.get_status_display().lower()}.')
    if not flow.current_stage_id:
        raise BackDateError('This request is not waiting at any stage.')


@transaction.atomic
def approve(flow, *, user, remarks=''):
    """Approve the current stage. Advances, or completes the flow.

    The caller must already have passed `permissions.may_act_on` — this
    function is the business transition, not the gate.

    Returns the flow.
    """
    flow = _locked(flow)
    _guard(flow)

    decided_stage_id = flow.current_stage_id
    log(flow.backdate, action=LogAction.APPROVE, user=user,
        stage_id=decided_stage_id, remarks=remarks)

    # The NEXT active stage as configured right now. Re-read rather than
    # remembered, so a stage deactivated mid-flight is skipped rather than
    # deadlocking the request.
    stages = stages_for(flow)
    current = next((s for s in stages if s.id == decided_stage_id), None)
    following = ([s for s in stages if s.sequence > current.sequence]
                 if current else [])

    if not following:
        flow.status = FlowStatus.APPROVED
        _point_at(flow, None)
        flow.save(update_fields=['status', 'current_stage', 'current_user',
                                 'updated_at'])
        # The requester is told the decision here; whether SAP then accepted
        # the grant is reported separately by the caller, because it is a
        # different fact and can fail on its own.
        notify_service.decided(flow.backdate, approved=True, actor=user)
        return flow

    _point_at(flow, following[0].id)
    flow.save(update_fields=['current_stage', 'current_user', 'updated_at'])
    notify_service.stage_awaiting(flow, flow.backdate)
    return flow


@transaction.atomic
def reject(flow, *, user, remarks):
    """Reject the current stage. One rejection ends the flow.

    Terminal for this request, not for the document: the user may raise a new
    request, which re-runs selection against the current configuration.
    """
    if not (remarks or '').strip():
        raise BackDateError('A reason is required when rejecting a request.')

    flow = _locked(flow)
    _guard(flow)

    decided_stage_id = flow.current_stage_id
    log(flow.backdate, action=LogAction.REJECT, user=user,
        stage_id=decided_stage_id, remarks=remarks)

    flow.status = FlowStatus.REJECTED
    # No stage is waiting any more, so the queue stops showing it without a
    # per-stage status to sweep.
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    notify_service.decided(flow.backdate, approved=False, actor=user,
                           remarks=remarks)
    return flow


def pending_for(user, *, on_date=None):
    """Flows awaiting THIS user's decision today, stand-ins included.

    Resolved through the engine's replacement mapping rather than trusting
    `current_user`: that column is refreshed when a flow MOVES, so a stage
    whose user changed — or a replacement window that opened — while the flow
    sat still would otherwise be missed. This is the query that has to stay
    correct for plan §44/§45; `current_user` is for display.
    """
    from workflow.models import WorkflowStage
    from workflow.services import replacements

    on_date = on_date or replacements.today()
    owners = replacements.configured_users_acting_for(user.pk, on_date)
    if not owners:
        return BackDateFlow.objects.none()

    stage_ids = list(
        WorkflowStage.objects
        .filter(user_id__in=owners, is_active=True)
        .values_list('id', flat=True)
    )
    if not stage_ids:
        return BackDateFlow.objects.none()

    return (
        BackDateFlow.objects
        .filter(status=FlowStatus.PENDING, current_stage_id__in=stage_ids)
        .select_related('backdate', 'backdate__created_by', 'current_stage')
        .order_by('-created_at')
    )


def decided_backdate_ids(user, status=None):
    """Requests this user has approved or rejected, optionally by outcome.

    A user decides one STAGE, not the request: approving stage 1 of three
    leaves the flow pending. So the set is "requests I acted on", narrowed by
    the flow's CURRENT status.
    """
    ids = (BackDateActionLog.objects
           .filter(acted_by=user,
                   action__in=[LogAction.APPROVE, LogAction.REJECT])
           .values_list('backdate_id', flat=True))
    qs = BackDateFlow.objects.filter(backdate_id__in=set(ids))
    if status:
        qs = qs.filter(status=status)
    return set(qs.values_list('backdate_id', flat=True))
