"""Who is told what — same framework and rules as `credit_limit.services.notify`.

    the stage's user  ADVANCE_PAYMENT_AWAITING_APPROVAL  a request reached their desk:
                                                        raised, approved on from the
                                                        stage before, resubmitted, or
                                                        sent back to Payment (with why)
    the creator       ADVANCE_PAYMENT_COMPLETED          posted to SAP: the money may go
    the creator       ADVANCE_PAYMENT_REJECTED           with the reason
    the creator       ADVANCE_PAYMENT_RETURNED           to edit and resubmit, with why
    the recipient     ADVANCE_PAYMENT_ASSIGNED           bills / POs sent to raise from

The recipient is the stage's user TODAY (`flow.current_user_id`, replacements
applied when the flow moved), resolved when it moves. Nobody is told about
their own action. Sent once the decision has COMMITTED, and a failed
notification never fails the decision: the request's state is what matters.
"""
import functools
import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction

from notifications.services.dispatcher import notify

logger = logging.getLogger(__name__)

AWAITING = 'ADVANCE_PAYMENT_AWAITING_APPROVAL'
COMPLETED = 'ADVANCE_PAYMENT_COMPLETED'
REJECTED = 'ADVANCE_PAYMENT_REJECTED'
RETURNED = 'ADVANCE_PAYMENT_RETURNED'
ASSIGNED = 'ADVANCE_PAYMENT_ASSIGNED'


def _amount(value):
    value = Decimal(str(value or 0))
    return f'₹{value:,.0f}' if value == value.to_integral_value() else f'₹{value:,.2f}'


def _describe(advance):
    return f'{advance.request_no} · {advance.partner_name} · {_amount(advance.amount)} ({advance.company})'


def _never_fails(fn):
    """A notification is a side effect: building one must not fail the decision either."""
    @functools.wraps(fn)
    def safe(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning('ADVANCE notification %s not sent', fn.__name__, exc_info=True)
            return None
    return safe


def _send(**kwargs):
    try:
        notify(**kwargs)
    except Exception:  # noqa: BLE001 — delivery must not break the decision
        logger.warning('ADVANCE notification failed', exc_info=True)


def _later(**kwargs):
    """Send once the surrounding transaction commits: a rolled-back decision tells nobody."""
    recipients = [u for u in kwargs.pop('recipients') if u is not None]
    actor = kwargs.get('actor')
    recipients = [u for u in recipients if actor is None or u.pk != actor.pk]
    if recipients:
        transaction.on_commit(lambda: _send(recipients=recipients, **kwargs))


def _user(pk):
    return get_user_model().objects.filter(pk=pk, is_active=True).first() if pk else None


@_never_fails
def awaiting(flow, *, actor, note=''):
    """Tell the user of the stage the request now waits at."""
    advance = flow.request
    stage = flow.current_stage.name if flow.current_stage_id else ''
    message = f'{_describe(advance)} · {stage}' + (f' · {note}' if note else '')
    _later(event_type=AWAITING, title='Payment request awaiting you', message=message,
           recipients=[_user(flow.current_user_id)], entity=advance, actor=actor)


@_never_fails
def completed(advance, *, actor, posted=False):
    from advance_payment.services import voucher as voucher_service

    live = voucher_service.live(advance) if posted else None
    doc_num = getattr(live, 'sap_doc_num', None)
    what = 'A/P invoice' if getattr(live, 'sap_object', '') == 'PURCHASE_INVOICE' else 'payment'
    tail = f'. Posted to SAP as {what} {doc_num}.' if doc_num else '. Approved and posted to SAP.'
    _later(event_type=COMPLETED, title='Payment request completed', message=_describe(advance) + tail,
           recipients=[advance.created_by], entity=advance, actor=actor)


@_never_fails
def rejected(advance, *, actor, remarks):
    _later(event_type=REJECTED, title='Payment request rejected',
           message=f'{_describe(advance)}. Reason: {remarks or "none given"}.',
           recipients=[advance.created_by], entity=advance, actor=actor)


@_never_fails
def returned(advance, *, actor, remarks):
    _later(event_type=RETURNED, title='Payment request returned to you',
           message=f'{_describe(advance)}. Edit and resubmit: {remarks or "no reason given"}.',
           recipients=[advance.created_by], entity=advance, actor=actor)


@_never_fails
def assigned(made, *, actor):
    """Bills / POs sent to someone to raise payment requests from: one notification per send."""
    if not made:
        return
    first = made[0]
    names = ', '.join(f'{a.get_kind_display()} {a.sap_doc_num}' for a in made[:3])
    more = f' and {len(made) - 3} more' if len(made) > 3 else ''
    title = ('A bill or PO was sent to you' if len(made) == 1
             else f'{len(made)} bills / POs were sent to you')
    _later(event_type=ASSIGNED, title=title,
           message=f'{first.company}: {names}{more} · {first.card_name}. Raise the payment request.',
           recipients=[first.assigned_to], entity=None, actor=actor)
