"""Who gets told what, and when, for Production Orders.

Uses the project's existing notification framework
(`notifications.services.dispatcher.notify`) — records participate in the
caller's transaction and push delivery is scheduled `on_commit`, so a rolled
back decision notifies nobody. No second transport is built here.

RECIPIENTS COME FROM THE ENGINE, NOT FROM A STORED ASSIGNEE
-----------------------------------------------------------
The person to notify is the stage's CURRENT effective user, resolved at send
time. A stage reassignment or an active replacement therefore changes who is
told, with nothing updated in this module.

THERE IS NO REQUESTER, AND THAT CHANGES WHO HEARS THE OUTCOME
-------------------------------------------------------------
BackDate tells the person who raised the request. PRDO has nobody to tell:
SAP is the point of origin, and `sap_created_by` is a snapshot of a SAP login,
not an OMS identity — there is no user row to notify and inventing a mapping
between the two namespaces is the mistake PRDO_DESIGN §3.1 exists to avoid.

So the outcome goes to the people who already ACTED on this order, minus
whoever just acted. In today's single-stage OIL configuration that is nobody,
which is correct: telling an approver about their own decision is noise. The
moment a second stage is configured, stage 1 learns what stage 2 did — which
is the only person who has been left guessing under JSAP.

WHAT IS DELIBERATELY NOT SENT
-----------------------------
* `OBSOLETE` — SAP moved the order out of Planned before anyone decided it.
  Nobody refused anything and nothing is owed to anyone; the queue simply
  stops showing it.
* A failed SAP write-back. It is arguably the most urgent event in the module
  — an order everyone believes is released is still held by SAP — but who
  should be paged for an integration failure is an operations decision, not
  one to make silently inside an approval path.

Notification failures never fail the business operation: an approval that
succeeded must not be reported as failed because a push did not go out.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.services.dispatcher import notify

from production.models import LogAction

logger = logging.getLogger(__name__)

#: Event names this module owns. The framework only validates the shape
#: (`^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$`); the meaning is ours.
#: An order has reached a stage and somebody must act. Fired when the flow
#: opens AND on every advance, because "it is now yours" is the same fact
#: either time — a separate SUBMITTED event would tell the first approver the
#: same thing twice under two names.
PRDO_AWAITING_APPROVAL = 'PRDO_AWAITING_APPROVAL'
#: The chain finished. Sent to whoever acted earlier in it.
PRDO_APPROVED = 'PRDO_APPROVED'
PRDO_REJECTED = 'PRDO_REJECTED'


def _users(ids):
    User = get_user_model()
    return list(User.objects.filter(pk__in={i for i in ids if i}))


def _describe(order):
    """Enough to decide whether to open it, in one line."""
    return (f'#{order.sap_doc_num or order.sap_doc_entry} · {order.company} · '
            f'{order.item_code} · {order.planned_qty} pcs')


def _safe(fn, *args, **kwargs):
    """Notification is a side effect, never a reason to fail the operation."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — delivery must not break a decision
        logger.warning('PRDO notification failed', exc_info=True)
        return None


def stage_awaiting(flow, order):
    """Tell the person who now has to act.

    Resolved from the stage's current effective user — see module docstring.
    The stage NAME is read from the engine rather than stored on the flow, so
    a renamed stage reads correctly here too.
    """
    from workflow.services.assignments import get_stage_assignment

    assignment = get_stage_assignment(flow.current_stage_id)
    if assignment is None:
        logger.warning('PRDO: stage %s has no assignment; nobody notified',
                       flow.current_stage_id)
        return None

    recipients = _users([assignment.effective_user_id])
    if not recipients:
        return None

    return _safe(
        notify,
        event_type=PRDO_AWAITING_APPROVAL,
        title='Production order awaiting your approval',
        message=f'{_describe(order)} — {assignment.stage_name}',
        recipients=recipients,
        entity=order,
        # No actor: SAP raised this, and the acting OMS user (if any) is the
        # previous approver, who did not address this order to anybody.
        actor=None,
    )


def decided(order, *, approved, actor, remarks=''):
    """Tell everyone who acted earlier in this flow what became of it.

    `actor` is excluded — nobody needs telling what they just did. With one
    stage configured that empties the list, and sending nothing is the right
    answer rather than a gap to fill.
    """
    earlier = (order.action_logs
               .filter(action__in=(LogAction.APPROVE, LogAction.REJECT))
               .exclude(acted_by=None)
               .values_list('acted_by_id', flat=True))

    actor_id = getattr(actor, 'pk', None)
    recipients = _users({uid for uid in earlier if uid != actor_id})
    if not recipients:
        return None

    if approved:
        title = 'Production order approved'
        message = f'{_describe(order)} — approved and sent to SAP.'
    else:
        title = 'Production order rejected'
        message = f'{_describe(order)} — {remarks or "no reason given"}.'

    return _safe(
        notify,
        event_type=PRDO_APPROVED if approved else PRDO_REJECTED,
        title=title,
        message=message,
        recipients=recipients,
        entity=order,
        actor=actor,
    )
