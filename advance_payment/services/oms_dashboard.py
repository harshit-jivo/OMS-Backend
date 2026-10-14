"""The OMS side of the Payments & Expenses board: the payment requests raised in OMS.

Read only, from OMS's own tables (no SAP). Two kinds of figure, kept apart:

- FLOW, for the chosen period: what was RAISED in it (by when the request was
  created) and what was PAID in it (by when it completed — Final's or the
  Expense auditor's approval, which posts it to SAP). Compared with the same
  number of days just before.
- STOCK, as it stands now, whatever the period: what is still pending, where it
  waits, with whom, and for how long.

Amounts are each request's own amount (an Expense's: the total of its lines).
"""
import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from advance_payment.models import AdvanceRequest, LogAction, RequestLog, RequestStatus, RequestType
from advance_payment.services.expense_dashboard import DashboardInvalid, _oms_requests

TREND_MONTHS = 12
COMPANIES = ('OIL', 'BEVERAGES', 'MART')
PENDING = (RequestStatus.IN_APPROVAL, RequestStatus.RETURNED)
#: How long a pending request has waited since it was raised, in days.
AGE_BUCKETS = ((0, 2, 'Under 2 days'), (2, 7, '2–7 days'), (7, 15, '7–15 days'), (15, None, 'Over 15 days'))
TYPE_LABELS = dict(RequestType.choices)


def _date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DashboardInvalid(f'{field} must be a date (YYYY-MM-DD).') from exc


def _bounds(start, end):
    """Aware datetimes covering the days [start, end]."""
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))


def _change(now, before):
    return round((now - before) / before * 100, 1) if before else None


def _money(value):
    return round(float(value or 0), 2)


def bill_or_advance(payment_against):
    """The approver's word for it: a Bill already received, or an Advance paid before it."""
    if payment_against == 'AGAINST_BILL':
        return 'Bill'
    if payment_against in ('AGAINST_PO', 'ADVANCE'):
        return 'Advance'
    if payment_against in ('DIRECT_EXPENSE', 'INDIRECT_EXPENSE'):
        return 'Expense'
    return 'Other'


def purpose_of(r):
    """The payment purpose as raised; an Expense has none of its own, it is an expense."""
    if r.purpose_label or r.purpose_code:
        return r.purpose_label or r.purpose_code
    return 'Expense' if r.request_type == RequestType.EXPENSE else 'No purpose'


def budget_head_of(r):
    return r.budget_name or r.budget_code or 'No budget head'


def _ranked(rows, key, top=None):
    amounts, counts = defaultdict(float), Counter()
    for r in rows:
        name = key(r) or '—'
        amounts[name] += float(r.amount or 0)
        counts[name] += 1
    total = sum(amounts.values()) or 0
    ranked = sorted(amounts.items(), key=lambda kv: kv[1], reverse=True)[:top] if top else \
        sorted(amounts.items(), key=lambda kv: kv[1], reverse=True)
    return [{'name': n, 'amount': round(a, 2), 'entries': counts[n],
             'share': round(a / total * 100, 1) if total else 0} for n, a in ranked]


def _paid(qs, start, end):
    """Requests that completed in [start, end]: `{request_id: completed_at}`."""
    lo, hi = _bounds(start, end)
    return dict(RequestLog.objects.filter(request__in=qs, action=LogAction.COMPLETED,
                                          created_on__gte=lo, created_on__lt=hi)
                .values_list('request_id', 'created_on'))


