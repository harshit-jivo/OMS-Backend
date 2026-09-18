"""Reporting aggregations for the document tracker.

Everything is derived from the append-only StageEvent log plus the live
current-stage pointer:

  * pending counts / overdue     -> open invoices at each stage
  * average days per stage       -> closed stage visits (days_spent)
  * bottleneck by stage/person/  -> closed visits grouped various ways
    vendor/category
  * ageing of open invoices      -> live dwell time bucketed

Volumes are office-scale, so overdue / ageing (which need a per-stage
threshold comparison) are computed in Python for clarity; the heavier
historical averages are aggregated in the database.
"""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg, Count
from django.utils import timezone

from .models import Invoice, Stage, StageEvent
from .services import days_at_stage

AGEING_BUCKETS = [
    ('0-2 days', 0, 2),
    ('3-5 days', 3, 5),
    ('6-10 days', 6, 10),
    ('10+ days', 11, None),
]


def _filter_invoices(params, with_dates=True):
    """The report's invoice population.

    `with_dates=False` drops the created-at window but keeps branch/unit/
    category. The per-stage flow metrics need that: they are counted by when an
    invoice ARRIVED at a desk, so an invoice raised in March that reached SAP
    Approval in September belongs in September's arrivals. Filtering the
    population by creation date first would have silently excluded it.
    """
    qs = Invoice.objects.select_related('current_stage', 'category')
    frm, to = params.get('from'), params.get('to')
    if with_dates:
        if frm:
            qs = qs.filter(created_at__date__gte=frm)
        if to:
            qs = qs.filter(created_at__date__lte=to)
    for f in ('branch', 'unit', 'category'):
        val = params.get(f)
        if val:
            qs = qs.filter(**{f'{f}_id': val})
    return qs


def _window(qs, params, field):
    """Apply the report's date window to `field` on an event queryset."""
    frm, to = params.get('from'), params.get('to')
    if frm:
        qs = qs.filter(**{f'{field}__date__gte': frm})
    if to:
        qs = qs.filter(**{f'{field}__date__lte': to})
    return qs


def _flow_by_stage(params, stages):
    """Arrivals and decisions per stage inside the selected window.

    **Volume** counts stage VISITS that began in the window — "how many invoices
    reached this desk". `NOTE` rows are excluded and that exclusion is load-
    bearing: a note copies the visit's `entered_at` (see `services.apply_action`),
    so counting it would report a second arrival that never happened, and a desk
    that holds a lot would look busier than it is.

    **Decisions** counts what each desk decided. A decision is dated when it was
    MADE, which differs by row: a visit closes at `exited_at`, while a note —
    a full hold, or a rejection parked awaiting its reason — never closes, so it
    is dated by `created_at`. Both are counted, so an invoice held and later
    advanced contributes a HOLD and an OK; those are two real decisions by that
    desk, not double counting.
    """
    inv_ids = list(_filter_invoices(params, with_dates=False)
                   .values_list('id', flat=True))
    base = StageEvent.objects.filter(invoice_id__in=inv_ids)

    arrivals = dict(
        _window(base.exclude(event_type=StageEvent.EventType.NOTE),
                params, 'entered_at')
        .values_list('stage_id').annotate(n=Count('id'))
    )

    decided = defaultdict(lambda: defaultdict(int))
    closed_rows = _window(
        base.exclude(event_type=StageEvent.EventType.NOTE)
            .filter(exited_at__isnull=False).exclude(stage_status=''),
        params, 'exited_at',
    ).values_list('stage_id', 'stage_status')
    note_rows = _window(
        base.filter(event_type=StageEvent.EventType.NOTE).exclude(stage_status=''),
        params, 'created_at',
    ).values_list('stage_id', 'stage_status')
    for stage_id, status in list(closed_rows) + list(note_rows):
        decided[stage_id][status] += 1

    out = []
    for s in stages:
        statuses = decided.get(s.id, {})
        out.append({
            'stage_code': s.code,
            'stage_name': s.name,
            'order': s.order,
            'arrived': arrivals.get(s.id, 0),
            'decided': sum(statuses.values()),
            'decisions': [{'status': k, 'count': v}
                          for k, v in sorted(statuses.items(),
                                             key=lambda kv: (-kv[1], kv[0]))],
        })
    return out


def _round(v):
    return float(Decimal(v or 0).quantize(Decimal('0.01')))


