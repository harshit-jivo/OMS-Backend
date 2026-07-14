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


def _filter_invoices(params):
    qs = Invoice.objects.select_related('current_stage', 'category')
    frm, to = params.get('from'), params.get('to')
    if frm:
        qs = qs.filter(created_at__date__gte=frm)
    if to:
        qs = qs.filter(created_at__date__lte=to)
    for f in ('branch', 'unit', 'category'):
        val = params.get(f)
        if val:
            qs = qs.filter(**{f'{f}_id': val})
    return qs


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
        'avg_days_per_stage': avg_days_per_stage,
        'bottleneck_by_person': by_person,
        'bottleneck_by_vendor': by_vendor,
        'bottleneck_by_category': by_category,
        'ageing': [{'bucket': label, 'count': ageing[label]}
                   for label, _, _ in AGEING_BUCKETS],
    }
