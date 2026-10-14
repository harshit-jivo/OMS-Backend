"""The Budget board: this month's spending by main budget head, against August's.

THE HEADS are the eight the business reads its spending by (user, 2026-10-14):
Sales, Salary, Freight, Interest & Finance (the user's "accounts or finance"), Factory, Electricity, Back Office
and Rent. SAP's budget dimension does not have all of them — Electricity, Rent
and Freight are accounts booked under many budget heads — so each posting is
put under ONE head, by the first rule that fits (`head_of`):

  Salary        a salary posting (SAP's own salary test), or budget Sal CF
  Electricity   an electricity or generator account
  Rent          a rent account
  Freight       a freight / cartage / transport account, or budget Transprt,
                Del Bkhp, Del Mayp (delivery)
  Finance       budget Interest, or an interest, bank-charges or penalty account
  Sales         budget Sales, Sales RE, ECOM, Med MKT, OTE
  Factory       budget Factory, FACT_COM, NPD1, NPD2, SUPPLY-C
  Back Office   budget BackOff, HR/ADMIN, SERVICES
  Other         anything else (August: only SAP's ₹62 of rounding)

So the heads add up to the whole, and nothing is counted twice.

THE YARDSTICK is August 2026 (`BASELINE`): each head's spending this month is
shown against its August total, and the heads are ranked — given room on the
board — by what they spent in August.

THE DATA is SAP's expense report, `REPORT _MONTHLY_EXPENSES` (Control Panel's
source): the Oil company's, which carries its Oil and Beverages branches, and
Jivo Mart's own. Read only. August is cached for hours (it is closed); the
current month for ten minutes.
"""
import calendar
from collections import defaultdict
from datetime import date

from django.core.cache import cache
from django.utils import timezone

from advance_payment.services.expense_dashboard import DashboardInvalid, SapUnavailable

#: The month every head is measured against (user, 2026-10-14).
BASELINE = '2026-08'
CURRENT_CACHE_SECONDS = 600
CLOSED_CACHE_SECONDS = 6 * 3600

COMPANIES = (('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart'))

#: `(key, name)`, in the order ties are broken.
HEADS = (
    ('SALES', 'Sales'),
    ('SALARY', 'Salary'),
    ('FREIGHT', 'Freight'),
    ('FINANCE', 'Interest & Finance'),  # interest is most of it (Aug: ₹39.8 L of ₹41.6 L)
    ('FACTORY', 'Factory'),
    ('ELECTRICITY', 'Electricity'),
    ('BACKOFFICE', 'Back Office'),
    ('RENT', 'Rent'),
    ('OTHER', 'Other'),
)

#: Who owns a head, by company: Sales has one owner per company (user, 2026-10-14).
OWNERS = {
    'SALES': [{'company': 'OIL', 'label': 'Oil', 'name': 'Raju'},
              {'company': 'MART', 'label': 'Mart', 'name': 'Prabhjot'},
              {'company': 'BEVERAGES', 'label': 'Beverages', 'name': 'Shunty'}],
}

SALES_BUDGETS = {'SALES', 'SALES RE', 'ECOM', 'MED MKT', 'OTE'}
FACTORY_BUDGETS = {'FACTORY', 'FACT_COM', 'NPD1', 'NPD2', 'SUPPLY-C'}
BACKOFFICE_BUDGETS = {'BACKOFF', 'HR/ADMIN', 'SERVICES'}
FREIGHT_BUDGETS = {'TRANSPRT', 'DEL BKHP', 'DEL MAYP'}

#: How much of each level of a head's breakdown is carried, largest first.
TOP_BUDGETS = 15
TOP_ACCOUNTS = 12
TOP_POSTINGS = 8


def head_of(row):
    """The one main head a posting belongs to (the rules in the module's docstring)."""
    account = str(row.get('account_name') or row.get('ledger') or '').upper()
    budget = str(row.get('budget') or '').strip().upper()
    if row.get('is_salary') or budget == 'SAL CF':
        return 'SALARY'
    if 'ELECTRIC' in account or 'GENERATOR' in account:
        return 'ELECTRICITY'
    if 'RENT' in account and 'PARENT' not in account:
        return 'RENT'
    if any(word in account for word in ('FREIGHT', 'CARTAGE', 'TRANSPORT')) or budget in FREIGHT_BUDGETS:
        return 'FREIGHT'
    if budget == 'INTEREST' or any(word in account for word in ('INTEREST', 'BANK CHARGES', 'PENALTY')):
        return 'FINANCE'
    if budget in SALES_BUDGETS:
        return 'SALES'
    if budget in FACTORY_BUDGETS:
        return 'FACTORY'
    if budget in BACKOFFICE_BUDGETS:
        return 'BACKOFFICE'
    return 'OTHER'


def _month(value, field):
    try:
        year, month = (int(part) for part in str(value).split('-'))
        return date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise DashboardInvalid(f'{field} must be a month (YYYY-MM).') from exc


def _month_end(start):
    return start.replace(day=calendar.monthrange(start.year, start.month)[1])


def _rows(start, end, *, closed):
    """Every company's expense rows for [start, end], each with its `company`; cached."""
    from cpanel.core import companies
    from cpanel.dashboard.expenses_service import fetch_expense_rows

    key = f'budget-board:rows:{start.isoformat()}:{end.isoformat()}'
    rows = cache.get(key)
    if rows is not None:
        return rows
    rows = []
    try:
        # The Oil company's report: its Oil and Beverages branches.
        for r in fetch_expense_rows(start.isoformat(), end.isoformat()):
            branch = str(r.get('branch') or '').upper()
            rows.append({**r, 'company': 'BEVERAGES' if branch == 'BEVERAGES' else 'OIL'})
        if companies.MART:
            for r in fetch_expense_rows(start.isoformat(), end.isoformat(), schema=companies.MART):
                rows.append({**r, 'company': 'MART'})
    except Exception as exc:  # noqa: BLE001 — any SAP / driver failure
        raise SapUnavailable(f'Could not read the expenses from SAP: {exc}') from exc
    cache.set(key, rows, CLOSED_CACHE_SECONDS if closed else CURRENT_CACHE_SECONDS)
    return rows


