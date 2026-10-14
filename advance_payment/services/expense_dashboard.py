"""The Expense dashboard: what the company actually spent, and what OMS has in hand.

SAP'S ACTUAL EXPENSES come from the same place as Control Panel's Expenses
page — SAP's `REPORT _MONTHLY_EXPENSES` procedure, read through
`cpanel.dashboard.expenses_service` — so the two dashboards agree. Each row is
one expense posting with its budget head, sub budget, variety, G/L, branch and
amount, classified there into five categories (Salaries & HR, Sales &
Marketing, ...). Salaries are left out by default, as on Control Panel, which
shows them on a page of their own.

The procedure lives in the Oil company and covers its two branches (OIL and
BEVERAGES); Mart is not in it. It carries a "current month budget" column that
SAP leaves at 0, so there is no budget-versus-actual here.

OMS'S EXPENSE REQUESTS are the Expense-type payment requests: how many wait at
each stage and for how much, and the latest raised.

Everything is read only. SAP's rows are cached for ten minutes per date range:
a dashboard is opened and re-filtered far more often than the books change.
"""
import calendar
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.core.cache import cache
from django.db.models import Count, Sum

from advance_payment.models import AdvanceRequest, RequestStatus, RequestType

CACHE_SECONDS = 600
#: The months the trend shows, ending with the period's own.
TREND_MONTHS = 12
#: Rows kept per budget head for its drill-down, largest first.
ROWS_PER_HEAD = 60
BRANCHES = ('OIL', 'BEVERAGES')
KINDS = ('all', 'direct', 'indirect')


class DashboardInvalid(Exception):
    """The filters asked for something the dashboard cannot show."""


class SapUnavailable(Exception):
    """SAP could not be read; the dashboard says so rather than show zeros."""


def _date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DashboardInvalid(f'{field} must be a date (YYYY-MM-DD).') from exc


def _rows(start, end):
    """SAP's expense rows for [start, end], cached; SapUnavailable when SAP cannot be read."""
    from cpanel.dashboard.expenses_service import fetch_expense_rows

    key = f'expense-dashboard:rows:{start.isoformat()}:{end.isoformat()}'
    rows = cache.get(key)
    if rows is None:
        try:
            rows = fetch_expense_rows(start.isoformat(), end.isoformat())
        except Exception as exc:  # noqa: BLE001 — any SAP / driver failure
            raise SapUnavailable(f'Could not read the expenses from SAP: {exc}') from exc
        cache.set(key, rows, CACHE_SECONDS)
    return rows


def _keep(rows, *, branch, kind, include_salary):
    out = []
    for r in rows:
        if not include_salary and r.get('is_salary'):
            continue
        if branch and str(r.get('branch') or '').upper() != branch:
            continue
        if kind != 'all' and str(r.get('expense_type') or '').lower() != kind:
            continue
        out.append(r)
    return out


def _total(rows):
    return round(sum(float(r.get('amount') or 0) for r in rows), 2)


def _change(now, before):
    """Percent change, or None when there is nothing to compare with."""
    return round((now - before) / before * 100, 1) if before else None


