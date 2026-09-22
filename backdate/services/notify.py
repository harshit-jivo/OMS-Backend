"""Who gets told what, and when, for BackDate.

Uses the project's existing notification framework
(`notifications.services.dispatcher.notify`) — records participate in the
caller's transaction and push delivery is scheduled `on_commit`, so a rolled
back approval notifies nobody. No second transport is built here.

RECIPIENTS COME FROM THE ENGINE, NOT FROM A STORED ASSIGNEE
-----------------------------------------------------------
The person to notify is the stage's CURRENT effective user, resolved at send
time. A stage reassignment or an active replacement therefore changes who is
told, with nothing updated in this module.

WHAT JSAP DID NOT DO
--------------------
JSAP sent nothing at all on rejection — a requester was never told their
request had been refused, by any channel. That is fixed here.

It also had a scheduled job that pushed a pending count to EVERY active user,
unfiltered by whether they approve BackDate at all. That is not migrated; it
is noise, and the approval queue already answers the question.

Notification failures never fail the business operation: an approval that
succeeded must not be reported as failed because a push did not go out.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.services.dispatcher import notify

logger = logging.getLogger(__name__)

#: Event names this module owns. The framework only validates the shape
#: (`^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$`); the meaning is ours.
#: A request has reached a stage and somebody must act. Fired on submission
#: AND on every advance, because "it is now yours" is the same fact either
#: time — there is no separate SUBMITTED event, which would have told the
#: first approver the same thing twice under two names.
BACKDATE_AWAITING_APPROVAL = 'BACKDATE_AWAITING_APPROVAL'
#: The chain finished. Sent to the REQUESTER, not to the approvers.
BACKDATE_APPROVED = 'BACKDATE_APPROVED'
BACKDATE_REJECTED = 'BACKDATE_REJECTED'


def _users(ids):
    User = get_user_model()
    return list(User.objects.filter(pk__in=[i for i in ids if i]))


def _describe(request_obj):
    """One line naming the request, for a notification's subtitle.

    `company_label`, not `company`: a request covering two companies stores
    `OIL,MART` and reads `Oil, Mart`. ONE notification either way — the
    approval is one decision on one request, and the SAP fan-out after final
    approval is not something an approver is notified about twice.
    """
    return (f'{request_obj.sap_username} · {request_obj.company_label} · '
            f'{request_obj.from_date} to {request_obj.to_date}')


def _safe(fn, *args, **kwargs):
    """Notification is a side effect, never a reason to fail the operation."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — delivery must not break approval
        logger.warning('BKDT notification failed', exc_info=True)
        return None


def stage_awaiting(flow, request_obj):
    """Tell the person who now has to act.

    Resolved from the stage's current effective user — see module docstring.
    The stage NAME is read from the engine rather than stored on the flow, so
    a renamed stage reads correctly here too.
    """
    from workflow.services.assignments import get_stage_assignment

    assignment = get_stage_assignment(flow.current_stage_id)
    if assignment is None:
        logger.warning('BKDT: stage %s has no assignment; nobody notified',
                       flow.current_stage_id)
        return None

    recipients = _users([assignment.effective_user_id])
    if not recipients:
        return None

    return _safe(
        notify,
        event_type=BACKDATE_AWAITING_APPROVAL,
        title='BackDate request awaiting your approval',
        message=f'{_describe(request_obj)} — {assignment.stage_name}',
        recipients=recipients,
        entity=request_obj,
        actor=request_obj.created_by,
    )


def decided(request_obj, *, approved, actor, remarks=''):
    """Tell the REQUESTER what happened. JSAP never did this on rejection."""
    recipients = _users([request_obj.created_by_id])
    if not recipients:
        return None

    if approved:
        title = 'BackDate request approved'
        message = f'{_describe(request_obj)} — rights granted in SAP.'
    else:
        title = 'BackDate request rejected'
        message = f'{_describe(request_obj)} — {remarks or "no reason given"}.'

    return _safe(
        notify,
        event_type=BACKDATE_APPROVED if approved else BACKDATE_REJECTED,
        title=title,
        message=message,
        recipients=recipients,
        entity=request_obj,
        actor=actor,
    )