def _amount(row):
    return float(row.get('amount') or 0)


def _money(value):
    return round(value, 2)


def _ratio(now, base):
    return round(now / base, 4) if base else None


def _budget_label(row):
    budget = str(row.get('budget') or '').strip() or 'No budget head'
    sub = str(row.get('sub_budget') or '').strip()
    return f'{budget} › {sub}' if sub else budget


def _account_label(row):
    return str(row.get('account_name') or row.get('ledger') or '—').strip()


def _grouped(base_rows, cur_rows, key, top):
    """`[(name, base_rows, cur_rows)]` by `key`, largest first (by the bigger month), at most `top`."""
    base, cur = defaultdict(list), defaultdict(list)
    for r in base_rows:
        base[key(r)].append(r)
    for r in cur_rows:
        cur[key(r)].append(r)
    total = lambda rows: sum(_amount(r) for r in rows)  # noqa: E731
    names = sorted(set(base) | set(cur), key=lambda n: -max(abs(total(base[n])), abs(total(cur[n]))))
    return [(n, base[n], cur[n]) for n in names[:top]]


def _figures(name, base_rows, cur_rows):
    base, cur = sum(_amount(r) for r in base_rows), sum(_amount(r) for r in cur_rows)
    return {'name': name, 'base': _money(base), 'current': _money(cur), 'entries': len(cur_rows)}


def _tree(base_rows, cur_rows):
    """What a head is made of, to open level by level: SAP budget › sub budget,
    then its G/L accounts, then that account's largest postings this month."""
    return [{
        **_figures(budget, b_rows, c_rows),
        'accounts': [{
            **_figures(account, ba_rows, ca_rows),
            'postings': [{
                'date': r.get('doc_date'), 'company': r['company'], 'doc_num': r.get('doc_num'),
                'remarks': r.get('remarks') or '', 'amount': _money(_amount(r)),
            } for r in sorted(ca_rows, key=lambda r: -abs(_amount(r)))[:TOP_POSTINGS]],
        } for account, ba_rows, ca_rows in _grouped(b_rows, c_rows, _account_label, TOP_ACCOUNTS)],
    } for budget, b_rows, c_rows in _grouped(base_rows, cur_rows, _budget_label, TOP_BUDGETS)]


def _head(key, name, base_rows, cur_rows):
    base_total = sum(_amount(r) for r in base_rows)
    cur_total = sum(_amount(r) for r in cur_rows)
    by_company = []
    for code, label in COMPANIES:
        b = sum(_amount(r) for r in base_rows if r['company'] == code)
        c = sum(_amount(r) for r in cur_rows if r['company'] == code)
        if b or c:
            by_company.append({'company': code, 'label': label, 'base': _money(b), 'current': _money(c)})
    return {
        'key': key,
        'name': name,
        'owners': OWNERS.get(key, []),
        'base': _money(base_total),
        'current': _money(cur_total),
        'ratio': _ratio(cur_total, base_total),
        'entries': len(cur_rows),
        'by_company': by_company,
        'tree': _tree(base_rows, cur_rows),
    }


def build(month='', baseline='', *, today=None):
    """The board: every main head's spending in `month` (default: this month, to today) against `baseline`'s."""
    today = today or timezone.localdate()
    cur_start = _month(month, 'Month') if month else today.replace(day=1)
    base_start = _month(baseline or BASELINE, 'Baseline')
    if cur_start > today:
        raise DashboardInvalid('That month has not started yet.')
    cur_month_end = _month_end(cur_start)
    cur_end = min(cur_month_end, today)
    base_end = _month_end(base_start)
    cur_days = cur_month_end.day
    elapsed = (cur_end - cur_start).days + 1

    base_rows = _rows(base_start, base_end, closed=base_end < today)
    cur_rows = _rows(cur_start, cur_end, closed=cur_end == cur_month_end and cur_end < today)
    base_by, cur_by = defaultdict(list), defaultdict(list)
    for r in base_rows:
        base_by[head_of(r)].append(r)
    for r in cur_rows:
        cur_by[head_of(r)].append(r)

    order = {key: i for i, (key, _n) in enumerate(HEADS)}
    heads = [_head(key, name, base_by[key], cur_by[key]) for key, name in HEADS]
    # Room on the board by what each spent in the yardstick month; Other always last.
    heads.sort(key=lambda h: (h['key'] == 'OTHER', -h['base'], order[h['key']]))

    base_total = sum(h['base'] for h in heads)
    cur_total = sum(h['current'] for h in heads)
    return {
        'current': {'month': cur_start.strftime('%Y-%m'), 'label': cur_start.strftime('%b %Y'),
                    'from': cur_start.isoformat(), 'to': cur_end.isoformat(),
                    'days': cur_days, 'elapsed': elapsed, 'pace': round(elapsed / cur_days, 4)},
        'baseline': {'month': base_start.strftime('%Y-%m'), 'label': base_start.strftime('%b %Y'),
                     'from': base_start.isoformat(), 'to': base_end.isoformat()},
        'total': {'base': _money(base_total), 'current': _money(cur_total), 'ratio': _ratio(cur_total, base_total)},
        'heads': heads,
    }
