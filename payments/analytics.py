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

  * SCOPE            — SAP-POSTED DOCUMENTS ONLY. Every figure on this screen
                       describes money settled in the books of record. Anything
                       still in the OMS pipeline (draft, awaiting approval,
                       mid-post) or that SAP refused or never confirmed is
                       excluded. See COUNTED_RECEIPT_STATUSES for each reason.
                       The operational tracking screens answer the other
                       question — what is outstanding.
  * "Against invoice" vs "Advance" — `PaymentReceipt.is_advance`, the stored
                       flag, NOT whether allocations exist. A receipt can be
                       flagged advance and later be allocated in SAP; the flag
                       is what the collector asserted at entry time.
  * "Deposited"      — `BankDeposit.deposit_amount`, the money that reached the
                       bank. NOT `collected_amount`, which is what was handed
                       over before any shortfall.
  * "Total payments" — every POSTED receipt in the window. It shares the posted
                       filter with everything else so no card on the screen
                       answers a different question from its neighbours.

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

# ONLY documents that reached SAP successfully.
#
# The dashboard reports money that is settled in the books of record, not money
# that is somewhere in the OMS pipeline. Everything else is excluded and each
# for its own reason:
#
#   DRAFT / PENDING_APPROVAL / APPROVED / POSTING_TO_SAP
#       still in flight — no SAP document exists yet, and an approval can still
#       be refused, so counting these would report revenue that may never land.
#   REJECTED / CANCELLED
#       refused or withdrawn; nothing moved.
#   PENDING_ERROR
#       SAP ANSWERED with an error, so nothing was committed there.
#   SAP_UNKNOWN
#       SAP never answered. The document may or may not exist. Counting an
#       unconfirmed posting is the one error a finance dashboard must not make,
#       because it cannot be told apart from a real one by looking.
#
# A receipt sitting in PENDING_ERROR is still real work and the cash may well be
# in someone's hand — but this screen answers "what is posted in SAP", and the
# operational tracking screens answer "what is outstanding". Conflating the two
# is what makes a dashboard stop being trusted.
COUNTED_RECEIPT_STATUSES = ('POSTED',)

# Same rule for deposits: banked and confirmed by SAP, or not counted.
COUNTED_DEPOSIT_STATUSES = ('POSTED',)

# Raised but NOT yet settled in SAP — the counterpart to the figures above.
#
# Restricting the dashboard to posted documents makes the totals trustworthy but
# would otherwise hide real work: a receipt SAP refused this morning is money
# somebody is holding, and a screen that simply omitted it would understate the
# day with no hint that anything was missing. These statuses are surfaced as
# their own cards so the two questions stay separate and both get answered.
#
# REJECTED and CANCELLED are NOT pending: they were refused or withdrawn, so
# nobody is waiting on them and nothing will ever post.
PENDING_STATUSES = (
    'DRAFT',
    'PENDING_APPROVAL',
    'APPROVED',
    'POSTING_TO_SAP',
    'PENDING_ERROR',
    'SAP_UNKNOWN',
)

# The subset that needs a human to look. The rest advance on their own as the
# approval chain moves; these two are stuck until someone acts:
#   PENDING_ERROR  SAP refused it — correct and resubmit.
#   SAP_UNKNOWN    SAP never answered — must be reconciled before retrying, or
#                  the payment could post twice.
BLOCKED_STATUSES = ('PENDING_ERROR', 'SAP_UNKNOWN')

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


# A rolling 30-day window rather than the calendar month: on the 1st of a month
# "this month" is a single day of data, so the dashboard opened almost empty and
# every reader's first action was to widen the range.
DEFAULT_PRESET = 'last_30_days'


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
    # erroring, so a stale client cannot break the page. Derived from
    # DEFAULT_PRESET so the fallback cannot drift away from it.
    return (today - timedelta(days=29), today)


def _receipts(company, start, end, statuses=COUNTED_RECEIPT_STATUSES):
    qs = PaymentReceipt.objects.filter(
        status__in=statuses,
        payment_date__gte=start,
        payment_date__lte=end,
    )
    return qs.filter(company=company) if company else qs


def _deposits(company, start, end, statuses=COUNTED_DEPOSIT_STATUSES):
    qs = BankDeposit.objects.filter(
        status__in=statuses,
        deposit_date__gte=start,
        deposit_date__lte=end,
    )
    return qs.filter(company=company) if company else qs


