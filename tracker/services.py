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

PRE_AUDIT_STAGE_CODE = 'pre_audit'
TRANSPORT_APPROVAL_STAGE_CODE = 'transport_approval'
# Stages reached by a DETOUR, never as the next step along the line. They are
# excluded from `stage_route` on purpose: Pre-Audit's linear neighbour must stay
# Data Entry, and the detour is applied as an override in `_detour_target`. If
# one of these ever appeared in the route, `_route_neighbour` would walk into it
# for every invoice, Transport or not.
DETOUR_STAGE_CODES = {TRANSPORT_APPROVAL_STAGE_CODE}
# The JSAP desk mirrors a decision taken in the JSAP system. Mart invoices are
# not budget-approved there at all, so they skip the desk entirely.
JSAP_STAGE_CODE = 'jsap_approval'
# Category names (normalised) that don't require a hold amount on a partial hold.
NO_HOLD_AMOUNT_CATEGORIES = {'rm-pm', 'pm-pm', 'pm/pm'}

# Stamped on the zero-length visits `fast_track` writes for the desks an invoice
# was sent past. It is deliberately NOT in any Stage.status_choices — no desk can
# choose it, it only ever appears on a bypassed visit — so reports can separate
# "this desk decided X" from "this desk never saw it".
SKIPPED_STATUS = 'SKIPPED'


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
    or (for the entry stage) they created it.

    `user=None` means the system itself is acting (the JSAP sync mirroring a
    decision made in JSAP), which is always allowed — there is no person to
    map to a stage, and the event is logged with acted_by=NULL.
    """
    if user is None:
        return True
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


def _full_hold_note(invoice):
    """The FULL-hold NOTE on the invoice's CURRENT visit, or None.

    Keyed on (stage, entered_at) so it is scoped to THIS visit — an invoice that
    was held here, released, and later came back is not still considered held.
    Earliest first: if a desk somehow records two holds on one visit, the clock
    stopped at the first.
    """
    return (StageEvent.objects
            .filter(invoice=invoice,
                    stage=invoice.current_stage,
                    entered_at=invoice.current_stage_entered_at,
                    hold_type=StageEvent.HoldType.FULL)
            .order_by('created_at')
            .first())


def is_full_hold(invoice):
    """True if the invoice's CURRENT stage visit carries a FULL hold note.

    A full hold keeps the invoice in place (partial holds advance it), so the
    presence of a FULL hold event on this visit means it's parked on purpose."""
    return _full_hold_note(invoice) is not None


def full_hold_invoice_ids(invoice_ids):
    """Of `invoice_ids`, those whose CURRENT visit carries a FULL hold.

    The set form exists because the queue and the alert sweep both need to
    exclude held invoices from a LIST — calling `is_full_hold` per row is one
    query per invoice, which is the N+1 this avoids. Matching on
    (invoice, stage, entered_at) against the invoice's own current pointers is
    what scopes it to the CURRENT visit rather than any past hold.
    """
    if not invoice_ids:
        return set()
    rows = (StageEvent.objects
            .filter(invoice_id__in=invoice_ids,
                    hold_type=StageEvent.HoldType.FULL)
            .values_list('invoice_id', 'stage_id', 'entered_at'))
    if not rows:
        return set()
    current = dict(
        Invoice.objects.filter(id__in=[r[0] for r in rows])
        .values_list('id', 'current_stage_id'))
    entered = dict(
        Invoice.objects.filter(id__in=[r[0] for r in rows])
        .values_list('id', 'current_stage_entered_at'))
    return {
        inv_id for inv_id, stage_id, ent in rows
        if current.get(inv_id) == stage_id and entered.get(inv_id) == ent
    }


