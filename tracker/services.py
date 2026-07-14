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


def _adjacent_stage(stage, step):
    return Stage.objects.filter(order=stage.order + step, is_active=True).first()


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
def _validate_disposition(invoice, user, stage_status, remarks):
    """Shared validation; raises ValidationError / PermissionDenied. Returns
    the resolved movement kind: 'ADVANCE' | 'RETURN' | 'HOLD'."""
    stage = invoice.current_stage

    if not can_act(user, invoice):
        raise PermissionDenied(
            f'You are not assigned to the "{stage.name}" stage.'
        )
    if invoice.status == Invoice.Status.COMPLETED:
        raise ValidationError('Invoice is already completed.')

    status = (stage_status or '').strip().upper()
    if stage.requires_status:
        allowed = {s.upper() for s in stage.status_choices}
        if status not in allowed:
            raise ValidationError(
                f'Status is required at "{stage.name}" and must be one of '
                f'{sorted(stage.status_choices)}.'
            )

    # Resolve what kind of movement this is.
    if status in RETURN_STATUSES:
        kind = 'RETURN'
    elif status in HOLD_STATUSES:
        kind = 'HOLD'
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

    return kind, status


@transaction.atomic
def apply_action(*, invoice, user, action=None, stage_status='', remarks=''):
    """Advance / return / hold a single invoice, enforcing all stage rules.

    `action` may be given explicitly ('ADVANCE' | 'RETURN') or left blank and
    inferred from `stage_status` (OK->advance, RETURN/REJECTED->return,
    HOLD->hold). Returns the (possibly moved) invoice.
    """
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    kind, status = _validate_disposition(invoice, user, stage_status, remarks)

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
            stage_status=status, remarks=remarks, acted_by=user,
            entered_at=invoice.current_stage_entered_at,
        )
        return invoice

    # ADVANCE or RETURN both close the current visit.
    target = _adjacent_stage(stage, +1 if kind == 'ADVANCE' else -1)
    if target is None:
        raise ValidationError('No adjacent stage to move to.')

    if visit:
        visit.event_type = (
            StageEvent.EventType.ADVANCE if kind == 'ADVANCE'
            else StageEvent.EventType.RETURN
        )
        visit.stage_status = status
        visit.remarks = remarks
        visit.acted_by = user
        visit.exited_at = now
        visit.days_spent = _days_between(visit.entered_at, now)
        visit.save()

    # Move the invoice and open the next visit.
    invoice.current_stage = target
    invoice.current_stage_entered_at = now
    if stage.code == 'entry':
        invoice.is_locked = True  # first handoff locks entry edits forever
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


def apply_bulk(*, invoice_ids, user, action=None, stage_status='', remarks=''):
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
            )
            processed.append(inv.pk)
        except (ValidationError, PermissionDenied) as exc:
            errors.append({'id': inv.pk, 'error': str(getattr(exc, 'message', exc))})
    return processed, errors