def build_report(params):
    invoices = _filter_invoices(params)
    inv_ids = list(invoices.values_list('id', flat=True))
    now = timezone.now()

    stages = list(Stage.objects.filter(is_active=True).order_by('order'))
    stage_by_id = {s.id: s for s in stages}

    # --- Pending + overdue per stage (open invoices) ---
    open_invoices = list(
        invoices.filter(status=Invoice.Status.IN_PROGRESS)
    )
    pending = defaultdict(lambda: {'count': 0, 'overdue': 0})
    ageing = {label: 0 for label, _, _ in AGEING_BUCKETS}
    for inv in open_invoices:
        sid = inv.current_stage_id
        pending[sid]['count'] += 1
        d = float(days_at_stage(inv, now))
        if d > inv.current_stage.threshold_days:
            pending[sid]['overdue'] += 1
        for label, lo, hi in AGEING_BUCKETS:
            if d >= lo and (hi is None or d <= hi):
                ageing[label] += 1
                break

    pending_by_stage = [{
        'stage_code': s.code,
        'stage_name': s.name,
        'order': s.order,
        'count': pending[s.id]['count'],
        'overdue': pending[s.id]['overdue'],
    } for s in stages]

    total_overdue = sum(p['overdue'] for p in pending_by_stage)

    # --- Closed stage visits (for averages / bottlenecks) ---
    closed = StageEvent.objects.filter(
        invoice_id__in=inv_ids,
        exited_at__isnull=False,
        days_spent__isnull=False,
    )

    # Average days per stage
    per_stage = closed.values('stage_id').annotate(
        avg=Avg('days_spent'), visits=Count('id'))
    avg_days_per_stage = []
    for row in per_stage:
        s = stage_by_id.get(row['stage_id'])
        if not s:
            continue
        avg_days_per_stage.append({
            'stage_code': s.code, 'stage_name': s.name, 'order': s.order,
            'avg_days': _round(row['avg']), 'visits': row['visits'],
        })
    avg_days_per_stage.sort(key=lambda x: x['order'])

    # Bottleneck by person
    by_person = [{
        'key': row['acted_by__username'] or '—',
        'avg_days': _round(row['avg']), 'visits': row['visits'],
    } for row in closed.values('acted_by__username').annotate(
        avg=Avg('days_spent'), visits=Count('id')).order_by('-avg')]

    # Bottleneck by vendor (party) and category
    by_vendor = [{
        'key': row['invoice__party_name'],
        'avg_days': _round(row['avg']), 'visits': row['visits'],
    } for row in closed.values('invoice__party_name').annotate(
        avg=Avg('days_spent'), visits=Count('id')).order_by('-avg')[:20]]

    by_category = [{
        'key': row['invoice__category__name'] or '—',
        'avg_days': _round(row['avg']), 'visits': row['visits'],
    } for row in closed.values('invoice__category__name').annotate(
        avg=Avg('days_spent'), visits=Count('id')).order_by('-avg')]

    # --- Summary ---
    completed_qs = invoices.filter(status=Invoice.Status.COMPLETED)
    # Average cycle = mean of (sum of days_spent) over completed invoices.
    cycle_rows = (
        StageEvent.objects
        .filter(invoice__in=completed_qs, days_spent__isnull=False)
        .values('invoice_id').annotate(total=Count('id'))
    )
    completed_ids = [r['invoice_id'] for r in cycle_rows]
    cycle_totals = defaultdict(Decimal)
    for ev in StageEvent.objects.filter(
        invoice_id__in=completed_ids, days_spent__isnull=False
    ).values('invoice_id', 'days_spent'):
        cycle_totals[ev['invoice_id']] += ev['days_spent']
    avg_cycle = (
        _round(sum(cycle_totals.values()) / len(cycle_totals))
        if cycle_totals else 0.0
    )

    return {
        'summary': {
            'in_progress': len(open_invoices),
            'completed': completed_qs.count(),
            'overdue': total_overdue,
            'avg_cycle_days': avg_cycle,
        },
        'pending_by_stage': pending_by_stage,
        # Throughput, not backlog: what MOVED through each desk in the window,
        # and what each desk decided. `pending_by_stage` above is the snapshot of
        # what is sitting there right now — the two answer different questions
        # and will not tally.
        'flow_by_stage': _flow_by_stage(params, stages),
        'avg_days_per_stage': avg_days_per_stage,
        'bottleneck_by_person': by_person,
        'bottleneck_by_vendor': by_vendor,
        'bottleneck_by_category': by_category,
        'ageing': [{'bucket': label, 'count': ageing[label]}
                   for label, _, _ in AGEING_BUCKETS],
    }