def stuck_visits(now=None, stage_ids=None):
    """Every in-progress invoice sitting at its stage beyond that stage's
    threshold. Returns a list of (invoice, stage, days_stuck).

    `stage_ids` restricts the scan to those stages, which is how `AlertsView`
    limits a non-superuser to their own desks: filtering in SQL rather than
    dropping rows after the dwell-time loop keeps the per-invoice work
    proportional to what the caller can actually see.

    The dwell test is per-invoice Python rather than SQL because the threshold
    lives on the stage and `days_at_stage` carries the rounding contract; at
    tracker's scale (hundreds of open invoices) one query plus a loop is
    cheaper than the alternative, and it keeps one definition of "stuck".
    """
    now = now or timezone.now()
    qs = (Invoice.objects
          .filter(status=Invoice.Status.IN_PROGRESS)
          .select_related('current_stage'))
    if stage_ids is not None:
        qs = qs.filter(current_stage_id__in=stage_ids)
    out = []
    for inv in qs:
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
    """Live dwell time (in days) at the invoice's current stage.

    A FULL hold does NOT pause this — the clock keeps running while an invoice
    is parked, so the true age of anything sitting on a desk stays visible. Full
    holds are instead kept out of the stuck-alert EMAILS and out of the queue's
    Current tab; the ageing itself is deliberately unadjusted.
    """
    now = now or timezone.now()
    return _days_between(invoice.current_stage_entered_at, now)


def is_overdue(invoice, now=None):
    return days_at_stage(invoice, now) > Decimal(invoice.current_stage.threshold_days)


def _category_name(invoice):
    return (getattr(invoice.category, 'name', '') or '').strip().lower()


def _is_transport(invoice):
    return _category_name(invoice) == 'transport'


def stage_route(invoice):
    """The ordered stages this invoice actually travels through, IN LINE.

    Non-Transport invoices skip the Bilty/GRPO desk and go Entry -> Pre-Audit.
    Detour desks (Transport Approval) are excluded for everyone — they are not
    a step along the line, they are reached and left via `_detour_target`.
    """
    stages = [s for s in Stage.objects.filter(is_active=True).order_by('order')
              if s.code not in DETOUR_STAGE_CODES]
    if _is_transport(invoice):
        return stages
    return [s for s in stages if s.code not in TRANSPORT_ONLY_STAGE_CODES]


def _transport_approved(invoice):
    """True once Transport Approval has APPROVED this invoice.

    Gating on APPROVED rather than "has visited the desk" is what makes a
    rejection mean something: a rejected invoice goes back to Pre-Audit and, on
    the next advance, is sent to Transport Approval AGAIN. Were this merely
    "has been there", one rejection would let the invoice slip past the desk
    into Data Entry — the exact approval it was denied.
    """
    return invoice.events.filter(
        stage__code=TRANSPORT_APPROVAL_STAGE_CODE,
        stage_status='APPROVED',
    ).exists()


def _detour_target(invoice, kind):
    """The Transport Approval detour, or None to use the normal route.

        Pre-Audit --(advance, Transport, not yet approved)--> Transport Approval
        Transport Approval --APPROVED or REJECTED----------> Pre-Audit

    Both verdicts hand the invoice straight back to Pre-Audit, so the desk is
    entered and left from the same place; the SECOND Pre-Audit advance is the
    one that continues to Data Entry. Only an ADVANCE out of Pre-Audit is
    diverted — a RETURN from Pre-Audit still walks back down the line.
    """
    code = invoice.current_stage.code

    if code == TRANSPORT_APPROVAL_STAGE_CODE:
        # Leaving the desk: APPROVED (an advance) and REJECTED (a return) both
        # land on Pre-Audit. The stage is off-route, so `_route_neighbour`
        # cannot find it and would raise "no adjacent stage" — this is the only
        # way out.
        return Stage.objects.filter(
            code=PRE_AUDIT_STAGE_CODE, is_active=True).first()

    if (kind == 'ADVANCE' and code == PRE_AUDIT_STAGE_CODE
            and _is_transport(invoice) and not _transport_approved(invoice)):
        # Detour only exists while the desk is active and assigned; if it has
        # been deactivated, fall through to the normal route rather than
        # stranding the invoice.
        return Stage.objects.filter(
            code=TRANSPORT_APPROVAL_STAGE_CODE, is_active=True).first()

    return None


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


