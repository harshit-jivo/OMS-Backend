"""Flow engine for the document tracker.

All stage-movement rules live here so the views stay thin and the logic is
testable in isolation. The model of record is one `StageEvent` per stage
*visit*: a row is opened when the invoice arrives at a stage (RECEIVE) and
closed when the handler dispositions it (ADVANCE / RETURN), stamping
`exited_at` and `days_spent`. A RETURN or a later re-entry opens a fresh
visit, so an invoice that bounces has multiple visits to the same stage — and
each visit's dwell time is captured independently for reporting.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Invoice, PaymentDetail, Stage, StageEvent, UserStageAccess

# Cut-off after which an arriving invoice is flagged "late received".
LATE_HOUR = 18  # 6 PM

# Stage statuses that force the invoice back a step.
RETURN_STATUSES = {'RETURN', 'REJECTED'}
# Statuses that keep the invoice where it is (annotation only).
HOLD_STATUSES = {'HOLD'}
# Statuses that require a written reason.
REASON_REQUIRED_STATUSES = {'HOLD', 'DEBIT', 'RETURN', 'REJECTED'}

# Stages that only apply to certain invoices. Bilty/GRPO is a Transport-only
# desk — non-Transport invoices skip it and go straight to Pre-Audit.
TRANSPORT_ONLY_STAGE_CODES = {'bilty_grpo'}
# Category names (normalised) that don't require a hold amount on a partial hold.
NO_HOLD_AMOUNT_CATEGORIES = {'rm-pm', 'pm-pm', 'pm/pm'}


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def accessible_stage_ids(user):
    """Stage ids this user may act on. Superusers get everything."""
    if user.is_superuser:
        return set(Stage.objects.values_list('id', flat=True))
    return set(
        UserStageAccess.objects
        .filter(user=user, is_active=True)
        .values_list('stage_id', flat=True)
    )


def can_act(user, invoice):
    """A user may act on an invoice if it sits at a stage they're mapped to,
    or (for the entry stage) they created it."""
    if user.is_superuser:
        return True
    stage = invoice.current_stage
    if stage.id in accessible_stage_ids(user):
        return True
    return stage.code == 'entry' and invoice.created_by_id == user.id


def stage_recipients(stage):
    """Active TRACKER users mapped to `stage` who have an email address — the
    people to notify when an invoice sits there too long.

    Only the three tracker sub-roles (tracker_admin / tracker_entry /
    tracker_user) are mailed — never plain OMS users or superusers."""
    from django.contrib.auth import get_user_model
    from .permissions import ROLE_PAGE_MAP
    User = get_user_model()
    tracker_roles = set(ROLE_PAGE_MAP.keys())
    return list(
        User.objects
        .filter(tracker_stage_access__stage=stage,
                tracker_stage_access__is_active=True,
                is_active=True,
                role__name__in=tracker_roles)
        .exclude(email__isnull=True).exclude(email='')
        .distinct()
    )


def is_full_hold(invoice):
    """True if the invoice's CURRENT stage visit carries a FULL hold note.

    A full hold keeps the invoice in place (partial holds advance it), so the
    presence of a FULL hold event on this visit means it's parked on purpose."""
    return StageEvent.objects.filter(
        invoice=invoice,
        stage=invoice.current_stage,
        entered_at=invoice.current_stage_entered_at,
        hold_type=StageEvent.HoldType.FULL,
    ).exists()


def stuck_visits(now=None):
    """Every in-progress invoice sitting at its stage beyond that stage's
    threshold. Returns a list of (invoice, stage, days_stuck)."""
    now = now or timezone.now()
    out = []
    for inv in (Invoice.objects
                .filter(status=Invoice.Status.IN_PROGRESS)
                .select_related('current_stage')):
        days = days_at_stage(inv, now)
        if days > inv.current_stage.threshold_days:
            out.append((inv, inv.current_stage, days))
    return out


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _is_late(dt):
    return timezone.localtime(dt).hour >= LATE_HOUR


def _days_between(start, end):
    seconds = (end - start).total_seconds()
    return Decimal(seconds / 86400).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def days_at_stage(invoice, now=None):
    """Live dwell time (in days) at the invoice's current stage."""
    now = now or timezone.now()
    return _days_between(invoice.current_stage_entered_at, now)


