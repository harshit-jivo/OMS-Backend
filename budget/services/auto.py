"""Auto-approval: an item that has waited long enough, within SAP's budget.

When `BudgetSettings.auto_approve_enabled`, an item waiting at a stage for
`auto_approve_hours` is approved on that stage's behalf, but ONLY when:

1. neither the stage's configured user nor today's stand-in is exempt
   (`BudgetSettings.exempt_users` — JSAP hard-coded three ids instead);
2. SAP has a budget for the head covering the draft's month (OBGS/OBGT); and
3. the head's posted spend that month plus this item stays within it.

No budget for the month means NO auto-approval — never "a budget of zero"
(JSAP's `ISNULL(@monthlyAllocated, 0)` stopped every auto-approval from March
2026 while logging ~90,000 "skipped" rows a day). An item that does not
qualify logs nothing; one that does logs AUTO_APPROVE once. The budget is
used only here: approvers are never shown it and never blocked by it.
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from workflow.services.assignments import get_stage_assignment

from budget.models import BudgetItem, BudgetSettings, ItemStatus
from budget.services import flow as flow_service
from budget.services import sap as sap_service

logger = logging.getLogger(__name__)


@dataclass
class AutoResult:
    considered: int = 0
    approved: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def within_budget(budget, spend, amount):
    """`(ok, reason)`: may this amount be auto-approved against the month's budget?"""
    if budget is None:
        return False, 'no SAP budget for the month'
    if spend + amount > budget:
        return False, 'over the month\'s budget'
    return True, ''


def exempt(stage_id, exempt_ids):
    """Whether the stage's configured user or today's stand-in is exempt."""
    assignment = get_stage_assignment(stage_id)
    if assignment is None:
        return True
    return bool({assignment.configured_user_id, assignment.effective_user_id} & set(exempt_ids))


def run(*, now=None, dry_run=False):
    settings_row = BudgetSettings.load()
    result = AutoResult()
    if not settings_row.auto_approve_enabled:
        return result
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings_row.auto_approve_hours)
    exempt_ids = list(settings_row.exempt_users.values_list('id', flat=True))
    due = (BudgetItem.objects.filter(status=ItemStatus.PENDING, waiting_since__lte=cutoff,
                                     company__in=list(sap_service.schemas()))
           .select_related('draft'))
    for item in due:
        result.considered += 1
        if exempt(item.current_stage_id, exempt_ids):
            result.skip('exempt user')
            continue
        day = item.draft.doc_date or now.date()
        try:
            budget = sap_service.month_budget(item.company, item.budget_code, day)
            spend = sap_service.month_spend(item.company, item.budget_code, day) if budget is not None else 0
        except sap_service.SapUnavailable as exc:
            result.errors.append({'item': item.pk, 'error': str(exc)})
            continue
        ok, reason = within_budget(budget, spend, item.amount)
        if not ok:
            result.skip(reason)
            continue
        if dry_run:
            result.approved.append(item.pk)
            continue
        hours = settings_row.auto_approve_hours
        try:
            item, gate = flow_service.approve(
                item, auto=True, remarks=f'Auto-approved after {hours} hours, within the month\'s SAP budget '
                                          f'({spend + item.amount} of {budget}).')
        except flow_service.BudgetFlowError as exc:
            result.errors.append({'item': item.pk, 'error': str(exc)})
            continue
        result.approved.append(item.pk)
        try:
            sap_service.write_item(item, gate, decided_by='oms-auto')
        except sap_service.SapWriteError as exc:
            result.errors.append({'item': item.pk, 'error': f'approved, but SAP write failed: {exc}'})
    return result