def _pending(company, start, end):
    """Receipts and deposits raised but not yet settled in SAP.

    Two aggregates rather than one combined figure: a pending receipt and a
    pending deposit are different problems for different people, and adding
    them would also double-count the same money where a receipt has been
    banked but neither document has posted.
    """
    receipts = _receipts(company, start, end, PENDING_STATUSES).aggregate(
        total=_sum('total_amount'),
        count=Count('id'),
        blocked=Coalesce(
            Sum('total_amount', filter=Q(status__in=BLOCKED_STATUSES)), ZERO),
        blocked_count=Count('id', filter=Q(status__in=BLOCKED_STATUSES)),
    )
    deposits = _deposits(company, start, end, PENDING_STATUSES).aggregate(
        total=_sum('deposit_amount'),
        count=Count('id'),
        blocked=Coalesce(
            Sum('deposit_amount', filter=Q(status__in=BLOCKED_STATUSES)), ZERO),
        blocked_count=Count('id', filter=Q(status__in=BLOCKED_STATUSES)),
    )
    return {
        'pending_receipts': _money(receipts['total']),
        'pending_receipts_count': receipts['count'],
        'pending_deposits': _money(deposits['total']),
        'pending_deposits_count': deposits['count'],
        # Needs someone to act, as opposed to simply waiting its turn in the
        # approval chain. This is the number worth chasing.
        'blocked_total': _money(receipts['blocked'] + deposits['blocked']),
        'blocked_count': receipts['blocked_count'] + deposits['blocked_count'],
    }


