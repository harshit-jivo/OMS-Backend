"""Who is told what — same framework and rules as `backdate.services.notify`.

Recipients are resolved from the engine at send time, and a failed
notification never fails the approval.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.services.dispatcher import notify

logger = logging.getLogger(__name__)

CREDIT_LIMIT_AWAITING_APPROVAL = 'CREDIT_LIMIT_AWAITING_APPROVAL'
CREDIT_LIMIT_APPROVED = 'CREDIT_LIMIT_APPROVED'
CREDIT_LIMIT_REJECTED = 'CREDIT_LIMIT_REJECTED'


def _describe(request):
    return (f'{request.card_name or request.card_code} · {request.company} · '
            f'new limit {request.new_credit_limit:,.2f}')


def _send(**kwargs):
    try:
        return notify(**kwargs)
    except Exception:  # noqa: BLE001 — delivery must not break approval
        logger.warning('CL notification failed', exc_info=True)
        return None


def stage_awaiting(flow):
    from workflow.services.assignments import get_stage_assignment

    assignment = get_stage_assignment(flow.current_stage_id)
    user = (get_user_model().objects.filter(pk=assignment.effective_user_id)
            .first() if assignment else None)
    if user is None:
        logger.warning('CL: stage %s has no user; nobody notified',
                       flow.current_stage_id)
        return None
    request = flow.request
    return _send(
        event_type=CREDIT_LIMIT_AWAITING_APPROVAL,
        title='Credit limit request awaiting your approval',
        message=f'{_describe(request)} — {assignment.stage_name}',
        recipients=[user], entity=request, company=request.company,
        actor=request.created_by)


def decided(request, *, approved, actor, remarks=''):
    if approved:
        title = 'Credit limit request approved'
        message = f'{_describe(request)} — limit set in SAP.'
    else:
        title = 'Credit limit request rejected'
        message = f'{_describe(request)} — {remarks or "no reason given"}.'
    return _send(
        event_type=(CREDIT_LIMIT_APPROVED if approved
                    else CREDIT_LIMIT_REJECTED),
        title=title, message=message, recipients=[request.created_by],
        entity=request, company=request.company, actor=actor)