FAST_TRACK_FROM_CODE = 'entry'
FAST_TRACK_TO_CODE = 'sap_approval'


@transaction.atomic
def fast_track(invoice, user, remarks):
    """Send an invoice straight from Invoice Entry to SAP Approval.

    The desks in between — Bilty/GRPO, Pre-Audit, Data Entry — are recorded as
    SKIPPED rather than omitted. Each gets a closed visit stamped
    `entered_at == exited_at` and `days_spent = 0`, carrying the same remarks and
    the user who fast-tracked it.

    Writing the skipped rows is not bookkeeping pedantry. Every duration and
    bottleneck figure in `reports.py` is derived from `StageEvent` visits, and
    `stage_route` is what the flow display walks. An invoice that simply
    teleported would leave those desks with fewer visits than invoices passed
    through them, so their averages would silently describe a different
    population than their counts — and nobody reading the report would know a
    desk had been bypassed at all. A zero-day visit says "this desk was skipped,
    here is who did it and why", which is the auditable version of the same fact.

    `remarks` are MANDATORY: this bypasses Pre-Audit, and Pre-Audit is where
    holds and debits are captured. Skipping it silently would lose the only
    record of why an invoice avoided the money checks.
    """
    if invoice.current_stage.code != FAST_TRACK_FROM_CODE:
        raise ValidationError(
            'Only an invoice at Invoice Entry can be sent straight to SAP Approval.')
    if not (remarks or '').strip():
        raise ValidationError(
            'A reason is mandatory when skipping Pre-Audit and Data Entry.')
    if not can_act(user, invoice):
        raise PermissionDenied('You cannot act on this invoice.')

    route = stage_route(invoice)
    codes = [s.code for s in route]
    if FAST_TRACK_TO_CODE not in codes:
        raise ValidationError('SAP Approval is not on this invoice\'s route.')
    start = codes.index(invoice.current_stage.code)
    end = codes.index(FAST_TRACK_TO_CODE)
    if end <= start:
        raise ValidationError('SAP Approval is not ahead of this invoice.')

    now = timezone.now()
    target = route[end]
    skipped = route[start + 1:end]

    # Close the Entry visit as a normal ADVANCE so its dwell time is real.
    visit = _open_event(invoice)
    if visit:
        visit.event_type = StageEvent.EventType.ADVANCE
        visit.remarks = remarks
        visit.acted_by = user
        visit.exited_at = now
        visit.days_spent = _days_between(visit.entered_at, now)
        visit.save()

    # Zero-length visits for the desks that were bypassed.
    for stage in skipped:
        StageEvent.objects.create(
            invoice=invoice, stage=stage,
            event_type=StageEvent.EventType.ADVANCE,
            stage_status=SKIPPED_STATUS,
            remarks=remarks, acted_by=user,
            entered_at=now, exited_at=now, days_spent=Decimal('0.00'),
        )

    invoice.current_stage = target
    invoice.current_stage_entered_at = now
    invoice.rejection_pending = False
    invoice.is_locked = True            # it has left entry; entry can no longer edit
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


