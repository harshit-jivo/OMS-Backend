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

from django.contrib.auth import get_user_model
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


DEFAULT_PRESET = 'this_month'


def resolve_range(preset, date_from=None, date_to=None, today=None):
    """Turn a preset name into an inclusive (from, to) pair of dates.

    Resolved on the SERVER. The browser's clock and timezone are not
    trustworthy for "today" — a collector in a different timezone, or with a
    skewed clock, would otherwise see a different day's figures than the
    approver looking at the same screen.
    """
    today = today or date.today()
    preset = (preset or DEFAULT_PRESET).strip().lower()

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
    if preset == 'today':
        return (today, today)
    if preset == 'this_month':
        return (today.replace(day=1), today)
    # Anything unrecognised falls back to the default window rather than
    # erroring, so a stale client cannot break the page.
    return (today.replace(day=1), today)


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


def _blank_entry():
    return {'received': 0.0, 'deposited': 0.0,
            'receipt_count': 0, 'deposit_count': 0,
            'roles': set()}


def _participants(company, start, end):
    """Everyone who took part in the payment workflow, keyed by identity.

    FOUR participation paths, and they do not share an identity type — which is
    why rows are keyed by `(kind, id)` rather than a bare integer:

        collected  PaymentReceipt.received_from_person -> CollectionPerson
        banked     BankDeposit.deposited_by            -> CollectionPerson
        recorded   PaymentReceipt.created_by           -> User
        submitted  BankDeposit.created_by              -> User

    A CollectionPerson and a User can both have id 3 and be different people, so
    merging them into one integer space would silently sum one person's
    collections onto another's. `kind` keeps them apart.

    The same human may still appear twice — once as the login that recorded a
    receipt, once as the collection person it was received from. The system
    holds no link between the two (CollectionPerson.user was dropped as it was
    NULL on every row), so inventing one by name-matching would be a guess. They
    are listed separately, each labelled with what it did.

    `roles` records which of the four a row participated in, so the UI can say
    why someone is listed even when their amounts are zero — a user who recorded
    a receipt that was later rejected still took part.
    """
    people = {}

    def entry(kind, pk):
        return people.setdefault((kind, pk), _blank_entry())

    receipts = _receipts(company, start, end)
    deposits = _deposits(company, start, end)

    # -- Collected: receipts received FROM a collection person ---------------
    for row in (receipts.filter(received_from_person__isnull=False)
                .values('received_from_person')
                .annotate(amount=_sum('total_amount'), count=Count('id'))):
        e = entry('person', row['received_from_person'])
        e['received'] += _money(row['amount'])
        e['receipt_count'] += row['count']
        e['roles'].add('collected')

    # -- Banked: deposits handed over by a collection person -----------------
    for row in (deposits.filter(deposited_by__isnull=False)
                .values('deposited_by')
                .annotate(amount=_sum('deposit_amount'), count=Count('id'))):
        e = entry('person', row['deposited_by'])
        e['deposited'] += _money(row['amount'])
        e['deposit_count'] += row['count']
        e['roles'].add('banked')

    # -- Recorded: the OMS login that raised the receipt ---------------------
    for row in (receipts.filter(created_by__isnull=False)
                .values('created_by')
                .annotate(amount=_sum('total_amount'), count=Count('id'))):
        e = entry('user', row['created_by'])
        e['received'] += _money(row['amount'])
        e['receipt_count'] += row['count']
        e['roles'].add('recorded')

    # -- Submitted: the OMS login that raised the deposit --------------------
    for row in (deposits.filter(created_by__isnull=False)
                .values('created_by')
                .annotate(amount=_sum('deposit_amount'), count=Count('id'))):
        e = entry('user', row['created_by'])
        e['deposited'] += _money(row['amount'])
        e['deposit_count'] += row['count']
        e['roles'].add('submitted')

    return people


ROLE_LABELS = {
    'collected': 'Collected payments',
    'banked': 'Banked deposits',
    'recorded': 'Recorded receipts',
    'submitted': 'Submitted deposits',
}

SORT_FIELDS = {'name', 'received', 'deposited', 'total'}