def _month_start(d, back=0):
    month, year = d.month - back, d.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def build(from_value, to_value, *, company='', today=None):
    """The OMS board for [from, to]. Defaults: this month so far."""
    today = today or timezone.localdate()
    start = _date(from_value, 'From') if from_value else today.replace(day=1)
    end = _date(to_value, 'To') if to_value else today
    if end < start:
        raise DashboardInvalid('"To" is before "From".')
    if (end - start).days > 366:
        raise DashboardInvalid('Choose at most a year.')
    company = (company or '').upper()
    if company and company not in COMPANIES:
        raise DashboardInvalid(f'Company must be one of {", ".join(COMPANIES)}.')

    every = AdvanceRequest.objects.all()
    if company:
        every = every.filter(company=company)
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)

    lo, hi = _bounds(start, end)
    plo, phi = _bounds(prev_start, prev_end)
    raised = list(every.filter(created_on__gte=lo, created_on__lt=hi).select_related('created_by'))
    raised_before = every.filter(created_on__gte=plo, created_on__lt=phi).aggregate(n=Count('id'), amount=Sum('amount'))

    paid_at = _paid(every, start, end)
    paid = list(every.filter(pk__in=list(paid_at)))
    paid_before = every.filter(pk__in=list(_paid(every, prev_start, prev_end))).aggregate(n=Count('id'), amount=Sum('amount'))
    days_to_pay = [(paid_at[r.pk] - r.created_on).total_seconds() / 86400 for r in paid]

    rejected = every.filter(status=RequestStatus.REJECTED, updated_on__gte=lo, updated_on__lt=hi) \
        .aggregate(n=Count('id'), amount=Sum('amount'))

    # Stock: what is pending now, whatever the period.
    now = timezone.now()
    pending = list(every.filter(status__in=PENDING)
                   .select_related('flow__current_stage', 'flow__current_user', 'created_by'))

    def waiting_where(r):
        if r.status == RequestStatus.RETURNED:
            return 'With creator'
        flow = getattr(r, 'flow', None)
        return flow.current_stage.name if flow and flow.current_stage_id else '—'

    def waiting_on(r):
        if r.status == RequestStatus.RETURNED:
            return getattr(r.created_by, 'name', '') or getattr(r.created_by, 'username', '') or 'Creator'
        flow = getattr(r, 'flow', None)
        user = flow.current_user if flow and flow.current_user_id else None
        return (getattr(user, 'name', '') or getattr(user, 'username', '')) if user else '—'

    def age(r):
        return (now - r.created_on).total_seconds() / 86400

    ageing = []
    for low, high, label in AGE_BUCKETS:
        items = [r for r in pending if age(r) >= low and (high is None or age(r) < high)]
        ageing.append({'name': label, 'entries': len(items), 'amount': round(sum(float(r.amount or 0) for r in items), 2)})
    oldest = sorted(pending, key=age, reverse=True)[:8]

    # Raised vs paid, month by month, over the twelve months ending with the period's.
    trend_start = _month_start(end, TREND_MONTHS - 1)
    tlo, thi = _bounds(trend_start, date(end.year, end.month, calendar.monthrange(end.year, end.month)[1]))
    months = [_month_start(end, back) for back in range(TREND_MONTHS - 1, -1, -1)]
    raised_by = defaultdict(float)
    for created, amount in every.filter(created_on__gte=tlo, created_on__lt=thi).values_list('created_on', 'amount'):
        raised_by[timezone.localtime(created).strftime('%Y-%m')] += float(amount or 0)
    paid_by = defaultdict(float)
    completed = RequestLog.objects.filter(request__in=every, action=LogAction.COMPLETED,
                                          created_on__gte=tlo, created_on__lt=thi) \
        .values_list('created_on', 'request__amount')
    for when, amount in completed:
        paid_by[timezone.localtime(when).strftime('%Y-%m')] += float(amount or 0)
    trend = [{'month': m.strftime('%Y-%m'), 'label': m.strftime('%b %y'),
              'raised': round(raised_by[m.strftime('%Y-%m')], 2), 'paid': round(paid_by[m.strftime('%Y-%m')], 2)}
             for m in months]

    raised_amount = round(sum(float(r.amount or 0) for r in raised), 2)
    paid_amount = round(sum(float(r.amount or 0) for r in paid), 2)
    return {
        'period': {'from': start.isoformat(), 'to': end.isoformat(), 'days': span,
                   'previous_from': prev_start.isoformat(), 'previous_to': prev_end.isoformat()},
        'filters': {'company': company, 'companies': list(COMPANIES)},
        'kpis': {
            'raised': {'count': len(raised), 'amount': raised_amount,
                       'previous': _money(raised_before['amount']),
                       'change': _change(raised_amount, float(raised_before['amount'] or 0))},
            'paid': {'count': len(paid), 'amount': paid_amount, 'previous': _money(paid_before['amount']),
                     'change': _change(paid_amount, float(paid_before['amount'] or 0))},
            'pending': {'count': len(pending), 'amount': round(sum(float(r.amount or 0) for r in pending), 2),
                        'returned': sum(1 for r in pending if r.status == RequestStatus.RETURNED)},
            'rejected': {'count': rejected['n'] or 0, 'amount': _money(rejected['amount'])},
            'days_to_pay': round(sum(days_to_pay) / len(days_to_pay), 1) if days_to_pay else None,
        },
        'trend': trend,
        'by_type': _ranked(raised, lambda r: TYPE_LABELS.get(r.request_type, r.request_type)),
        'bill_or_advance': _ranked(raised, lambda r: bill_or_advance(r.payment_against)),
        'by_company': _ranked(raised, lambda r: r.company),
        'top_payees': _ranked(raised, lambda r: r.partner_name or getattr(r.created_by, 'name', ''), top=8),
        # What it was for, and whose budget it comes out of.
        'by_purpose': _ranked(raised, purpose_of, top=12),
        'by_budget': _ranked(raised, budget_head_of, top=12),
        'waiting_at': _ranked(pending, waiting_where),
        'waiting_on': _ranked(pending, waiting_on, top=8),
        'ageing': ageing,
        'oldest': [{
            'id': r.pk, 'request_no': r.request_no, 'company': r.company,
            'payee': r.partner_name or getattr(r.created_by, 'name', ''),
            'type': TYPE_LABELS.get(r.request_type, r.request_type), 'kind': bill_or_advance(r.payment_against),
            'amount': _money(r.amount), 'stage': waiting_where(r), 'waiting_on': waiting_on(r),
            'days': round(age(r), 1),
        } for r in oldest],
        'expenses': _oms_requests(),
    }