def _open_event(invoice):
    """The still-open VISIT row for the invoice's current stage.

    `NOTE` rows are excluded, and that exclusion is the whole point. A note —
    a full HOLD, or a rejection parked awaiting its reason — is written with
    `entered_at` copied from the visit and no `exited_at`, so before it had its
    own event type a held stage had two rows that both looked open and tied on
    the only column this orders by. Which one a later advance closed was left
    to the database; the other stayed open at a stage the invoice had left,
    where every queue count and dwell report still counted it.

    The tie-break on `id` is belt and braces. A re-entry after a RETURN can
    legitimately open a second visit at the same stage, and `entered_at` is a
    timestamp two rows can share; without a deterministic second key the pick
    would be arbitrary again in exactly the case that is hardest to reproduce.
    """
    return (
        invoice.events
        .filter(stage=invoice.current_stage, exited_at__isnull=True)
        .exclude(event_type=StageEvent.EventType.NOTE)
        .order_by('-entered_at', '-id')
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
        # SAP/JSAP: a REJECT without remarks is parked as "pending rejection"
        # (stays at the stage, shows in the Rejected tab) rather than returned.
        # Supplying remarks (now or later) turns it into a real RETURN.
        if status == 'REJECTED' and not (remarks or '').strip():
            kind = 'REJECT_PENDING'
        else:
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

    # Reason enforcement: always on RETURN, plus any reason-required status —
    # EXCEPT a pending rejection, which is allowed precisely because it has no
    # remarks yet (they come later, when it is returned).
    if kind == 'REJECT_PENDING':
        needs_reason = False
    else:
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
        #
        # NOTE, not RECEIVE: this row is an annotation, not an occupancy. As a
        # RECEIVE it was indistinguishable from the open visit beside it — see
        # `_open_event` and `StageEvent.EventType.NOTE`.
        StageEvent.objects.create(
            invoice=invoice, stage=stage,
            event_type=StageEvent.EventType.NOTE,
            stage_status=status, hold_type=hold_type, remarks=remarks,
            acted_by=user, entered_at=invoice.current_stage_entered_at,
        )
        return invoice

    if kind == 'REJECT_PENDING':
        # Rejected but no reason yet: flag it and keep it here (Rejected tab).
        # An immutable note records the rejection; remarks follow when returned.
        invoice.rejection_pending = True
        invoice.save(update_fields=['rejection_pending', 'updated_at'])
        # NOTE for the same reason as the HOLD branch above. `exited_at` stays
        # NULL deliberately — `tracker/views.py` reads exactly that to mean
        # "awaiting the written reason", which is what makes the SAP/JSAP
        # two-step rejection work.
        StageEvent.objects.create(
            invoice=invoice, stage=stage,
            event_type=StageEvent.EventType.NOTE,
            stage_status='REJECTED', remarks='',
            acted_by=user, entered_at=invoice.current_stage_entered_at,
        )
        return invoice

    # ADVANCE or RETURN both close the current visit. The Transport Approval
    # detour overrides the linear neighbour in both directions (see
    # `_detour_target`); everything else walks the route.
    target = (_detour_target(invoice, kind)
              or _route_neighbour(invoice, +1 if kind == 'ADVANCE' else -1))
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

    # A DEBIT disposition withholds part of the value permanently — preserve it
    # on the invoice so every later stage and the payment maths use the net value.
    if kind == 'ADVANCE' and status == 'DEBIT' and amount:
        invoice.debit_amount = (invoice.debit_amount or Decimal('0')) + amount
    # A PARTIAL hold advances the invoice but holds a portion back — preserve the
    # held amount for the payment stage (subtracted unless released there).
    if kind == 'ADVANCE' and status == 'HOLD' and hold_type == 'PARTIAL' and amount:
        invoice.hold_amount = (invoice.hold_amount or Decimal('0')) + amount

    # Move the invoice and open the next visit.
    invoice.current_stage = target
    invoice.current_stage_entered_at = now
    invoice.rejection_pending = False   # any pending rejection is resolved on move
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


# ---------------------------------------------------------------------------
# Payment (terminal stage)
# ---------------------------------------------------------------------------
def _q2(value):
    """Round to 2 decimals (bankers' half-up), tolerant of None."""
    return Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def compute_payment(invoice, *, discount_pct, tds_pct, paid_amount, hold_added_back=False):
    """Derive every payment figure from the two percentages + the paid amount.

    Discount base = net invoice value (invoice_value - debit) — this INCLUDES any
    held-back portion, so discount is unaffected by a hold. TDS% applies to the
    taxable value (pre-GST).

    Two different figures come out of the hold:
      * net_payable  — what may be PAID this round. Subtracts the hold amount
        UNLESS `hold_added_back` releases it. This is the cap on the paid amount.
      * total_owed   — the full obligation, which ALWAYS includes the hold (it is
        part of net invoice value). The open balance is measured against this, so
        a withheld hold keeps the invoice open (balance never drops below the
        hold) until it is released and paid.

    `paid_amount=None` defaults to the full net payable. Raises ValidationError
    if paid is negative or exceeds net payable.
    """
    net_invoice = invoice.net_invoice_value            # invoice_value - debit
    hold = invoice.hold_amount or Decimal('0')
    payable_base = net_invoice if hold_added_back else _q2(net_invoice - hold)
    if payable_base < 0:
        payable_base = Decimal('0.00')

    # Discount is on the FULL net invoice value (i.e. incl. the held portion).
    discount_amount = _q2(net_invoice * (discount_pct or 0) / Decimal('100'))
    tds_amount = _q2((invoice.taxable_value or 0) * (tds_pct or 0) / Decimal('100'))

    net_payable = _q2(payable_base - discount_amount - tds_amount)   # cap this round
    if net_payable < 0:
        net_payable = Decimal('0.00')
    total_owed = _q2(net_invoice - discount_amount - tds_amount)     # full obligation (incl. hold)
    if total_owed < 0:
        total_owed = Decimal('0.00')

    paid = net_payable if paid_amount is None else _q2(paid_amount)
    if paid < 0:
        raise ValidationError('Paid amount cannot be negative.')
    if paid > net_payable:
        raise ValidationError(
            f'Paid amount (₹{paid}) cannot exceed the net payable (₹{net_payable}).')

    # Open balance is against the full obligation, so any un-released hold stays
    # outstanding here even after this round's payable is fully paid.
    open_balance = _q2(total_owed - paid)
    return {
        'discount_amount': discount_amount,
        'tds_amount': tds_amount,
        'net_payable': net_payable,
        'total_owed': total_owed,
        'paid_amount': paid,
        'open_balance': open_balance,
    }


@transaction.atomic
def apply_payment(*, invoice, user, discount_pct=0, tds_pct=0, paid_amount=None,
                  hold_added_back=False):
    """Record/update a payment at the terminal stage.

    Percentages drive the discount/TDS amounts; the paid amount is capped at the
    net payable. A zero open balance marks the invoice PAID and COMPLETED (it
    leaves the queue); any positive balance keeps it OPEN and IN_PROGRESS so it
    stays on the payment desk (Partial tab) for further part-payments.
    """
    invoice = Invoice.objects.select_for_update().select_related('current_stage').get(pk=invoice.pk)
    if not invoice.current_stage.is_terminal:
        raise ValidationError('Invoice is not at the payment stage.')

    dpct = _parse_amount(discount_pct) or Decimal('0')
    tpct = _parse_amount(tds_pct) or Decimal('0')
    for pct, name in ((dpct, 'Discount'), (tpct, 'TDS')):
        if pct < 0 or pct > 100:
            raise ValidationError(f'{name} % must be between 0 and 100.')

    hold_back = bool(hold_added_back)
    calc = compute_payment(
        invoice, discount_pct=dpct, tds_pct=tpct,
        paid_amount=_parse_amount(paid_amount), hold_added_back=hold_back)

    payment, _ = PaymentDetail.objects.get_or_create(invoice=invoice)
    payment.discount_pct = dpct
    payment.tds_pct = tpct
    payment.hold_added_back = hold_back
    payment.discount_amount = calc['discount_amount']
    payment.tds_amount = calc['tds_amount']
    payment.paid_amount = calc['paid_amount']
    payment.open_balance = calc['open_balance']
    payment.status = (PaymentDetail.Status.PAID if calc['open_balance'] <= 0
                      else PaymentDetail.Status.OPEN)
    payment.updated_by = user
    payment.save()

    if payment.status == PaymentDetail.Status.PAID:
        if invoice.status != Invoice.Status.COMPLETED:
            invoice.status = Invoice.Status.COMPLETED
            invoice.save(update_fields=['status', 'updated_at'])
    elif invoice.status != Invoice.Status.IN_PROGRESS:
        # A previously-completed invoice being re-opened (paid amount lowered).
        invoice.status = Invoice.Status.IN_PROGRESS
        invoice.save(update_fields=['status', 'updated_at'])

    return invoice, payment


# ---------------------------------------------------------------------------
# JSAP desk — mirrors the budget decision made in the JSAP system
# ---------------------------------------------------------------------------
def sync_jsap(invoice, *, user=None):
    """Reconcile one invoice at the JSAP desk with JSAP's own decision.

    JSAP is the system of record for the budget approval; this desk only
    reflects it, so nothing is ever written back to JSAP. Behaviour:

        Approved -> advance to the next stage
        Rejected -> return to SAP Approval, carrying JSAP's own reason
        Pending / not found -> leave it here (the desk shows why)

    Returns {'changed': bool, 'action': ADVANCE|RETURN|None, 'status': {...}}.
    Safe to call on any invoice: one not at the JSAP desk is a no-op.
    """
    from . import jsap

    result = {'changed': False, 'action': None, 'status': None}
    if invoice.current_stage.code != JSAP_STAGE_CODE:
        return result
    if invoice.status == Invoice.Status.COMPLETED:
        return result
    # A handler rejected this by hand and is still writing the reason. Their
    # decision outranks the mirror — otherwise the next sweep could quietly
    # advance an invoice someone had deliberately parked.
    if invoice.rejection_pending:
        result['status'] = {'available': False, 'reason': 'rejection_pending',
                            'detail': 'Manually rejected here — awaiting remarks.'}
        return result

    status = jsap.status_for_invoice(invoice)
    result['status'] = status
    if not status.get('available'):
        # Mart is never budget-approved in JSAP — it would otherwise sit here
        # forever, so let it straight through.
        if status.get('reason') == 'not_in_jsap':
            apply_action(invoice=invoice, user=user, action='ADVANCE',
                         stage_status='APPROVED',
                         remarks='Not applicable in JSAP — passed through.')
            result.update(changed=True, action='ADVANCE')
        return result

    if status['status'] == jsap.STATUS_APPROVED:
        apply_action(invoice=invoice, user=user, action='ADVANCE',
                     stage_status='APPROVED',
                     remarks=(status.get('description')
                              or f"Approved in JSAP (draft {status['doc_entry']})."))
        result.update(changed=True, action='ADVANCE')
    elif status['status'] == jsap.STATUS_REJECTED:
        # Remarks are mandatory on a return, and JSAP's description is the
        # approver's actual reason — fall back only if it was left blank.
        apply_action(invoice=invoice, user=user, action='RETURN',
                     stage_status='REJECTED',
                     remarks=(status.get('description')
                              or f"Rejected in JSAP (draft {status['doc_entry']})."))
        result.update(changed=True, action='RETURN')
    return result


def sync_jsap_all(*, user=None, limit=None):
    """Run sync_jsap over every invoice parked at the JSAP desk.

    Used by the `sync_jsap` management command and the manual refresh button.
    One invoice's failure never stops the sweep.
    """
    qs = (Invoice.objects
          .filter(current_stage__code=JSAP_STAGE_CODE,
                  status=Invoice.Status.IN_PROGRESS)
          .select_related('current_stage', 'category', 'unit', 'branch'))
    if limit:
        qs = qs[:limit]

    advanced, returned, waiting, errors = [], [], [], []
    for inv in qs:
        try:
            res = sync_jsap(inv, user=user)
        except (ValidationError, PermissionDenied) as exc:
            errors.append({'id': inv.pk, 'error': str(getattr(exc, 'message', exc))})
            continue
        if res['action'] == 'ADVANCE':
            advanced.append(inv.pk)
        elif res['action'] == 'RETURN':
            returned.append(inv.pk)
        else:
            waiting.append(inv.pk)
    return {'advanced': advanced, 'returned': returned,
            'waiting': waiting, 'errors': errors}


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
