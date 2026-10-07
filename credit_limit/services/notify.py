"""Who is told what — same framework and rules as `backdate.services.notify`.

Recipients are resolved from the engine at send time, and a failed
notification never fails the approval.

    approver   CREDIT_LIMIT_AWAITING_APPROVAL   a request reached their stage
    requester  CREDIT_LIMIT_APPROVED            the new limit is set in SAP
    requester  CREDIT_LIMIT_REJECTED            with the approver's reason

A multi-party submission tells each approver ONCE, however many of its
requests landed on them; the entity is then left empty so a click opens the
approval queue rather than one arbitrary request of the batch.

`company` is NOT passed to the dispatcher: it means the recipient's
`users.Company` (an organisational master), not OIL/BEVERAGES/MART, and
passing the code string made every send raise.
"""
import logging
from collections import defaultdict

from django.contrib.auth import get_user_model

from notifications.services.dispatcher import notify

logger = logging.getLogger(__name__)

CREDIT_LIMIT_AWAITING_APPROVAL = 'CREDIT_LIMIT_AWAITING_APPROVAL'
CREDIT_LIMIT_APPROVED = 'CREDIT_LIMIT_APPROVED'
CREDIT_LIMIT_REJECTED = 'CREDIT_LIMIT_REJECTED'


def _amount(value):
    return f'{value:,.0f}' if value == int(value) else f'{value:,.2f}'


def _describe(request):
    return (f'{request.card_name or request.card_code} ({request.company}) — '
            f'limit {_amount(request.current_credit_limit)} → '
            f'{_amount(request.new_credit_limit)}')


def _send(**kwargs):
    try:
        return notify(**kwargs)
    except Exception:  # noqa: BLE001 — delivery must not break approval
        logger.warning('CL notification failed', exc_info=True)
        return None


def _approver(flow):
    """`(user, stage_name)` for whoever must act on the flow now, or None."""
    from workflow.services.assignments import get_stage_assignment

    assignment = get_stage_assignment(flow.current_stage_id)
    user = (get_user_model().objects.filter(pk=assignment.effective_user_id)
            .first() if assignment else None)
    if user is None:
        logger.warning('CL: stage %s has no user; nobody notified',
                       flow.current_stage_id)
        return None
    return user, assignment.stage_name


def stage_awaiting(flow):
    """Tell the person who now has to act on one request."""
    found = _approver(flow)
    if found is None:
        return None
    user, stage_name = found
    request = flow.request
    return _send(
        event_type=CREDIT_LIMIT_AWAITING_APPROVAL,
        title='Credit limit request awaiting your approval',
        message=f'{_describe(request)} · {stage_name}',
        recipients=[user], entity=request, actor=request.created_by)


def submitted(flows):
    """Tell each approver about a new submission — once per approver."""
    by_user = defaultdict(list)
    users = {}
    for flow in flows:
        found = _approver(flow)
        if found is not None:
            users[found[0].pk] = found[0]
            by_user[found[0].pk].append(flow)

    for user_pk, mine in by_user.items():
        if len(mine) == 1:
            stage_awaiting(mine[0])
            continue
        first = mine[0].request
        names = ', '.join(f.request.card_name or f.request.card_code
                          for f in mine[:3])
        more = f' and {len(mine) - 3} more' if len(mine) > 3 else ''
        _send(
            event_type=CREDIT_LIMIT_AWAITING_APPROVAL,
            title=f'{len(mine)} credit limit requests awaiting your approval',
            message=f'{first.company}: {names}{more}',
            recipients=[users[user_pk]], entity=None,
            actor=first.created_by)


def decided(request, *, approved, actor, remarks=''):
    """Tell the requester the outcome."""
    if approved:
        title = 'Credit limit approved'
        message = f'{_describe(request)}. The new limit is set in SAP.'
    else:
        title = 'Credit limit request rejected'
        message = f'{_describe(request)}. Reason: {remarks or "none given"}.'
    return _send(
        event_type=(CREDIT_LIMIT_APPROVED if approved
                    else CREDIT_LIMIT_REJECTED),
        title=title, message=message, recipients=[request.created_by],
        entity=request, actor=actor)
