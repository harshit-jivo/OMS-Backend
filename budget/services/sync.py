"""Taking budget-gated drafts in from SAP, and noticing when they change or leave.

    intake     SAP's waiting drafts  -> new ones become items, routed and opened
    changed    a known draft whose lines differ -> old items SUPERSEDED, its gate
               rows cleared, fresh items raised (rejection is final per VERSION)
    gone       OMS items still pending whose draft SAP no longer has waiting
               (posted or deleted) -> GONE

Noisy on purpose (the PRDO lesson): an unreadable SAP raises; a run that saw
nothing is reported as quiet; every successful read stamps
`BudgetSettings.last_sync[company]`.

Lines JSAP already approved (the gate holds 'A') are not taken in again on a
draft OMS has never seen — at the switch-over those are JSAP's decisions,
imported into OMS's gate table by `budget_gate`.
"""
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from budget.models import (BudgetDraft, BudgetItem, BudgetLine, BudgetSettings, DraftStatus, ItemStatus,
                           LogAction)
from budget.services import flow as flow_service
from budget.services import notify as notify_service
from budget.services import routing
from budget.services import sap as sap_service

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    company: str
    lines_seen: int = 0
    drafts_seen: int = 0
    created: int = 0
    reopened: int = 0
    gone: int = 0
    already_approved: int = 0
    skipped_no_budget: int = 0
    unroutable: list = field(default_factory=list)
    routed: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    #: Items opened by this run (not in a dry run), to announce once per approver.
    opened: list = field(default_factory=list, repr=False)

    @property
    def quiet(self):
        return self.lines_seen == 0 and not self.errors

    def as_dict(self):
        return {k: getattr(self, k) for k in ('company', 'lines_seen', 'drafts_seen', 'created', 'reopened',
                                               'gone', 'already_approved', 'skipped_no_budget',
                                               'unroutable', 'routed', 'errors')}


def fingerprint(rows):
    """What the draft says, as one hash: a later difference means it was changed in SAP."""
    parts = sorted(f"{r['line_num']}|{r['acct_code']}|{r['budget_code']}|{r['amount']}" for r in rows)
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def group(lines):
    """{(obj_type, draft_entry): [lines]}"""
    out = defaultdict(list)
    for line in lines:
        out[(line['obj_type'], line['draft_entry'])].append(line)
    return out


def by_route(rows):
    """{route: [lines]}, and the lines that have no route (no budget code)."""
    routes, unrouted = defaultdict(list), []
    for row in rows:
        route = routing.route_for(row['budget_code'], row['acct_code'])
        (routes[route] if route else unrouted).append(row)
    return routes, unrouted


def _snapshot(draft, first, print_):
    draft.doc_num = first['doc_num']
    draft.doc_date = first['doc_date']
    draft.card_code = first['card_code']
    draft.card_name = first['card_name']
    draft.sap_created_by = first['created_by']
    draft.comments = first['comments']
    draft.fingerprint = print_
    draft.synced_at = timezone.now()


def _raise_items(draft, rows, result):
    """One item per route, its lines, and its flow. Raises BudgetFlowError if one cannot be routed."""
    routes, unrouted = by_route(rows)
    result.skipped_no_budget += len(unrouted)
    for route, lines in routes.items():
        item = BudgetItem.objects.create(
            draft=draft, company=draft.company, route=route, budget_code=lines[0]['budget_code'],
            amount=sum((ln['amount'] for ln in lines), start=0))
        BudgetLine.objects.bulk_create([BudgetLine(
            draft=draft, item=item, line_num=ln['line_num'], vis_order=ln['vis_order'], acct_code=ln['acct_code'],
            acct_name=ln['acct_name'], budget_code=ln['budget_code'], sub_budget_code=ln['sub_budget_code'],
            effect_month=ln['effect_month'], amount=ln['amount'], remarks=ln['remarks']) for ln in lines])
        try:
            flow_service.open_item(item)
        except flow_service.BudgetFlowError as exc:
            raise flow_service.BudgetFlowError(f'{route}: {exc}') from exc
        result.opened.append(item)
        result.routed.append({'draft': draft.draft_entry, 'obj_type': draft.obj_type, 'route': route,
                              'workflow': item.workflow.code if item.workflow else None,
                              'user': getattr(item.current_user, 'username', None)})
    return routes


