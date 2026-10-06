"""The BUDGET flow: open an item, approve, reject, retire.

Same split as PRDO (`production/services/flow.py`): the Workflow Engine picks
the workflow and says who sits on each stage; this module opens the item at
its first stage, moves it, keeps the history, and writes SAP.

Every stage is an approval. There is no Payment / Audit / Final here — JSAP's
budget templates were all one or two approval stages, and the last approval is
what lets SAP post the draft.

* Rejection is final for this version of the draft. A draft changed in SAP
  comes back as fresh items (`sync`), the way out JSAP never had.
* One approve completes a stage; one reject ends the item.
* `version` refuses a decision made on a stale screen.
"""
import logging

from django.db import transaction
from django.utils import timezone

from workflow.exceptions import WorkflowError
from workflow.services import selection
from workflow.services.assignments import get_stage_assignment

from budget.models import BudgetActionLog, BudgetItem, DraftStatus, ItemStatus, LogAction

logger = logging.getLogger(__name__)

MODULE_CODE = 'BUDGET'


class BudgetFlowError(Exception):
    """A business rule refused the operation. Carries a safe message."""


def log(item, *, action, user=None, stage_id=None, remarks='', action_data=None):
    return BudgetActionLog.objects.create(item=item, action=action, acted_by=user, stage_id=stage_id,
                                          remarks=remarks or '', action_data=action_data)


def effective_user_id(stage_id):
    """Who acts on `stage_id` today, replacements applied. The one resolution point."""
    if not stage_id:
        return None
    assignment = get_stage_assignment(stage_id)
    return assignment.effective_user_id if assignment else None


def _point_at(item, stage_id):
    item.current_stage_id = stage_id
    item.current_user_id = effective_user_id(stage_id)
    item.waiting_since = timezone.now() if stage_id else None
    item.version += 1


def open_item(item):
    """Select the item's workflow and open its first stage. Raises BudgetFlowError."""
    try:
        chosen = selection.select_for_module(module_code=MODULE_CODE, document_id=item.pk,
                                             company=item.company)
    except WorkflowError as exc:
        raise BudgetFlowError(str(exc)) from exc
    if not chosen.stages:
        raise BudgetFlowError(f'Workflow "{chosen.workflow.code}" has no active stages.')
    item.workflow = chosen.workflow
    item.total_stage = len(chosen.stages)
    _point_at(item, chosen.stages[0].id)
    item.save()
    log(item, action=LogAction.SYNC, remarks=f'Taken in from SAP and routed to {chosen.workflow.code}.')
    return item


def _locked(item):
    # The item row alone, no join: `of=('self',)` would render
    # `FOR UPDATE OF "budget"."budget_item"`, and Postgres refuses a
    # schema-qualified name there. Draft and stage load lazily, unlocked.
    return BudgetItem.objects.select_for_update().get(pk=item.pk)


def _guard(item, version):
    if item.status != ItemStatus.PENDING:
        raise BudgetFlowError(f'This is already {ItemStatus(item.status).label.lower()}.')
    if not item.current_stage_id:
        raise BudgetFlowError('This is not waiting at any stage.')
    if version not in (None, '') and int(version) != item.version:
        raise BudgetFlowError('It changed since you opened it. Reload it and try again.')


def refresh_draft_status(draft):
    """APPROVED when every live item is; REJECTED when any is; else PENDING."""
    live = [i.status for i in draft.items.exclude(status__in=[ItemStatus.SUPERSEDED, ItemStatus.GONE])]
    if not live:
        return draft
    if ItemStatus.REJECTED in live:
        status = DraftStatus.REJECTED
    elif all(s == ItemStatus.APPROVED for s in live):
        status = DraftStatus.APPROVED
    else:
        status = DraftStatus.PENDING
    if draft.status != status:
        draft.status = status
        draft.save(update_fields=['status', 'updated_at'])
    return draft


@transaction.atomic
def approve(item, *, user=None, remarks='', version=None, auto=False):
    """Approve the current stage: move on, or complete the item. Returns `(item, gate_status)`.

    `gate_status` is what the caller writes to SAP after the commit: 'A' when
    the item is now approved, 'V' when it moved to the next stage.
    """
    item = _locked(item)
    _guard(item, version)
    stage_id = item.current_stage_id
    log(item, action=LogAction.AUTO_APPROVE if auto else LogAction.APPROVE,
        user=None if auto else user, stage_id=stage_id, remarks=remarks)
    # The stages after this one as configured NOW: one deactivated mid-flight
    # is skipped rather than leaving the item stuck on it.
    done = item.current_stage.sequence if item.current_stage else 0
    following = [s for s in selection.stages_for(item.workflow) if s.sequence > done]
    if following:
        _point_at(item, following[0].id)
        item.save()
        return item, 'V'
    item.status = ItemStatus.APPROVED
    _point_at(item, None)
    item.save()
    refresh_draft_status(item.draft)
    return item, 'A'


@transaction.atomic
def reject(item, *, user, remarks, version=None):
    """Reject: final for this version of the draft."""
    if not (remarks or '').strip():
        raise BudgetFlowError('Say why, in the remarks, when rejecting.')
    item = _locked(item)
    _guard(item, version)
    log(item, action=LogAction.REJECT, user=user, stage_id=item.current_stage_id, remarks=remarks)
    item.status = ItemStatus.REJECTED
    _point_at(item, None)
    item.save()
    refresh_draft_status(item.draft)
    return item


def retire(item, *, action, reason):
    """Close an open item without a decision: GONE (left SAP) or SUPERSEDED (changed in SAP)."""
    status = ItemStatus.GONE if action == LogAction.GONE else ItemStatus.SUPERSEDED
    if item.status == ItemStatus.PENDING or status == ItemStatus.SUPERSEDED:
        log(item, action=action, remarks=reason)
        item.status = status
        _point_at(item, None)
        item.save()
    return item


def pending_for(user, *, on_date=None):
    """Items waiting for THIS user today, stand-ins included."""
    from workflow.models import WorkflowStage
    from workflow.services import replacements

    owners = replacements.configured_users_acting_for(user.pk, on_date or replacements.today())
    if not owners:
        return BudgetItem.objects.none()
    stage_ids = list(WorkflowStage.objects.filter(user_id__in=owners, is_active=True,
                                                  workflow__module__code=MODULE_CODE)
                     .values_list('id', flat=True))
    return (BudgetItem.objects.filter(status=ItemStatus.PENDING, current_stage_id__in=stage_ids)
            .select_related('draft', 'current_stage', 'workflow').order_by('-created_at'))


def decided_item_ids(user):
    return set(BudgetActionLog.objects.filter(acted_by=user, action__in=[LogAction.APPROVE, LogAction.REJECT])
               .values_list('item_id', flat=True))


def may_act_on(user, item):
    """`(allowed, reason)`: the item waits at a stage that is this user's today."""
    if item.status != ItemStatus.PENDING:
        return False, f'This is already {ItemStatus(item.status).label.lower()}.'
    if effective_user_id(item.current_stage_id) != user.pk:
        return False, 'This is not waiting for your decision.'
    return True, ''
