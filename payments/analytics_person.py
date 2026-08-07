"""Per-person analytics for the Payments Dashboard drill-down.

Split from `analytics.py` because it answers a different question: that module
aggregates the company, this one aggregates one participant. Keeping them apart
means the dashboard's shared helpers stay small and the person view can grow its
own charts without lengthening the page-level module.

The identity model here matters. A participant is `(kind, id)` where kind is
either `person` (a CollectionPerson — who money was received from, or who banked
it) or `user` (an OMS login — who recorded the receipt or submitted the
deposit). The same human can appear as both, and the two are NOT merged: the
system holds no link between a login and a collection person, so joining them by
name would be a guess dressed up as data.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce

from .analytics import (
    DEFAULT_PRESET,
    ZERO,
    _deposits,
    _money,
    _receipts,
    _slice,
    _sum,
    resolve_range,
)
from .models import CollectionPerson, PaymentMethodEntry


def _person_receipts(kind, pk, company, start, end):
    """Receipts attributable to one participant.

    `kind` decides WHICH relationship is being asked about — the same person can
    be the collector a receipt was received from, or the login that recorded it,
    and those are different questions with different answers.
    """
    qs = _receipts(company, start, end)
    return (qs.filter(received_from_person_id=pk) if kind == 'person'
            else qs.filter(created_by_id=pk))


def _person_deposits(kind, pk, company, start, end):
    qs = _deposits(company, start, end)
    return (qs.filter(deposited_by_id=pk) if kind == 'person'
            else qs.filter(created_by_id=pk))


def _recent_activity(receipts, deposits, limit=30):
    """Recent activity, receipts and deposits interleaved by date.

    One list rather than two, because "what did this person do last week" is a
    single question — reading it off two tables side by side makes the reader do
    the merge themselves.
    """
    events = []
    for row in (receipts.order_by('-payment_date', '-id')[:limit]
                .values('id', 'receipt_no', 'payment_date', 'total_amount',
                        'status', 'card_name', 'is_advance')):
        events.append({
            'kind': 'RECEIPT',
            'id': row['id'],
            'reference': row['receipt_no'],
            'date': row['payment_date'].isoformat(),
            'amount': _money(row['total_amount']),
            'status': row['status'],
            'party': row['card_name'],
            'detail': 'Advance' if row['is_advance'] else 'Against invoice',
        })
    for row in (deposits.order_by('-deposit_date', '-id')[:limit]
                .values('id', 'deposit_no', 'deposit_date', 'deposit_amount',
                        'status', 'bank_display_name', 'deposit_type')):
        events.append({
            'kind': 'DEPOSIT',
            'id': row['id'],
            'reference': row['deposit_no'],
            'date': row['deposit_date'].isoformat(),
            'amount': _money(row['deposit_amount']),
            'status': row['status'],
            'party': row['bank_display_name'] or 'Bank',
            'detail': row['deposit_type'].title(),
        })

    # Newest first. `reference` breaks ties so the order is stable between
    # calls rather than depending on which queryset happened to be read first.
    events.sort(key=lambda e: (e['date'], e['reference']), reverse=True)
    return events[:limit]


def _daily_series(receipts, deposits, start, end, max_days=93):
    """Collected vs banked per day — the collection timeline chart.

    Capped at a quarter: a year-long custom range would return 365 points, which
    no phone-sized chart renders legibly. Past the cap the series is omitted and
    the client falls back to the totals rather than drawing a dense smear that
    implies detail it cannot show.
    """
    if (end - start).days + 1 > max_days:
        return []

    by_day = {}
    for row in (receipts.values('payment_date')
                .annotate(amount=_sum('total_amount'))):
        day = by_day.setdefault(row['payment_date'],
                                {'received': 0.0, 'deposited': 0.0})
        day['received'] = _money(row['amount'])
    for row in (deposits.values('deposit_date')
                .annotate(amount=_sum('deposit_amount'))):
        day = by_day.setdefault(row['deposit_date'],
                                {'received': 0.0, 'deposited': 0.0})
        day['deposited'] = _money(row['amount'])

    return [{'date': day.isoformat(), **values}
            for day, values in sorted(by_day.items())]


def _identity(kind, pk):
    """Name and code for one participant, or None if the row is gone."""
    if kind == 'person':
        row = (CollectionPerson.objects.filter(pk=pk)
               .values('id', 'name', 'code').first())
        return row and {**row, 'subtitle': 'Collection person'}

    row = (get_user_model().objects.filter(pk=pk)
           .values('id', 'username', 'name').first())
    if not row:
        return None
    return {'id': row['id'],
            'name': (row['name'] or '').strip() or row['username'],
            'code': row['username'], 'subtitle': 'OMS user'}


def person_detail(kind, pk, *, company='', preset=DEFAULT_PRESET,
                  date_from=None, date_to=None, today=None):
    """Everything the person-detail view shows, for one participant.

    Returns None when the identity does not exist, so the view can 404 rather
    than render a convincing-looking page of zeroes for a mistyped id.
    """
    kind = 'person' if kind == 'person' else 'user'
    company = (company or '').strip().upper()
    start, end = resolve_range(preset, date_from, date_to, today=today)

    who = _identity(kind, pk)
    if not who:
        return None

    receipts = _person_receipts(kind, pk, company, start, end)
    deposits = _person_deposits(kind, pk, company, start, end)

    totals = receipts.aggregate(
        total=_sum('total_amount'),
        against_invoice=Coalesce(
            Sum('total_amount', filter=Q(is_advance=False)), ZERO),
        advance=Coalesce(
            Sum('total_amount', filter=Q(is_advance=True)), ZERO),
        count=Count('id'),
    )
    banked = deposits.aggregate(total=_sum('deposit_amount'),
                                collected=_sum('collected_amount'),
                                count=Count('id'))

    # Method mix for this person only. Same shape as the dashboard donut, so the
    # clients render both with one chart component.
    method_rows = (PaymentMethodEntry.objects
                   .filter(receipt__in=receipts)
                   .values('method')
                   .annotate(amount=_sum('amount')))
    by_method = {r['method']: r['amount'] for r in method_rows}
    method_total = _money(sum(by_method.values(), Decimal('0')))
    labels = dict(PaymentMethodEntry.Method.choices)
    known = [m for m, _ in PaymentMethodEntry.Method.choices]
    extra = [m for m in by_method if m not in known]
    methods = [
        _slice(labels.get(m, m.title()), by_method.get(m, 0), method_total,
               key=m)
        for m in known + extra
    ]

    received_total = _money(totals['total'])

    return {
        'person': {
            'id': who['id'],
            'kind': kind,
            'key': f"{kind}:{who['id']}",
            'name': who['name'],
            'code': who['code'],
            'subtitle': who['subtitle'],
        },
        'filters': {
            'company': company,
            'preset': preset,
            'date_from': start.isoformat(),
            'date_to': end.isoformat(),
        },
        'kpis': {
            'received_total': received_total,
            'received_count': totals['count'],
            'deposit_total': _money(banked['total']),
            'deposit_collected': _money(banked['collected']),
            'deposit_count': banked['count'],
            'against_invoice': _money(totals['against_invoice']),
            'advance_payment': _money(totals['advance']),
        },
        'charts': {
            'received': {
                'total': received_total,
                'slices': [
                    _slice('Against Invoice', totals['against_invoice'],
                           received_total, key='AGAINST_INVOICE'),
                    _slice('Advance Payment', totals['advance'],
                           received_total, key='ADVANCE'),
                ],
            },
            'methods': {'total': method_total, 'slices': methods},
        },
        'timeline': _daily_series(receipts, deposits, start, end),
        'recent_activity': _recent_activity(receipts, deposits),
    }