def _month_start(d, back=0):
    month = d.month - back
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _month_end(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _row(r):
    return {
        'date': r.get('doc_date'), 'doc_num': r.get('doc_num'), 'trans_id': r.get('trans_id'),
        'budget': r.get('budget') or 'Unassigned', 'sub_budget': r.get('sub_budget') or '',
        'variety': r.get('variety') or '', 'account': r.get('account_name') or r.get('ledger') or '',
        'branch': r.get('branch') or '', 'kind': r.get('expense_type') or '', 'category': r.get('category') or '',
        'amount': round(float(r.get('amount') or 0), 2), 'remarks': r.get('remarks') or '',
    }


def _ranked(rows, key, top=None):
    """`[{name, amount, entries, share}]`, largest first, by `key(row)`."""
    amounts, entries = defaultdict(float), Counter()
    for r in rows:
        name = key(r) or 'Unassigned'
        amounts[name] += float(r.get('amount') or 0)
        entries[name] += 1
    total = sum(amounts.values()) or 0
    ranked = sorted(amounts.items(), key=lambda kv: kv[1], reverse=True)
    if top:
        ranked = ranked[:top]
    return [{'name': name, 'amount': round(amount, 2), 'entries': entries[name],
             'share': round(amount / total * 100, 1) if total else 0} for name, amount in ranked]


def _heads(rows, previous):
    """Each budget head: spend, the period before, its accounts and its largest rows."""
    before = defaultdict(float)
    for r in previous:
        before[r.get('budget') or 'Unassigned'] += float(r.get('amount') or 0)
    grouped = defaultdict(list)
    for r in rows:
        grouped[r.get('budget') or 'Unassigned'].append(r)
    total = _total(rows) or 0
    out = []
    for head, items in grouped.items():
        amount = _total(items)
        largest = sorted(items, key=lambda r: float(r.get('amount') or 0), reverse=True)
        out.append({
            'name': head, 'amount': amount, 'previous': round(before[head], 2),
            'change': _change(amount, before[head]), 'entries': len(items),
            'share': round(amount / total * 100, 1) if total else 0,
            'accounts': _ranked(items, lambda r: r.get('account_name') or r.get('ledger'), top=5),
            'sub_budgets': _ranked(items, lambda r: r.get('sub_budget'), top=5),
            'rows': [_row(r) for r in largest[:ROWS_PER_HEAD]],
        })
    return sorted(out, key=lambda h: h['amount'], reverse=True)


def _trend(rows, last_month):
    """Direct and indirect spend per month, for the TREND_MONTHS ending `last_month`."""
    months = [_month_start(last_month, back) for back in range(TREND_MONTHS - 1, -1, -1)]
    buckets = {m.strftime('%Y-%m'): {'direct': 0.0, 'indirect': 0.0} for m in months}
    for r in rows:
        key = str(r.get('doc_date') or '')[:7]
        if key in buckets:
            kind = 'indirect' if str(r.get('expense_type')).lower() == 'indirect' else 'direct'
            buckets[key][kind] += float(r.get('amount') or 0)
    return [{'month': m.strftime('%Y-%m'), 'label': m.strftime('%b %y'),
             'direct': round(buckets[m.strftime('%Y-%m')]['direct'], 2),
             'indirect': round(buckets[m.strftime('%Y-%m')]['indirect'], 2),
             'total': round(sum(buckets[m.strftime('%Y-%m')].values()), 2)} for m in months]


def _oms_requests():
    """OMS's Expense requests: by status, what waits at each stage, the latest."""
    qs = AdvanceRequest.objects.filter(request_type=RequestType.EXPENSE)
    by_status = {row['status']: {'count': row['n'], 'amount': float(row['amount'] or 0)}
                 for row in qs.values('status').annotate(n=Count('id'), amount=Sum('amount'))}
    waiting = (qs.filter(status=RequestStatus.IN_APPROVAL)
               .values('flow__current_stage__name').annotate(n=Count('id'), amount=Sum('amount'))
               .order_by('-amount'))
    latest = (qs.select_related('flow__current_stage', 'created_by').order_by('-created_on')[:8])
    return {
        'by_status': by_status,
        'waiting': [{'stage': row['flow__current_stage__name'] or '—', 'count': row['n'],
                     'amount': float(row['amount'] or 0)} for row in waiting],
        'latest': [{
            'id': a.pk, 'request_no': a.request_no, 'company': a.company,
            'payee': a.partner_name or getattr(a.created_by, 'name', '') or '',
            'budget': a.budget_name or a.budget_code, 'amount': float(a.amount or 0),
            'status': a.status,
            'stage': a.flow.current_stage.name if getattr(a, 'flow', None) and a.flow.current_stage_id else '',
            'created_on': a.created_on.isoformat(),
        } for a in latest],
    }


def build(from_value, to_value, *, branch='', kind='all', include_salary=False, today=None):
    """The whole dashboard for [from, to]. Defaults: this month so far."""
    today = today or date.today()
    start = _date(from_value, 'From') if from_value else today.replace(day=1)
    end = _date(to_value, 'To') if to_value else today
    if end < start:
        raise DashboardInvalid('"To" is before "From".')
    if (end - start).days > 366:
        raise DashboardInvalid('Choose at most a year.')
    branch = (branch or '').upper()
    if branch and branch not in BRANCHES:
        raise DashboardInvalid(f'Branch must be one of {", ".join(BRANCHES)}.')
    kind = (kind or 'all').lower()
    if kind not in KINDS:
        raise DashboardInvalid(f'Kind must be one of {", ".join(KINDS)}.')

    # The same number of days just before, to compare with.
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    trend_start = _month_start(end, TREND_MONTHS - 1)

    keep = dict(branch=branch, kind=kind, include_salary=include_salary)
    all_now = _rows(start, end)
    now = _keep(all_now, **keep)
    previous = _keep(_rows(prev_start, prev_end), **keep)
    trend_rows = _keep(_rows(trend_start, _month_end(end)), **keep)

    total, before = _total(now), _total(previous)
    direct = _total([r for r in now if str(r.get('expense_type')).lower() == 'direct'])
    salary = _total([r for r in _keep(all_now, branch=branch, kind=kind, include_salary=True)
                     if r.get('is_salary')])
    heads = _heads(now, previous)
    largest = sorted(now, key=lambda r: float(r.get('amount') or 0), reverse=True)[:10]
    return {
        'period': {'from': start.isoformat(), 'to': end.isoformat(), 'days': span,
                   'previous_from': prev_start.isoformat(), 'previous_to': prev_end.isoformat()},
        'filters': {'branch': branch, 'kind': kind, 'include_salary': include_salary,
                    'branches': list(BRANCHES)},
        'kpis': {
            'total': total, 'previous': before, 'change': _change(total, before),
            'direct': direct, 'indirect': round(total - direct, 2),
            'entries': len(now), 'per_day': round(total / span, 2),
            'salary': salary, 'salary_included': include_salary,
            'top_head': ({'name': heads[0]['name'], 'amount': heads[0]['amount'], 'share': heads[0]['share']}
                         if heads else None),
        },
        'trend': _trend(trend_rows, end),
        'categories': _ranked(now, lambda r: r.get('category')),
        'heads': heads,
        'accounts': _ranked(now, lambda r: r.get('account_name') or r.get('ledger'), top=10),
        'varieties': _ranked(now, lambda r: r.get('variety'), top=8),
        'branches': _ranked(now, lambda r: r.get('branch')),
        'largest': [_row(r) for r in largest],
        'oms': _oms_requests(),
    }
