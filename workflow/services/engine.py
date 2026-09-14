"""The engine: start an execution, open stages, approve, reject — plan §7, §10.

What lives here is deliberately narrow. The engine owns START WORKFLOW, OPEN
STAGE, APPROVE, REJECT, COMPLETE STAGE and COMPLETE WORKFLOW. It does NOT own
RESUBMIT: whether an entry may be resubmitted, how often, and what that means
for the business document are module concerns (plan §3.1). When a module
decides to resubmit, it records that in its own history and calls `start()`
again on the SAME flow row — the engine then does ordinary work and has no
idea it was a resubmission.

Consequently there is no `round_number`, no attempt counter, no
`resubmission_count`, and no resubmission API.

Approval model: one stage, one user, one task. One approve completes the
stage; one reject ends the execution. No quorum, no N-of-M, no sibling tasks
to cancel.
"""
import logging

from django.db import transaction
from django.db.models import Max

from workflow.exceptions import (
    InvalidWorkflowAction,
    StageUserUnavailable,
    UnauthorizedWorkflowAction,
    WorkflowAlreadyRunning,
)
from workflow.models import (
    ActionType,
    FlowStatus,
    TaskStatus,
    WorkflowAction,
    WorkflowStage,
    WorkflowTask,
)
from workflow.services import replacements, selection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def _next_action_sequence(module_id, flow_id):
    """Next monotonic history sequence for a flow.

    Computed under the flow row's lock (every caller holds it), so the
    read-then-write is safe. The sequence CONTINUES across successive
    executions of the same flow rather than restarting, which is why no
    attempt counter is needed to order history.
    """
    current = (
        WorkflowAction.objects
        .filter(module_id=module_id, flow_id=flow_id)
        .aggregate(top=Max('sequence'))['top']
    )
    return (current or 0) + 1


def _log(flow, module, *, action, stage=None, user=None, on_behalf_of_id=None,
         remarks='', ctx=None):
    """Append one engine action. Never updates, never deletes.

    `on_behalf_of_id` is set only when a replacement was in effect, so
    `acted_by` + `on_behalf_of` together answer "who acted, and for whom".
    """
    ctx = ctx or {}
    return WorkflowAction.objects.create(
        module=module,
        flow_id=flow.pk,
        sequence=_next_action_sequence(module.pk, flow.pk),
        stage=stage,
        stage_name=stage.name if stage else '',
        action=action,
        acted_by=user,
        acted_by_username=getattr(user, 'username', '') or '',
        on_behalf_of_id=on_behalf_of_id,
        remarks=remarks or '',
        ip_address=ctx.get('ip_address'),
        user_agent=(ctx.get('user_agent') or '')[:400],
    )


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def _stage_at(workflow, sequence):
    return (
        WorkflowStage.objects
        .filter(workflow=workflow, sequence=sequence)
        .select_related('user')
        .first()
    )


def _open_stage(flow, module, stage, *, on_date=None):
    """Point the flow at `stage` and create its single task.

    Raises `StageUserUnavailable` if the effective user cannot act, so an
    unusable stage is reported at the moment it would open rather than
    leaving the document to deadlock there.
    """
    on_date = on_date or replacements.today()
    effective_id = replacements.effective_user_id(stage.user_id, on_date)

    # The configured user is NOT NULL, so the only failure mode left is an
    # account that has been deactivated with no replacement covering today.
    from django.contrib.auth import get_user_model
    if not get_user_model().objects.filter(pk=effective_id, is_active=True).exists():
        raise StageUserUnavailable(
            f'Stage "{stage.name}" resolves to an inactive user.',
            context={'stage': stage.name, 'effective_user_id': effective_id},
        )

    flow.current_stage = stage
    flow.current_sequence = stage.sequence
    flow.status = FlowStatus.PENDING
    flow.lock_version = (flow.lock_version or 0) + 1
    flow.save(update_fields=['current_stage', 'current_sequence', 'status',
                             'lock_version', 'updated_at'])

    # `stage_user` is the CONFIGURED user. The effective actor is resolved by
    # date at read/action time, so the original automatically resumes when a
    # replacement window closes — see services/replacements.py.
    task, _created = WorkflowTask.objects.get_or_create(
        module=module,
        flow_id=flow.pk,
        stage=stage,
        status=TaskStatus.PENDING,
        defaults={'sequence': stage.sequence, 'stage_user_id': stage.user_id},
    )
    return task


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

