"""Who gets told what, and when, for budget approval.

Uses the project's notification framework (`notifications.services.dispatcher.
notify`): records join the caller's transaction and push delivery happens
`on_commit`, so a rolled-back decision — or a sync dry run — notifies nobody.
The same push reaches the web app and the OMS mobile app.

Same shape as Production Orders (`production/services/notify.py`), for the same
reason: SAP raised the draft, and its creator is a SAP login, not an OMS user,
so there is no requester to tell.

    BUDGET_AWAITING_APPROVAL  an item reached your stage (it moved on to you)
    BUDGET_NEW_ITEMS          a sync took in items waiting for you: ONE message
                              per approver per run, not one per item — the first
                              run alone opens hundreds
    BUDGET_PENDING_REMINDER   the daily nudge: how many wait for you
    BUDGET_APPROVED /         the chain finished; told to whoever acted earlier
    BUDGET_REJECTED           in it (the actor excepted). One stage: nobody.

Recipients are resolved at send time from the engine (`get_stage_assignment`):
a stage reassignment or a stand-in changes who is told with nothing to update
here. Each message's entity is an item, so a click opens it (the summary ones
point at the oldest waiting item).

A failed notification never fails the decision it reports.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.services.dispatcher import notify

from budget.models import LogAction

logger = logging.getLogger(__name__)

BUDGET_AWAITING_APPROVAL = 'BUDGET_AWAITING_APPROVAL'
BUDGET_NEW_ITEMS = 'BUDGET_NEW_ITEMS'
BUDGET_PENDING_REMINDER = 'BUDGET_PENDING_REMINDER'
BUDGET_APPROVED = 'BUDGET_APPROVED'
BUDGET_REJECTED = 'BUDGET_REJECTED'


def _users(ids):
    return list(get_user_model().objects.filter(pk__in={i for i in ids if i}, is_active=True))


def _rupees(value):
    return f'₹{value:,.2f}'


def describe(item):
    """One line: enough to decide whether to open it."""
    draft = item.draft
    party = f' · {draft.card_name}' if draft.card_name else ''
    return (f'{draft.get_obj_type_display()} {draft.doc_num or draft.draft_entry} · {item.company} · '
            f'{item.budget_code}{party} · {_rupees(item.amount)}')


def _safe(**kwargs):
    try:
        return notify(**kwargs)
    except Exception:  # noqa: BLE001 — delivery must not break a decision
        logger.warning('BUDGET notification %s failed', kwargs.get('event_type'), exc_info=True)
        return None


def stage_awaiting(item):
    """Tell the person who now has to act on `item`."""
    from workflow.services.assignments import get_stage_assignment

    assignment = get_stage_assignment(item.current_stage_id) if item.current_stage_id else None
    if assignment is None:
        return None
    recipients = _users([assignment.effective_user_id])
    if not recipients:
        return None
    return _safe(event_type=BUDGET_AWAITING_APPROVAL, title='Budget approval waiting for you',
                 message=f'{describe(item)} — {assignment.stage_name}', recipients=recipients,
                 entity=item, actor=None)


def new_items(user, items):
    """One message to `user` for the items a sync just put at their stage."""
    items = sorted(items, key=lambda i: i.pk)
    if not items or user is None or not user.is_active:
        return None
    if len(items) == 1:
        title, message = 'Budget approval waiting for you', describe(items[0])
    else:
        total = sum((i.amount for i in items), start=0)
        title = f'{len(items)} budget approvals waiting for you'
        message = f'{len(items)} new items from SAP, {_rupees(total)} in all.'
    return _safe(event_type=BUDGET_NEW_ITEMS, title=title, message=message, recipients=[user],
                 entity=items[0], actor=None)


def pending_reminder(user, items):
    """The daily nudge: everything waiting for `user`, pointing at the oldest."""
    if not items or user is None or not user.is_active:
        return None
    oldest = min(items, key=lambda i: (i.waiting_since is None, i.waiting_since, i.pk))
    count = len(items)
    title = f'{count} budget approval{"s" if count != 1 else ""} pending'
    return _safe(event_type=BUDGET_PENDING_REMINDER, title=title,
                 message=f'Oldest: {describe(oldest)}.', recipients=[user], entity=oldest, actor=None)


def decided(item, *, approved, actor, remarks=''):
    """Tell everyone who acted earlier on this item what became of it, the actor excepted."""
    earlier = (item.action_logs.filter(action__in=(LogAction.APPROVE, LogAction.REJECT))
               .exclude(acted_by=None).values_list('acted_by_id', flat=True))
    actor_id = getattr(actor, 'pk', None)
    recipients = _users({uid for uid in earlier if uid != actor_id})
    if not recipients:
        return None
    if approved:
        title, message = 'Budget approved', f'{describe(item)} — approved; SAP may now post it.'
    else:
        title, message = 'Budget rejected', f'{describe(item)} — {remarks or "no reason given"}.'
    return _safe(event_type=BUDGET_APPROVED if approved else BUDGET_REJECTED, title=title, message=message,
                 recipients=recipients, entity=item, actor=actor)
