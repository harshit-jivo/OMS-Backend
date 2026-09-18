"""The PRDO business flow: open, approve, reject, retire.

WHERE THE ENGINE STOPS AND THIS MODULE STARTS
---------------------------------------------
    Workflow Engine                     PRDO module
    ---------------                     -----------
    which workflow applies?      -->    open the flow at its first stage
    what are its stages/users?   -->    advance, complete, reject, retire
                                        write the history
                                        decide when SAP is written to

`selection.select_for_module()` is the only engine call on the intake path,
and it writes nothing.

NO TASK TABLE
-------------
A flow holds the stage it is waiting at. Passed stages are in the action log;
stages ahead are configuration the engine already stores. So "who has to act"
is one resolution, done on demand:

    flow.current_stage -> get_stage_assignment(stage_id) -> effective user

WHAT IS DELIBERATELY ABSENT
---------------------------
No quorum and no approval counts — every live JSAP production stage is one
user, one approval. One approve completes a stage; one reject ends the flow.

No resubmission. A rejected order is terminal in OMS: the planner cancels it
in SAP or raises a new one, which the sync picks up as a new request and
re-runs selection against the CURRENT configuration.
"""
import logging

from django.db import transaction
from django.utils import timezone

from workflow.exceptions import WorkflowError
from workflow.services import selection
from workflow.services.assignments import get_stage_assignment

from production.models import (
    FlowStatus,
    LogAction,
    ProductionOrderActionLog,
    ProductionOrderFlow,
)
from production.services import notify as notify_service

logger = logging.getLogger(__name__)

MODULE_CODE = 'PRDO'


class ProductionFlowError(Exception):
    """A business rule refused the operation. Carries a safe message."""


def log(order, *, action, user=None, stage_id=None, remarks='', action_data=None):
    """Append one history row. Never updates an existing one."""
    return ProductionOrderActionLog.objects.create(
        production_order=order,
        action=action,
        acted_by=user,
        stage_id=stage_id,
        remarks=remarks or '',
        action_data=action_data,
    )


def effective_user_id(stage_id):
    """Who must act on `stage_id` TODAY, replacements applied.

    The single resolution point. The queue, the permission check and
    `flow.current_user` all go through it, so there is exactly one definition
    of "current responsibility".
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
def open_flow(order):
    """Select a workflow for a freshly synced order and open its first stage.

    Atomic with the caller's upsert, so a selection failure rolls the snapshot
    back too — an order that cannot be routed should not sit in OMS looking
    like it is waiting for someone. JSAP instead left such rows behind with no
    flow at all, silently.

    Raises `ProductionFlowError` when the engine cannot choose exactly one
    workflow; the engine's own message says which case it was
    (`WorkflowNotConfigured`, `AmbiguousWorkflowSelection`) and is safe to
    surface to an operator.
    """
    if ProductionOrderFlow.objects.filter(production_order=order).exists():
        raise ProductionFlowError('This order already has an approval flow.')

    try:
        chosen = selection.select_for_module(
            module_code=MODULE_CODE,
            document_id=order.pk,
            company=order.company,
        )
    except WorkflowError as exc:
        # Engine messages are written for an operator and carry no SQL or
        # connection detail, so they pass straight through.
        raise ProductionFlowError(str(exc)) from exc

    if not chosen.stages:
        raise ProductionFlowError(
            f'Workflow "{chosen.workflow.code}" has no active stages '
            f'configured, so this order cannot be approved by anyone.')

    first = chosen.stages[0]
    flow = ProductionOrderFlow(
        production_order=order,
        status=FlowStatus.PENDING,
        workflow=chosen.workflow,
        total_stage=len(chosen.stages),
    )
    _point_at(flow, first.id)
    flow.save()

    log(order, action=LogAction.SYNC,
        remarks=f'Synced from SAP and routed to {chosen.workflow.code}.')
    # Inside the same transaction as the routing: an order that fails to
    # route is not stored, and must not have announced itself either.
    notify_service.stage_awaiting(flow, order)
    return flow


def stages_for(flow):
    """The workflow's active stages, in order, as the engine has them NOW."""
    return selection.stages_for(flow.workflow)