@transaction.atomic
def start(*, module, flow, document_key, company='', user=None, context=None,
          ctx=None):
    """Begin a workflow execution for a document.

    `flow` is the module's OWN flow row — the module creates it (or reuses the
    existing one) and passes it in. The engine never creates a second flow row
    for a document and never creates one because an entry was resubmitted.

    Must be called inside the module's transaction together with the business
    insert, so that a selection failure rolls the document back too.

    Raises `WorkflowAlreadyRunning`, `WorkflowNotConfigured`,
    `AmbiguousWorkflowSelection`, `InvalidWorkflowConfiguration`,
    `StageUserUnavailable`, or `ConditionExecutionError`.
    """
    # Lock the flow row FIRST. Two concurrent starts then serialise, and the
    # second sees PENDING — the invariant is held by the lock plus the flow
    # table's UNIQUE(document), not by an application check-then-act.
    locked = type(flow).objects.select_for_update().get(pk=flow.pk)
    if locked.status == FlowStatus.PENDING and locked.current_stage_id:
        raise WorkflowAlreadyRunning(
            context={'flow_id': locked.pk, 'module': module.code},
        )

    workflow, matched_query = selection.select_workflow(
        module, document_key, company)

    first_stage = _stage_at(workflow, 1) or workflow.stages.order_by(
        'sequence').first()

    locked.workflow = workflow
    locked.matched_query = matched_query
    locked.company = company or locked.company
    locked.context_snapshot = {
        **(context or {}),
        'selected_workflow': workflow.code,
        'matched_query': matched_query.name,
        'matched_query_id': matched_query.pk,
    }
    locked.save(update_fields=['workflow', 'matched_query', 'company',
                               'context_snapshot', 'updated_at'])

    task = _open_stage(locked, module, first_stage)
    _log(locked, module, action=ActionType.SUBMIT, stage=first_stage,
         user=user, remarks='', ctx=ctx)
    return locked, task


def _authorise(task, user, on_date=None):
    """The caller must be the task's effective actor.

    Authorisation is computed from the CONFIGURED user plus today's
    replacement, so a stand-in can act during their window and the original
    cannot — and the reverse once the window closes. Returns the configured
    user id when the caller is acting on someone's behalf, else None.
    """
    on_date = on_date or replacements.today()
    effective_id = replacements.effective_user_id(task.stage_user_id, on_date)
    if effective_id != user.pk:
        raise UnauthorizedWorkflowAction(
            context={'task_id': task.pk, 'expected_user_id': effective_id},
        )
    return task.stage_user_id if task.stage_user_id != user.pk else None


@transaction.atomic
def approve(*, task_id, user, remarks='', ctx=None):
    """Approve the open task: complete the stage, open the next, or finish.

    One approve completes the stage. There is no approval count and no quorum.
    """
    task = (
        WorkflowTask.objects
        .select_related('module', 'stage', 'stage__workflow')
        .select_for_update()
        .get(pk=task_id)
    )
    if task.status != TaskStatus.PENDING:
        raise InvalidWorkflowAction(
            f'This task is already {task.status.lower()}.',
            context={'task_id': task.pk, 'status': task.status},
        )

    on_behalf_of_id = _authorise(task, user)
    module = task.module
    flow = _locked_flow(module, task.flow_id)

    if flow.status != FlowStatus.PENDING:
        raise InvalidWorkflowAction(
            f'This workflow is already {flow.status.lower()}.',
            context={'flow_id': flow.pk, 'status': flow.status},
        )

    task.status = TaskStatus.APPROVED
    task.save(update_fields=['status', 'updated_at'])

    _log(flow, module, action=ActionType.APPROVE, stage=task.stage, user=user,
         on_behalf_of_id=on_behalf_of_id, remarks=remarks, ctx=ctx)

    next_stage = _stage_at(task.stage.workflow, task.stage.sequence + 1)
    if next_stage is None:
        flow.status = FlowStatus.APPROVED
        flow.current_stage = None
        flow.lock_version = (flow.lock_version or 0) + 1
        flow.save(update_fields=['status', 'current_stage', 'lock_version',
                                 'updated_at'])
        return flow, None

    next_task = _open_stage(flow, module, next_stage)
    return flow, next_task


