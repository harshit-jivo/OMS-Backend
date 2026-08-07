"""Aggregations behind the Payments Dashboard.

One request produces every number on the screen: five KPI cards, three donut
charts and the collection-performance table. Kept in one place, and in one
round of queries, because the alternative — a view per widget — means the cards
and the charts can disagree with each other when a receipt is posted between
two calls.

Every total is computed by the DATABASE (`Sum`/`Count`), not by pulling rows
into Python and adding them up. The rest of this app sums in Python because it
is always working with one document's few lines; a dashboard spans every
receipt ever raised, and materialising those to add up a column would get
slower every month.

WHAT COUNTS AS WHAT — the definitions the UI labels imply:

  * "Received"       — receipts that represent money actually in hand. Draft,
                       rejected and cancelled receipts are excluded: nothing was
                       received. See COUNTED_RECEIPT_STATUSES.
  * "Against invoice" vs "Advance" — `PaymentReceipt.is_advance`, the stored
                       flag, NOT whether allocations exist. A receipt can be
                       flagged advance and later be allocated in SAP; the flag
                       is what the collector asserted at entry time.
  * "Deposited"      — `BankDeposit.deposit_amount`, the money that reached the
                       bank. NOT `collected_amount`, which is what was handed
                       over before any shortfall.
  * "Total payments" — every receipt in the window regardless of status, which
                       is why it is larger than "Received" and is labelled
                       "all payment receipts".

Amounts are returned as floats. They are money, so Decimal is what the database
and the ORM carry, but JSON has no decimal type and the client formats for
display only — never for arithmetic that is written back.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import (
    BankDeposit,
    CollectionPerson,
    PaymentMethodEntry,
    PaymentReceipt,
)

# Statuses where the money is genuinely in hand. DRAFT has not been submitted,
# REJECTED was refused, CANCELLED was withdrawn, and none of the three mean a
# rupee moved. PENDING_ERROR / SAP_UNKNOWN DO count: the receipt exists and the
# cash was taken; only the SAP posting is unresolved.
COUNTED_RECEIPT_STATUSES = (
    'PENDING_APPROVAL',
    'APPROVED',
    'POSTING_TO_SAP',
    'POSTED',
    'PENDING_ERROR',
    'SAP_UNKNOWN',
)

# Same reasoning for deposits.
COUNTED_DEPOSIT_STATUSES = COUNTED_RECEIPT_STATUSES

# Sum() returns NULL for an empty set, which would serialise as null and force
# every consumer to null-check before formatting. Coalesced to 0 here so the
# dashboard renders "₹0.00" rather than blanking out.
ZERO = Value(Decimal('0'), output_field=DecimalField(max_digits=18,
                                                     decimal_places=2))


def _money(value):
    """Decimal (or None) -> float, rounded to paise."""
    return round(float(value or 0), 2)


def _sum(field):
    return Coalesce(Sum(field), ZERO)


def resolve_range(preset, date_from=None, date_to=None, today=None):
    """Turn a preset name into an inclusive (from, to) pair of dates.

    Resolved on the SERVER. The browser's clock and timezone are not
    trustworthy for "today" — a collector in a different timezone, or with a
    skewed clock, would otherwise see a different day's figures than the
    approver looking at the same screen.
    """
    today = today or date.today()
    preset = (preset or 'today').strip().lower()

    if preset == 'custom':
        # Both ends are required; a half-open custom range is a client bug, and
        # silently substituting today would show numbers nobody asked for.
        return (date_from or today, date_to or today)
    if preset == 'yesterday':
        d = today - timedelta(days=1)
        return (d, d)
    if preset == 'last_7_days':
        # Inclusive of today, so "last 7 days" is 7 days of data, not 8.
        return (today - timedelta(days=6), today)
    if preset == 'last_30_days':
        return (today - timedelta(days=29), today)
    if preset == 'this_month':
        return (today.replace(day=1), today)
    return (today, today)                                    # 'today'


def _receipts(company, start, end):
    qs = PaymentReceipt.objects.filter(
        status__in=COUNTED_RECEIPT_STATUSES,
        payment_date__gte=start,
        payment_date__lte=end,
    )
    return qs.filter(company=company) if company else qs


def _deposits(company, start, end):
    qs = BankDeposit.objects.filter(
        status__in=COUNTED_DEPOSIT_STATUSES,
        deposit_date__gte=start,
        deposit_date__lte=end,
    )
    return qs.filter(company=company) if company else qs


def _kpis(company, start, end):
    """The five headline cards, in two queries."""
    # `total_payments` deliberately ignores the status filter: the card is
    # labelled "all payment receipts", so a draft still counts toward it. Every
    # other figure here is money in hand.
    everything = PaymentReceipt.objects.filter(
        payment_date__gte=start, payment_date__lte=end)
    if company:
        everything = everything.filter(company=company)

    received = _receipts(company, start, end).aggregate(
        total=_sum('total_amount'),
        against_invoice=Coalesce(
            Sum('total_amount', filter=Q(is_advance=False)), ZERO),
        advance=Coalesce(
            Sum('total_amount', filter=Q(is_advance=True)), ZERO),
        count=Count('id'),
    )
    all_totals = everything.aggregate(total=_sum('total_amount'),
                                      count=Count('id'))
    deposits = _deposits(company, start, end).aggregate(
        total=_sum('deposit_amount'),
        collected=_sum('collected_amount'),
        count=Count('id'),
    )

    return {
        'total_payments': _money(all_totals['total']),
        'total_payments_count': all_totals['count'],
        'deposit_total': _money(deposits['total']),
        'deposit_collected': _money(deposits['collected']),
        'deposit_count': deposits['count'],
        'received_total': _money(received['total']),
        'received_count': received['count'],
        'against_invoice': _money(received['against_invoice']),
        'advance_payment': _money(received['advance']),
    }


def _slice(label, amount, total, key=None):
    """One donut segment, with its share pre-computed.

    The percentage is calculated here rather than in the browser so that every
    consumer — the chart, the legend and its tooltip — shows the same rounding
    of the same number.
    """
    amount = _money(amount)
    share = round(amount / total * 100, 1) if total else 0.0
    return {'key': key or label, 'label': label,
            'amount': amount, 'percent': share}


def _received_split(company, start, end, received_total):
    """Donut 1 — against-invoice vs advance."""
    row = _receipts(company, start, end).aggregate(
        against=Coalesce(Sum('total_amount', filter=Q(is_advance=False)), ZERO),
        advance=Coalesce(Sum('total_amount', filter=Q(is_advance=True)), ZERO),
    )
    return [
        _slice('Against Invoice', row['against'], received_total,
               key='AGAINST_INVOICE'),
        _slice('Advance Payment', row['advance'], received_total,
               key='ADVANCE'),
    ]


def _method_split(company, start, end):
    """Donut 2 — how the money arrived (UPI / Cash / Cheque).

    Summed over PaymentMethodEntry, not PaymentReceipt: one receipt can mix
    tenders (cash + cheque in a single collection), so the split lives on the
    method lines. That also means this total equals the receipt total only
    because the model constrains the lines to sum to the header.
    """
    qs = PaymentMethodEntry.objects.filter(
        receipt__status__in=COUNTED_RECEIPT_STATUSES,
        receipt__payment_date__gte=start,
        receipt__payment_date__lte=end,
    )
    if company:
        qs = qs.filter(receipt__company=company)

    rows = qs.values('method').annotate(amount=_sum('amount'))
    by_method = {r['method']: r['amount'] for r in rows}
    total = _money(sum((v for v in by_method.values()), Decimal('0')))

    labels = dict(PaymentMethodEntry.Method.choices)
    # Fixed order so the colours do not shuffle between refreshes. Any method
    # that appears in the data but not in the model choices is still shown,
    # appended after the known ones, rather than silently dropped.
    known = [m for m, _ in PaymentMethodEntry.Method.choices]
    extra = [m for m in by_method if m not in known]

    return [
        _slice(labels.get(m, m.title()), by_method.get(m, 0), total, key=m)
        for m in known + extra
    ], total


def _deposit_split(company, start, end):
    """Donut 3 — cash vs cheque deposits.

    MIXED is a real stored value (a deposit carrying both tenders) and is kept
    as its own segment rather than being forced into one of the other two,
    which would misstate both.
    """
    rows = (_deposits(company, start, end)
            .values('deposit_type')
            .annotate(amount=_sum('deposit_amount')))
    by_type = {r['deposit_type']: r['amount'] for r in rows}
    total = _money(sum((v for v in by_type.values()), Decimal('0')))

    labels = dict(BankDeposit.DepositType.choices)
    known = [t for t, _ in BankDeposit.DepositType.choices]
    extra = [t for t in by_type if t not in known]

    return [
        _slice(labels.get(t, t.title()), by_type.get(t, 0), total, key=t)
        for t in known + extra
    ], total


def _collection_performance(company, start, end, limit=5):
    """The table — top collectors by total collected.

    Ranked on received + deposited together, because someone who collects a lot
    but banks none of it is not performing, and neither is the reverse.

    The percentages are shares of the leader, not of a target: the system holds
    no collection targets, so a "% of target" bar would be inventing its
    denominator. A share-of-best bar is a real comparison, and the tooltip says
    so explicitly.
    """
    receipts = (_receipts(company, start, end)
                .filter(received_from_person__isnull=False)
                .values('received_from_person')
                .annotate(amount=_sum('total_amount'), count=Count('id')))
    deposits = (_deposits(company, start, end)
                .filter(deposited_by__isnull=False)
                .values('deposited_by')
                .annotate(amount=_sum('deposit_amount'), count=Count('id')))

    people = {}
    for row in receipts:
        people.setdefault(row['received_from_person'],
                          {'received': 0.0, 'deposited': 0.0,
                           'receipt_count': 0, 'deposit_count': 0})
        entry = people[row['received_from_person']]
        entry['received'] = _money(row['amount'])
        entry['receipt_count'] = row['count']
    for row in deposits:
        entry = people.setdefault(row['deposited_by'],
                                  {'received': 0.0, 'deposited': 0.0,
                                   'receipt_count': 0, 'deposit_count': 0})
        entry['deposited'] = _money(row['amount'])
        entry['deposit_count'] = row['count']

    if not people:
        return []

    # One query for both columns. Only the people who actually appear in the
    # window are fetched, so this stays small however long the master list is.
    detail = {
        row['id']: row
        for row in CollectionPerson.objects
        .filter(id__in=people)
        .values('id', 'name', 'code')
    }

    rows = [
        {
            'id': pid,
            'name': detail.get(pid, {}).get('name', 'Unknown'),
            'code': detail.get(pid, {}).get('code', ''),
            'received': v['received'],
            'deposited': v['deposited'],
            'total': round(v['received'] + v['deposited'], 2),
            'receipt_count': v['receipt_count'],
            'deposit_count': v['deposit_count'],
        }
        for pid, v in people.items()
    ]
    rows.sort(key=lambda r: r['total'], reverse=True)
    rows = rows[:limit]

    # Bar lengths, as a share of the strongest performer in each column.
    top_received = max((r['received'] for r in rows), default=0) or 0
    top_deposited = max((r['deposited'] for r in rows), default=0) or 0
    for r in rows:
        r['received_percent'] = (
            round(r['received'] / top_received * 100) if top_received else 0)
        r['deposit_percent'] = (
            round(r['deposited'] / top_deposited * 100) if top_deposited else 0)
    return rows


def dashboard(*, company='', preset='today', date_from=None, date_to=None,
              today=None):
    """Every figure the dashboard shows, for one company and date window.

    `company` blank means all companies — the aggregate across OIL, BEVERAGES
    and MART. That is a legitimate view for a head-office user even though the
    three are separate SAP databases, because these totals are OMS-side and
    already denominated in one currency.
    """
    company = (company or '').strip().upper()
    start, end = resolve_range(preset, date_from, date_to, today=today)

    kpis = _kpis(company, start, end)
    received_total = kpis['received_total']

    methods, method_total = _method_split(company, start, end)
    deposit_types, deposit_total = _deposit_split(company, start, end)

    return {
        'filters': {
            'company': company,
            'preset': preset,
            'date_from': start.isoformat(),
            'date_to': end.isoformat(),
        },
        'kpis': kpis,
        'charts': {
            'received': {
                'total': received_total,
                'slices': _received_split(company, start, end, received_total),
            },
            'methods': {'total': method_total, 'slices': methods},
            'deposits': {'total': deposit_total, 'slices': deposit_types},
        },
        'collection_performance': _collection_performance(company, start, end),
    }