def _locked(flow):
    """Re-read FOR UPDATE so two approvers cannot both complete a flow."""
    return (ProductionOrderFlow.objects
            .select_for_update()
            .select_related('production_order')
            .get(pk=flow.pk))


def _guard(flow):
    if flow.status != FlowStatus.PENDING:
        raise ProductionFlowError(
            f'This request is already {flow.get_status_display().lower()}.')
    if not flow.current_stage_id:
        raise ProductionFlowError('This request is not waiting at any stage.')


@transaction.atomic
def approve(flow, *, user, remarks=''):
    """Approve the current stage. Advances, or completes the flow.

    The caller must already have passed `permissions.may_act_on` — this is the
    business transition, not the gate.
    """
    flow = _locked(flow)
    _guard(flow)

    decided_stage_id = flow.current_stage_id
    log(flow.production_order, action=LogAction.APPROVE, user=user,
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
        notify_service.decided(flow.production_order, approved=True, actor=user)
        return flow

    _point_at(flow, following[0].id)
    flow.save(update_fields=['current_stage', 'current_user', 'updated_at'])
    notify_service.stage_awaiting(flow, flow.production_order)
    return flow


@transaction.atomic
def reject(flow, *, user, remarks):
    """Reject the current stage. One rejection ends the flow.

    Terminal in OMS, not in SAP: the planner cancels the order there, or
    raises a new one that the sync picks up fresh.
    """
    if not (remarks or '').strip():
        raise ProductionFlowError('A reason is required when rejecting.')

    flow = _locked(flow)
    _guard(flow)

    log(flow.production_order, action=LogAction.REJECT, user=user,
        stage_id=flow.current_stage_id, remarks=remarks)

    flow.status = FlowStatus.REJECTED
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    notify_service.decided(flow.production_order, approved=False, actor=user,
                           remarks=remarks)
    return flow


@transaction.atomic
def retire(flow, *, reason):
    """SAP moved the order out of Planned before anyone decided it.

    Not a decision and not a failure — the question stopped being asked. The
    queue stops showing it without anyone having to sweep, and the history
    says what happened.

    This is the exit JSAP lacked, which is why 17 of its orders have sat
    Planned since as far back as October 2025.
    """
    flow = _locked(flow)
    if flow.status != FlowStatus.PENDING:
        return flow

    log(flow.production_order, action=LogAction.OBSOLETE, remarks=reason)
    flow.status = FlowStatus.OBSOLETE
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    return flow


def pending_for(user, *, on_date=None):
    """Flows awaiting THIS user's decision today, stand-ins included.

    Resolved through the engine's replacement mapping rather than trusting
    `current_user`: that column is refreshed when a flow MOVES, so a stage
    whose user changed — or a replacement window that opened — while the flow
    sat still would otherwise be missed.
    """
    from workflow.models import WorkflowStage
    from workflow.services import replacements

    on_date = on_date or replacements.today()
    owners = replacements.configured_users_acting_for(user.pk, on_date)
    if not owners:
        return ProductionOrderFlow.objects.none()

    stage_ids = list(
        WorkflowStage.objects
        .filter(user_id__in=owners, is_active=True)
        .values_list('id', flat=True)
    )
    if not stage_ids:
        return ProductionOrderFlow.objects.none()

    return (
        ProductionOrderFlow.objects
        .filter(status=FlowStatus.PENDING, current_stage_id__in=stage_ids)
        .select_related('production_order', 'current_stage')
        .order_by('-created_at')
    )


def decided_order_ids(user, status=None):
    """Orders this user has approved or rejected, optionally by outcome.

    A user decides one STAGE, not the request: approving stage 1 of three
    leaves the flow pending. So the set is "requests I acted on", narrowed by
    the flow's CURRENT status.
    """
    ids = (ProductionOrderActionLog.objects
           .filter(acted_by=user,
                   action__in=[LogAction.APPROVE, LogAction.REJECT])
           .values_list('production_order_id', flat=True))
    qs = ProductionOrderFlow.objects.filter(production_order_id__in=set(ids))
    if status:
        qs = qs.filter(status=status)
    return set(qs.values_list('production_order_id', flat=True))