def _kpis(company, start, end):
    """The five headline cards, in two queries.

    `total_payments` counts the SAME posted-only set as everything else. It once
    included drafts and rejections, on the reasoning that the card said "all
    payment receipts" — but with every other figure now restricted to what SAP
    confirmed, that one card would be the only number on the screen answering a
    different question, and the first thing a reader would do is try to
    reconcile it against Received Total and fail.

    It still differs from Received Total in a useful way: this is every posted
    receipt, Received Total is the money side of the same set, so the two agree
    while the counts show whether any posted receipt carries no value.
    """
    received = _receipts(company, start, end).aggregate(
        total=_sum('total_amount'),
        against_invoice=Coalesce(
            Sum('total_amount', filter=Q(is_advance=False)), ZERO),
        advance=Coalesce(
            Sum('total_amount', filter=Q(is_advance=True)), ZERO),
        count=Count('id'),
        # Counted alongside the sums so the Against Invoice and Advance cards
        # can state a receipt count like every other card, instead of prose
        # that repeats what their tooltip already says.
        against_invoice_count=Count('id', filter=Q(is_advance=False)),
        advance_count=Count('id', filter=Q(is_advance=True)),
    )
    deposits = _deposits(company, start, end).aggregate(
        total=_sum('deposit_amount'),
        collected=_sum('collected_amount'),
        count=Count('id'),
    )

    return {
        'total_payments': _money(received['total']),
        'total_payments_count': received['count'],
        'deposit_total': _money(deposits['total']),
        'deposit_collected': _money(deposits['collected']),
        'deposit_count': deposits['count'],
        'received_total': _money(received['total']),
        'received_count': received['count'],
        'against_invoice': _money(received['against_invoice']),
        'against_invoice_count': received['against_invoice_count'],
        'advance_payment': _money(received['advance']),
        'advance_count': received['advance_count'],
        **_pending(company, start, end),
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


#: A handover is late once the money has been in someone's hand this long.
#: Matches the verification queue's own default window, so "still in the queue
#: when it opens" and "late" mean the same number of days.
VERIFICATION_SLA_DAYS = 2


def _verification_delays(company, start, end):
    """Per creator: how long their collections wait to be verified.

    Measured from `payment_date` — the day the collector actually took the
    money — to `verified_at`. NOT from `created_at`: cash collected on Monday
    and typed up on Thursday sat in a pocket for three days, and an
    entry-to-verification figure would score that as same-day.

    Deliberately NOT restricted to POSTED receipts (unlike every other figure
    on this dashboard). The whole point is money that has NOT completed its
    journey — a receipt still waiting to be verified is exactly the case this
    exists to surface, and filtering to posted would hide it.

    Still-unverified receipts are measured against TODAY, so an entry nobody
    has touched for a week reads as seven days late rather than as absent.
    """
    from django.utils import timezone

    receipts = PaymentReceipt.objects.filter(
        payment_date__gte=start,
        payment_date__lte=end,
    ).exclude(status=PaymentReceipt.Status.CANCELLED)
    if company:
        receipts = receipts.filter(company=company)

    today = timezone.localdate()
    # Keyed by BOTH identities — ('person', id) for who the money came from,
    # ('user', id) for the login that recorded it — so the same figure serves
    # whichever view the table is showing. A receipt contributes to both, and
    # they are different questions: how long the collector held the cash, and
    # how long that operator's entries take to clear.
    by_key = {}
    for person_id, user_id, payment_date, verified_at in receipts.values_list(
        'received_from_person', 'created_by', 'payment_date', 'verified_at',
    ):
        if payment_date is None:
            continue
        # An unverified receipt is measured to today — it is still waiting.
        end_date = timezone.localtime(verified_at).date() if verified_at else today
        days = (end_date - payment_date).days
        # A negative span means the payment_date was entered in the future;
        # clamped rather than dropped, so the row still counts as same-day.
        days = max(days, 0)

        for key in (('person', person_id), ('user', user_id)):
            if key[1] is None:
                continue
            stats = by_key.setdefault(
                key, {'total_days': 0, 'count': 0, 'late': 0, 'worst': 0,
                      'pending': 0})
            stats['total_days'] += days
            stats['count'] += 1
            stats['worst'] = max(stats['worst'], days)
            if days > VERIFICATION_SLA_DAYS:
                stats['late'] += 1
            if verified_at is None:
                stats['pending'] += 1
    return by_key


ROLE_LABELS = {
    'collected': 'Collected payments',
    'banked': 'Banked deposits',
    'recorded': 'Recorded receipts',
    'submitted': 'Submitted deposits',
}

SORT_FIELDS = {'name', 'received', 'deposited', 'total', 'avg_verify_days'}


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
                           direction='desc', page=1, page_size=25,
                           participants='person'):
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

    # WHO the table is about: the collection people named on the documents —
    # who the money was received FROM and who banked it — not the OMS logins
    # that typed the entries.
    #
    # An admin reading this asks "how much has Goldy collected and banked, and
    # when". Listing the login as well answered a different question next to
    # it, and double-counted the same money under two names: a receipt raised
    # by `gagan_P&D` from `Goldy` appeared in full on both rows, so the column
    # summed to roughly twice the money that exists.
    #
    # `participants='user'` keeps the old view for anyone who wants
    # per-operator activity, and 'all' restores the mixed list.
    if participants in ('person', 'user'):
        people = {k: v for k, v in people.items() if k[0] == participants}

    if not people:
        # The SLA is published on this branch too: a client that reads it once
        # to configure its table must not get a different shape back just
        # because a window happened to be empty.
        return {'results': [],
                'pagination': {'page': 1, 'page_size': page_size,
                               'total': 0, 'total_pages': 0},
                'verification_sla_days': VERIFICATION_SLA_DAYS}

    names = _name_lookup(people)
    # How long this participant's collections wait for their handover check,
    # keyed the same way the rows are — so it answers for the collection
    # person on a person row and for the login on a user row.
    delays = _verification_delays(company, start, end)

    rows = []
    for (kind, pk), v in people.items():
        who = names.get((kind, pk))
        if who is None:
            continue                       # deleted between aggregate and read
        d = delays.get((kind, pk))
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
            # Days from collection to verification. Null for a collection
            # person, and for a user who recorded nothing in the window — a
            # zero there would read as "always same-day", which is a claim.
            'avg_verify_days': (
                round(d['total_days'] / d['count'], 1)
                if d and d['count'] else None
            ),
            'worst_verify_days': d['worst'] if d else None,
            # Handed over later than the SLA allows.
            'late_verify_count': d['late'] if d else None,
            # Still not verified at all — the money is outstanding right now.
            'pending_verify_count': d['pending'] if d else None,
        })

    if search:
        needle = search.strip().lower()
        rows = [r for r in rows
                if needle in r['name'].lower() or needle in r['code'].lower()]

    sort = sort if sort in SORT_FIELDS else 'total'
    reverse = direction != 'asc'
    # Name sorts alphabetically; the money and delay columns sort by magnitude.
    # Name is case-folded so "amit" and "Amit" do not end up in separate blocks.
    #
    # `avg_verify_days` is None for a collection person and for anyone who
    # recorded nothing, which cannot be compared against a float. Those rows
    # sort as -1 so they sit at the bottom of a descending "slowest first"
    # view rather than raising a TypeError.
    if sort == 'name':
        key = lambda r: r['name'].lower()                       # noqa: E731
    elif sort == 'avg_verify_days':
        key = lambda r: (r['avg_verify_days'] if r['avg_verify_days']  # noqa: E731
                         is not None else -1)
    else:
        key = lambda r: r[sort]                                 # noqa: E731
    rows.sort(key=key, reverse=reverse)

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
        # Published so the client colours a late figure against the SAME
        # threshold the server counted `late_verify_count` with, rather than
        # hardcoding its own and drifting.
        'verification_sla_days': VERIFICATION_SLA_DAYS,
    }


def dashboard(*, company='', preset=DEFAULT_PRESET, date_from=None,
              date_to=None, today=None, search='', sort='total',
              direction='desc', page=1, page_size=25, participants='all'):
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
        # `participants='all'` here, unlike the standalone endpoint: this
        # bundled payload is the whole dashboard, and narrowing it would
        # change a shape other callers already read. The dedicated
        # /collection-performance/ endpoint is where the person-only view
        # lives, and the web table calls that.
        'collection_performance': collection_performance(
            company, start, end, search=search, sort=sort,
            direction=direction, page=page, page_size=page_size,
            participants=participants),
    }