def is_overdue(invoice, now=None):
    return days_at_stage(invoice, now) > Decimal(invoice.current_stage.threshold_days)


def _category_name(invoice):
    return (getattr(invoice.category, 'name', '') or '').strip().lower()


def _is_transport(invoice):
    return _category_name(invoice) == 'transport'


def stage_route(invoice):
    """The ordered stages this invoice actually travels through.

    Non-Transport invoices skip the Bilty/GRPO desk and go Entry -> Pre-Audit.
    """
    stages = list(Stage.objects.filter(is_active=True).order_by('order'))
    if _is_transport(invoice):
        return stages
    return [s for s in stages if s.code not in TRANSPORT_ONLY_STAGE_CODES]


def _route_neighbour(invoice, step):
    """Next (+1) or previous (-1) stage along this invoice's route."""
    route = stage_route(invoice)
    codes = [s.code for s in route]
    try:
        idx = codes.index(invoice.current_stage.code)
    except ValueError:
        return None
    nxt = idx + step
    return route[nxt] if 0 <= nxt < len(route) else None


def _open_event(invoice):
    """The still-open visit row for the invoice's current stage."""
    return (
        invoice.events
        .filter(stage=invoice.current_stage, exited_at__isnull=True)
        .order_by('-entered_at')
        .first()
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
@transaction.atomic
def create_invoice(*, created_by, **fields):
    """Create an invoice at the entry stage and open its first visit row."""
    entry = Stage.objects.get(code='entry')
    now = timezone.now()
    invoice = Invoice.objects.create(
        created_by=created_by,
        current_stage=entry,
        current_stage_entered_at=now,
        is_locked=False,
        status=Invoice.Status.IN_PROGRESS,
        **fields,
    )
    StageEvent.objects.create(
        invoice=invoice, stage=entry,
        event_type=StageEvent.EventType.RECEIVE,
        acted_by=created_by, entered_at=now,
    )
    return invoice


# ---------------------------------------------------------------------------
# The single disposition entry point (used for bulk too)
# ---------------------------------------------------------------------------
def _parse_amount(value):
    if value in (None, '', 'null'):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        raise ValidationError('Amount must be a number.')


def _validate_disposition(invoice, user, stage_status, remarks, hold_type, amount):
    """Shared validation; raises ValidationError / PermissionDenied. Returns
    (kind, status, hold_type, amount) where kind is ADVANCE | RETURN | HOLD."""
    stage = invoice.current_stage

    if not can_act(user, invoice):
        raise PermissionDenied(
            f'You are not assigned to the "{stage.name}" stage.'
        )
    if invoice.status == Invoice.Status.COMPLETED:
        raise ValidationError('Invoice is already completed.')

    status = (stage_status or '').strip().upper()
    hold_type = (hold_type or '').strip().upper()
    if stage.requires_status:
        allowed = {s.upper() for s in stage.status_choices}
        if status not in allowed:
            raise ValidationError(
                f'Status is required at "{stage.name}" and must be one of '
                f'{sorted(stage.status_choices)}.'
            )

    positive = amount is not None and amount > 0

    # Resolve what kind of movement this is.
    if status in RETURN_STATUSES:
        kind = 'RETURN'
    elif status == 'HOLD':
        if hold_type == 'PARTIAL':
            # A portion of the value is withheld and the invoice advances. The
            # amount is mandatory except for RM-PM style categories.
            if not positive and _category_name(invoice) not in NO_HOLD_AMOUNT_CATEGORIES:
                raise ValidationError(
                    'A partial hold needs a hold amount before it can advance.')
            kind = 'ADVANCE'
        else:
            # Full hold: the invoice stays put until worked on later.
            hold_type = 'FULL'
            kind = 'HOLD'
    elif status == 'DEBIT':
        if not positive:
            raise ValidationError('A debit needs an amount before it can advance.')
        kind = 'ADVANCE'
    else:
        kind = 'ADVANCE'

    if kind == 'RETURN' and not stage.can_return:
        raise ValidationError(f'"{stage.name}" cannot return invoices.')
    if kind == 'ADVANCE' and stage.is_terminal:
        raise ValidationError(f'"{stage.name}" is the final stage.')

    # Reason enforcement: always on RETURN, plus any reason-required status.
    needs_reason = kind == 'RETURN' or status in REASON_REQUIRED_STATUSES
    if needs_reason and not (remarks or '').strip():
        raise ValidationError('Remarks are mandatory for this action.')

    return kind, status, hold_type, (amount if positive else None)


@transaction.atomic
def apply_action(*, invoice, user, action=None, stage_status='', remarks='',
                 hold_type='', amount=None):
    """Advance / return / hold a single invoice, enforcing all stage rules.

    `action` may be given explicitly ('ADVANCE' | 'RETURN') or left blank and
    inferred from `stage_status`. HOLD splits into a full hold (invoice stays)
    and a partial hold (amount withheld, invoice advances). DEBIT requires an
    amount and then advances. Returns the (possibly moved) invoice.
    """
    invoice = Invoice.objects.select_for_update().select_related(
        'current_stage', 'category').get(pk=invoice.pk)
    amount = _parse_amount(amount)
    kind, status, hold_type, amount = _validate_disposition(
        invoice, user, stage_status, remarks, hold_type, amount)

    # An explicit RETURN action overrides an advance-looking status.
    if (action or '').upper() == 'RETURN':
        if not invoice.current_stage.can_return:
            raise ValidationError('This stage cannot return invoices.')
        if not (remarks or '').strip():
            raise ValidationError('Remarks are mandatory to return an invoice.')
        kind = 'RETURN'

    now = timezone.now()
    stage = invoice.current_stage
    visit = _open_event(invoice)

    if kind == 'HOLD':
        # Annotate in place — the invoice does not move, dwell clock keeps
        # running. Recorded as its own immutable note row.
        StageEvent.objects.create(
            invoice=invoice, stage=stage,
            event_type=StageEvent.EventType.RECEIVE,
            stage_status=status, hold_type=hold_type, remarks=remarks,
            acted_by=user, entered_at=invoice.current_stage_entered_at,
        )
        return invoice

    # ADVANCE or RETURN both close the current visit.
    target = _route_neighbour(invoice, +1 if kind == 'ADVANCE' else -1)
    if target is None:
        raise ValidationError('No adjacent stage to move to.')

    if visit:
        visit.event_type = (
            StageEvent.EventType.ADVANCE if kind == 'ADVANCE'
            else StageEvent.EventType.RETURN
        )
        visit.stage_status = status
        visit.hold_type = hold_type
        visit.amount = amount
        visit.remarks = remarks
        visit.acted_by = user
        visit.exited_at = now
        visit.days_spent = _days_between(visit.entered_at, now)
        visit.save()

    # Move the invoice and open the next visit.
    invoice.current_stage = target
    invoice.current_stage_entered_at = now
    if stage.code == 'entry':
        invoice.is_locked = True   # advancing out of entry locks edits
    elif target.code == 'entry':
        # Returned all the way back to the Head Office / entry desk — let the
        # entry user edit it again before re-submitting.
        invoice.is_locked = False
    if target.is_terminal:
        # Ensure a payment row exists to fill in at the terminal stage.
        PaymentDetail.objects.get_or_create(invoice=invoice)
    invoice.save()

    StageEvent.objects.create(
        invoice=invoice, stage=target,
        event_type=StageEvent.EventType.RECEIVE,
        receiving_note=(
            StageEvent.ReceivingNote.LATE if _is_late(now)
            else StageEvent.ReceivingNote.ON_TIME
        ),
        acted_by=user, entered_at=now,
    )
    return invoice


def apply_bulk(*, invoice_ids, user, action=None, stage_status='', remarks='',
               hold_type='', amount=None):
    """Apply the same action to many invoices. All must sit at the same stage.
    Returns (processed, errors) where errors is a list of {id, error}."""
    invoices = list(
        Invoice.objects.filter(pk__in=invoice_ids).select_related('current_stage')
    )
    if not invoices:
        raise ValidationError('No invoices selected.')

    stages = {inv.current_stage_id for inv in invoices}
    if len(stages) > 1:
        raise ValidationError(
            'All selected invoices must be at the same stage to act in bulk.'
        )

    processed, errors = [], []
    for inv in invoices:
        try:
            apply_action(
                invoice=inv, user=user, action=action,
                stage_status=stage_status, remarks=remarks,
                hold_type=hold_type, amount=amount,
            )
            processed.append(inv.pk)
        except (ValidationError, PermissionDenied) as exc:
            errors.append({'id': inv.pk, 'error': str(getattr(exc, 'message', exc))})
    return processed, errors