@transaction.atomic
def reject(*, task_id, user, remarks='', ctx=None):
    """Reject the open task: the workflow execution ends.

    One reject ends the execution. There is no rejection count and no quorum.

    This ends the EXECUTION, not the document's life. What happens next is the
    module's decision — it may leave the entry rejected, or (if its own rules
    allow) let the entry be edited, record RESUBMITTED in its own history, and
    call `start()` again on this same flow row.
    """
    task = (
        WorkflowTask.objects
        .select_related('module', 'stage', 'stage__workflow')
        .select_for_update()
        .get(pk=task_id)
    )
    if task.status != TaskStatus.PENDING:
        raise InvalidWorkflowAction(
            f'This task is already {task.status.lower()}.',
            context={'task_id': task.pk, 'status': task.status},
        )

    on_behalf_of_id = _authorise(task, user)
    module = task.module
    flow = _locked_flow(module, task.flow_id)

    if flow.status != FlowStatus.PENDING:
        raise InvalidWorkflowAction(
            f'This workflow is already {flow.status.lower()}.',
            context={'flow_id': flow.pk, 'status': flow.status},
        )

    task.status = TaskStatus.REJECTED
    task.save(update_fields=['status', 'updated_at'])

    _log(flow, module, action=ActionType.REJECT, stage=task.stage, user=user,
         on_behalf_of_id=on_behalf_of_id, remarks=remarks, ctx=ctx)

    flow.status = FlowStatus.REJECTED
    flow.lock_version = (flow.lock_version or 0) + 1
    flow.save(update_fields=['status', 'lock_version', 'updated_at'])
    return flow, None


@transaction.atomic
def cancel(*, module, flow, user=None, remarks='', ctx=None):
    """Cancel a running execution. Open tasks become CANCELLED."""
    locked = _locked_flow(module, flow.pk)
    if locked.status != FlowStatus.PENDING:
        raise InvalidWorkflowAction(
            f'This workflow is already {locked.status.lower()}.',
            context={'flow_id': locked.pk, 'status': locked.status},
        )
    WorkflowTask.objects.filter(
        module=module, flow_id=locked.pk, status=TaskStatus.PENDING,
    ).update(status=TaskStatus.CANCELLED)

    stage = locked.current_stage
    locked.status = FlowStatus.CANCELLED
    locked.lock_version = (locked.lock_version or 0) + 1
    locked.save(update_fields=['status', 'lock_version', 'updated_at'])
    _log(locked, module, action=ActionType.CANCEL, stage=stage, user=user,
         remarks=remarks, ctx=ctx)
    return locked


# ---------------------------------------------------------------------------
# flow resolution
# ---------------------------------------------------------------------------

def flow_model_for(module):
    """The concrete flow model for a module, via the app registry.

    Resolved lazily from `WorkflowModule.flow_model` so the generic engine
    never imports a business module at module-import time.
    """
    from django.apps import apps
    if not module.flow_model:
        raise InvalidWorkflowAction(
            f'Module "{module.code}" has no flow_model configured.',
            context={'module': module.code},
        )
    app_label, model_name = module.flow_model.split('.', 1)
    return apps.get_model(app_label, model_name)


def _locked_flow(module, flow_id):
    model = flow_model_for(module)
    return model.objects.select_for_update().get(pk=flow_id)


def inbox_for(user, on_date=None):
    """Pending tasks the user currently owns, including as a stand-in.

    One query for the replacement mapping and one for the tasks — no per-row
    replacement lookup (plan §15).
    """
    on_date = on_date or replacements.today()
    owners = replacements.configured_users_acting_for(user.pk, on_date)
    if not owners:
        return WorkflowTask.objects.none()
    return (
        WorkflowTask.objects
        .filter(stage_user_id__in=owners, status=TaskStatus.PENDING)
        .select_related('module', 'stage', 'stage__workflow',
                        'stage__workflow__module', 'stage_user')
        .order_by('-created_at')
    )


def history_for(module, flow_id):
    """Append-only engine history for one flow, in order."""
    return (
        WorkflowAction.objects
        .filter(module=module, flow_id=flow_id)
        .select_related('stage', 'acted_by', 'on_behalf_of')
        .order_by('sequence')
    )