def _new(company, key, rows, approved, result, dry_run):
    pending = [r for r in rows if (r['obj_type'], r['draft_entry'], r['line_num']) not in approved]
    if not pending:
        result.already_approved += 1
        return
    sid = transaction.savepoint()
    try:
        draft = BudgetDraft(company=company, obj_type=key[0], draft_entry=key[1])
        _snapshot(draft, rows[0], fingerprint(rows))
        draft.save()
        if not _raise_items(draft, pending, result):
            transaction.savepoint_rollback(sid)
            return
    except flow_service.BudgetFlowError as exc:
        transaction.savepoint_rollback(sid)
        result.opened = [i for i in result.opened if i.draft_id != draft.pk]
        result.unroutable.append({'draft': key[1], 'obj_type': key[0], 'reason': str(exc)})
        return
    if dry_run:
        transaction.savepoint_rollback(sid)
    else:
        transaction.savepoint_commit(sid)
    result.created += 1


def _changed(draft, rows, result, dry_run):
    """The draft was edited in SAP: what was decided no longer describes it."""
    if dry_run:
        result.reopened += 1
        return
    sap_service.clear_draft(draft.company, draft.obj_type, draft.draft_entry)
    for item in draft.items.exclude(status__in=[ItemStatus.SUPERSEDED, ItemStatus.GONE]):
        flow_service.retire(item, action=LogAction.SUPERSEDED,
                            reason='The draft was changed in SAP; it comes back for approval as it is now.')
    _snapshot(draft, rows[0], fingerprint(rows))
    draft.status = DraftStatus.PENDING
    draft.save()
    _raise_items(draft, rows, result)
    result.reopened += 1


@transaction.atomic
def _one(company, key, rows, draft, approved, result, dry_run):
    if draft is None:
        _new(company, key, rows, approved, result, dry_run)
    elif draft.status != DraftStatus.GONE and draft.fingerprint != fingerprint(rows):
        _changed(draft, rows, result, dry_run)


def _gone(company, seen, result, dry_run):
    for draft in BudgetDraft.objects.filter(company=company, status=DraftStatus.PENDING):
        if (draft.obj_type, draft.draft_entry) in seen:
            continue
        result.gone += 1
        if dry_run:
            continue
        with transaction.atomic():
            for item in draft.items.filter(status=ItemStatus.PENDING):
                flow_service.retire(item, action=LogAction.GONE,
                                    reason='SAP no longer has this draft waiting: it was posted or deleted.')
            flow_service.refresh_draft_status(draft)
            if not draft.items.filter(status__in=[ItemStatus.APPROVED, ItemStatus.REJECTED]).exists():
                draft.status = DraftStatus.GONE
                draft.save(update_fields=['status', 'updated_at'])


def announce(items):
    """One message per approver for the items a run put at their stage."""
    by_user = defaultdict(list)
    for item in items:
        if item.status == ItemStatus.PENDING and item.current_user_id:
            by_user[item.current_user_id].append(item)
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=list(by_user))}
    for user_id, mine in by_user.items():
        notify_service.new_items(users.get(user_id), mine)


def run(company, *, dry_run=False):
    """One sweep for one company. Raises `SapUnavailable` when SAP cannot be read."""
    result = SyncResult(company=company)
    lines, approved = sap_service.gated_lines(company)
    groups = group(lines)
    result.lines_seen, result.drafts_seen = len(lines), len(groups)
    known = {(d.obj_type, d.draft_entry): d for d in BudgetDraft.objects.filter(company=company)}
    for key, rows in groups.items():
        opened = len(result.opened)
        try:
            _one(company, key, rows, known.get(key), approved, result, dry_run)
        except Exception as exc:  # noqa: BLE001 — one bad draft must not stop the run
            del result.opened[opened:]  # rolled back: nothing of it to announce
            logger.exception('BUDGET: draft %s %s failed', company, key)
            result.errors.append({'draft': key[1], 'obj_type': key[0], 'error': f'{type(exc).__name__}: {exc}'})
    _gone(company, set(groups), result, dry_run)
    if not dry_run:
        announce(result.opened)
        row = BudgetSettings.load()
        row.last_sync = {**(row.last_sync or {}), company: timezone.now().isoformat()}
        row.save(update_fields=['last_sync', 'updated_at'])
    return result