def _name_lookup(people):
    """(kind, id) -> {name, code} for everyone in the result, in two queries."""
    person_ids = [pk for kind, pk in people if kind == 'person']
    user_ids = [pk for kind, pk in people if kind == 'user']

    names = {}
    for row in (CollectionPerson.objects.filter(id__in=person_ids)
                .values('id', 'name', 'code')):
        names[('person', row['id'])] = {'name': row['name'],
                                        'code': row['code']}

    User = get_user_model()
    # This project's User has a single `name` field, not first/last.
    for row in (User.objects.filter(id__in=user_ids)
                .values('id', 'username', 'name')):
        names[('user', row['id'])] = {
            'name': (row['name'] or '').strip() or row['username'],
            'code': row['username'],
        }
    return names


def collection_performance(company, start, end, *, search='', sort='total',
                           direction='desc', page=1, page_size=25):
    """Every participant, searchable, sortable and paginated.

    Not limited to a top few: the point of the table is to show the whole team,
    and a cut-off would silently hide the people whose figures most need
    looking at.

    Sorted and sliced in PYTHON rather than SQL. The rows come from four
    separate aggregate queries over two tables with two different identity
    types, so there is no single queryset to order or LIMIT. The set is bounded
    by how many people can take part in a company's payment workflow — tens,
    not thousands — so materialising it is cheap and predictable, and it costs
    the same fixed number of queries however large the window.
    """
    people = _participants(company, start, end)
    if not people:
        return {'results': [], 'pagination': {
            'page': 1, 'page_size': page_size, 'total': 0, 'total_pages': 0}}

    names = _name_lookup(people)

    rows = []
    for (kind, pk), v in people.items():
        who = names.get((kind, pk))
        if who is None:
            continue                       # deleted between aggregate and read
        rows.append({
            'id': pk,
            'kind': kind,                  # 'person' | 'user'
            'key': f'{kind}:{pk}',         # stable identity for the client
            'name': who['name'],
            'code': who['code'],
            'received': round(v['received'], 2),
            'deposited': round(v['deposited'], 2),
            'total': round(v['received'] + v['deposited'], 2),
            'receipt_count': v['receipt_count'],
            'deposit_count': v['deposit_count'],
            'roles': sorted(v['roles']),
            'role_labels': [ROLE_LABELS[r] for r in sorted(v['roles'])],
        })

    if search:
        needle = search.strip().lower()
        rows = [r for r in rows
                if needle in r['name'].lower() or needle in r['code'].lower()]

    sort = sort if sort in SORT_FIELDS else 'total'
    reverse = direction != 'asc'
    # Name sorts alphabetically; the money columns sort by magnitude. Name is
    # cased-folded so "amit" and "Amit" do not end up in separate blocks.
    rows.sort(key=(lambda r: r['name'].lower()) if sort == 'name'
              else (lambda r: r[sort]), reverse=reverse)

    # Bars are a share of the strongest performer across the WHOLE result set,
    # not the current page — otherwise the same person's bar would change
    # length depending on which page they landed on.
    top_received = max((r['received'] for r in rows), default=0) or 0
    top_deposited = max((r['deposited'] for r in rows), default=0) or 0
    for r in rows:
        r['received_percent'] = (
            round(r['received'] / top_received * 100) if top_received else 0)
        r['deposit_percent'] = (
            round(r['deposited'] / top_deposited * 100) if top_deposited else 0)

    total = len(rows)
    page_size = max(1, min(int(page_size or 25), 200))
    total_pages = (total + page_size - 1) // page_size
    page = max(1, min(int(page or 1), total_pages or 1))
    start_at = (page - 1) * page_size

    return {
        'results': rows[start_at:start_at + page_size],
        'pagination': {'page': page, 'page_size': page_size,
                       'total': total, 'total_pages': total_pages},
    }


def dashboard(*, company='', preset=DEFAULT_PRESET, date_from=None,
              date_to=None, today=None, search='', sort='total',
              direction='desc', page=1, page_size=25):
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
        'collection_performance': collection_performance(
            company, start, end, search=search, sort=sort,
            direction=direction, page=page, page_size=page_size),
    }
