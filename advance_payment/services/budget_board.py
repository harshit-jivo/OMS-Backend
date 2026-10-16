"""The Budget board: this month's spending against August's — by budget head, and by kind of cost.

TWO CUTS OF THE SAME POSTINGS (user, 2026-10-14):

THE HEADS (`heads`, the board's rows) are by SAP's budget dimension — whose
budget a posting was booked to, salaries, rent and power included — so they
add up to the whole (`department_of`):

  Sales               Sales, Sales RE, ECOM, Med MKT, OTE
  Factory             Factory (but its CIVIL), FACT_COM
  Back Office         BackOff, HR/ADMIN, SERVICES, Sal CF
  Freight (delivery)  Del Bkhp, Del Mayp, Transprt, SUPPLY-C (Mart's supply chain)
  Bank Interest       Interest
  Other               anything else (August: SAP's ₹62 of rounding)

THE COSTS (`costs`, the side's pie) cut across those budgets by what the money
was for — each posting under the first rule that fits (`cost_of`), and only
these five shown:

  Salary              a salary posting (SAP's own salary test — incentive to
                      employees among them), TA/DA, or budget Sal CF
  Electricity         an electricity or generator account
  Rent                a rent account
  Freight             a freight / cartage / transport account, or budget
                      Transprt, Del Bkhp, Del Mayp
  Interest & Finance  budget Interest, or an interest, bank-charges or penalty account

MART COUNTS ONLY THROUGH ITS SALARY AND FREIGHT (user, 2026-10-14) — its
salaries under Sales, its freight under Freight (delivery) — and every
other Mart posting — its sales promotion, supply chain, office costs, rent,
interest — is left off the board altogether, heads, costs and total alike
(`counted`). Oil and Beverages count in full — but for new-product development
(NPD1, NPD2) and factory civil works (Factory › CIVIL), which the board leaves
out too (user, 2026-10-14; `LEFT_OUT`).

THE YARDSTICK is August 2026 (`BASELINE`): every figure this month is shown
against August's.

THE DATA is SAP's expense report, `REPORT _MONTHLY_EXPENSES` (Control Panel's
source): the Oil company's, which carries its Oil and Beverages branches, and
Jivo Mart's own. Read only. August is cached for hours (it is closed); the
current month for ten minutes.
"""
import calendar
import re
from collections import defaultdict
from datetime import date

from django.core.cache import cache
from django.utils import timezone

from advance_payment.services.expense_dashboard import DashboardInvalid, SapUnavailable

#: The month every figure is measured against (user, 2026-10-14).
BASELINE = '2026-08'
CURRENT_CACHE_SECONDS = 600
CLOSED_CACHE_SECONDS = 6 * 3600

COMPANIES = (('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart'))

#: The board's rows: `(key, name, SAP budget heads)` — every budget in one, so they add up.
DEPARTMENTS = (
    ('SALES', 'Sales', {'SALES', 'SALES RE', 'ECOM', 'MED MKT', 'OTE'}),
    ('FACTORY', 'Factory', {'FACTORY', 'FACT_COM'}),
    ('BACKOFFICE', 'Back Office', {'BACKOFF', 'HR/ADMIN', 'SERVICES', 'SAL CF'}),
    ('DELIVERY', 'Freight (delivery)', {'DEL BKHP', 'DEL MAYP', 'TRANSPRT', 'SUPPLY-C'}),
    ('INTEREST', 'Bank Interest', {'INTEREST'}),
)

#: The kinds of cost the side's pie shows, `(key, name)`, in its order.
COSTS = (
    ('SALARY', 'Salary'),
    ('FREIGHT', 'Freight'),
    ('FINANCE', 'Interest & Finance'),
    ('RENT', 'Rent'),
    ('ELECTRICITY', 'Electricity'),
)

#: Who owns a head, by company: Sales has one owner per company (user, 2026-10-14).
OWNERS = {
    'SALES': [{'company': 'OIL', 'label': 'Oil', 'name': 'Raju Vg'},
              {'company': 'MART', 'label': 'Mart', 'name': 'Prabhjot Vg'},
              {'company': 'BEVERAGES', 'label': 'Beverages', 'name': 'Shunty Vg'}],
    'FACTORY': [{'company': 'OIL', 'label': 'Oil', 'name': 'Gagan Vg'},
                {'company': 'BEVERAGES', 'label': 'Beverages', 'name': 'Arvinder Vg'}],
    # A whole head, every company (no `company`): its one owner (user, 2026-10-16).
    'BACKOFFICE': [{'company': '', 'label': '', 'name': 'Nirmal Didi'}],
    'DELIVERY': [{'company': '', 'label': '', 'name': 'Sandeep Vg'}],
    'INTEREST': [{'company': '', 'label': '', 'name': 'Avtar Vg'}],
    'FINANCE': [{'company': '', 'label': '', 'name': 'Avtar Vg'}],
}

#: EACH OWNER'S OWN BOARD (user, 2026-10-16): an owner signed in sees only
#: their part — `parts`, `(head, company or None for the whole head)`, and any
#: `costs` — never the rest. `users`: their OMS logins, matched without regard
#: to case ("Raju Vg" is also "Raju VG"); overridable by setting
#: ADVANCE_PAYMENT_BOARD_VIEW_USERS = {view key: [usernames]}. Freight's owner
#: has no OMS login yet (2026-10-16). An administrator sees everything, and may
#: look at any owner's board as the owner sees it.
VIEWS = [
    {'key': 'SALES_OIL', 'label': 'Sales · Oil', 'owner': 'Raju Vg', 'parts': [('SALES', 'OIL')],
     'users': ['Raju Vg']},
    {'key': 'SALES_MART', 'label': 'Sales · Mart', 'owner': 'Prabhjot Vg', 'parts': [('SALES', 'MART')],
     'users': ['Prabhjot']},
    {'key': 'SALES_BEVERAGES', 'label': 'Sales · Beverages', 'owner': 'Shunty Vg',
     'parts': [('SALES', 'BEVERAGES')], 'users': ['Shunty Vg']},
    {'key': 'FACTORY_OIL', 'label': 'Factory · Oil', 'owner': 'Gagan Vg', 'parts': [('FACTORY', 'OIL')],
     'users': ['gagan']},
    {'key': 'FACTORY_BEVERAGES', 'label': 'Factory · Beverages', 'owner': 'Arvinder Vg',
     'parts': [('FACTORY', 'BEVERAGES')], 'users': ['arvinder']},
    {'key': 'BACKOFFICE', 'label': 'Back Office', 'owner': 'Nirmal Didi', 'parts': [('BACKOFFICE', None)],
     'users': ['nirmal']},
    {'key': 'FREIGHT', 'label': 'Freight (delivery)', 'owner': 'Sandeep Vg', 'parts': [('DELIVERY', None)],
     'users': []},
    # Bank Interest, and the interest and finance costs across every head.
    {'key': 'FINANCE', 'label': 'Bank Interest & Finance', 'owner': 'Avtar Vg', 'parts': [('INTEREST', None)],
     'costs': ['FINANCE'], 'users': ['Avtarsingh']},
]


def _view_users(view):
    from django.conf import settings

    override = (getattr(settings, 'ADVANCE_PAYMENT_BOARD_VIEW_USERS', None) or {}).get(view['key'])
    return [u.lower() for u in (view['users'] if override is None else override)]


def view_by_key(key):
    return next((v for v in VIEWS if v['key'] == key), None)


def view_for(user):
    """The owner's board this login sees, or None (the whole board)."""
    name = (user.get_username() if user else '').lower()
    return next((v for v in VIEWS if name and name in _view_users(v)), None)


def view_summary(view):
    return {'key': view['key'], 'label': view['label'], 'owner': view['owner']}


def _part(head, company):
    """A head as one company's part of it: its figures, owner and breakdown alone."""
    if company is None:
        return head
    part = next((c for c in head['by_company'] if c['company'] == company), None)
    base = part['base'] if part else 0.0
    current = part['current'] if part else 0.0
    return {**head, 'base': base, 'current': current, 'ratio': _ratio(current, base),
            'entries': part.get('entries', 0) if part else 0, 'tree': part.get('tree', []) if part else [],
            'by_company': [part] if part else [],
            'owners': [o for o in head['owners'] if o['company'] in (company, '')]}


def scoped(board, view):
    """The board as `view`'s owner sees it: their head (or their company's part
    of it) and their costs; the total is theirs."""
    by_key = {h['key']: h for h in board['heads']}
    heads = [_part(by_key[key], company) for key, company in view['parts'] if key in by_key]
    costs = [c for c in board['costs'] if c['key'] in view.get('costs', ())]
    base, current = sum(h['base'] for h in heads), sum(h['current'] for h in heads)
    return {**board, 'heads': heads, 'costs': costs,
            'total': {'base': _money(base), 'current': _money(current), 'ratio': _ratio(current, base)},
            'view': view_summary(view)}

#: The only Mart postings the board counts: its salaries and its freight (user, 2026-10-14).
MART_KEEPS = {'SALARY', 'FREIGHT'}

#: Budgets the board leaves out, in every company: new-product development —
#: NPD1 (its Legal sub budget included) and NPD2 — and, as `(budget, sub
#: budget)`, factory civil works (user, 2026-10-14).
LEFT_OUT_BUDGETS = {'NPD1', 'NPD2'}
LEFT_OUT_SUB_BUDGETS = {('FACTORY', 'CIVIL')}

FREIGHT_BUDGETS = {'TRANSPRT', 'DEL BKHP', 'DEL MAYP'}

#: Travelling and daily allowance ("TA AND DA", 5630005) counts as salary (user,
#: 2026-10-14) — as whole words, so a "META DATA" never does.
TA_DA = re.compile(r'\bTA\s*(?:AND|&|/)?\s*DA\b')

#: How much of each level of a breakdown is carried, largest first.
TOP_BUDGETS = 15
TOP_ACCOUNTS = 12
TOP_POSTINGS = 8


def _budget(row):
    return str(row.get('budget') or '').strip().upper()


def department_of(row):
    """The row a posting is counted in: by its SAP budget head; 'OTHER' for none of them.

    Mart's salaries count under Sales — its card, Prabhjot's — whatever budget
    they were booked to (most to HR/ADMIN; user, 2026-10-14).
    """
    if row.get('company') == 'MART' and cost_of(row) == 'SALARY':
        return 'SALES'
    budget = _budget(row)
    return next((key for key, _name, budgets in DEPARTMENTS if budget in budgets), 'OTHER')


def cost_of(row):
    """The kind of cost a posting is (the rules in the module's docstring), or None for none of the five."""
    account = str(row.get('account_name') or row.get('ledger') or '').upper()
    budget = _budget(row)
    if row.get('is_salary') or budget == 'SAL CF' or TA_DA.search(account):
        cost = 'SALARY'
    elif 'ELECTRIC' in account or 'GENERATOR' in account:
        cost = 'ELECTRICITY'
    elif 'RENT' in account and 'PARENT' not in account:
        cost = 'RENT'
    elif any(word in account for word in ('FREIGHT', 'CARTAGE', 'TRANSPORT')) or budget in FREIGHT_BUDGETS:
        cost = 'FREIGHT'
    elif budget == 'INTEREST' or any(word in account for word in ('INTEREST', 'BANK CHARGES', 'PENALTY')):
        cost = 'FINANCE'
    else:
        return None
    return cost


def counted(row):
    """Whether the board counts a posting at all: Oil's and Beverages' but for what is
    left out, and of Mart's only its salary and freight."""
    budget = _budget(row)
    if budget in LEFT_OUT_BUDGETS or (budget, str(row.get('sub_budget') or '').strip().upper()) in LEFT_OUT_SUB_BUDGETS:
        return False
    return row.get('company') != 'MART' or cost_of(row) in MART_KEEPS


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
        b_rows = [r for r in base_rows if r['company'] == code]
        c_rows = [r for r in cur_rows if r['company'] == code]
        b, c = sum(_amount(r) for r in b_rows), sum(_amount(r) for r in c_rows)
        if b or c:
            # Each company's own breakdown: a company's card opens onto its data alone.
            by_company.append({'company': code, 'label': label, 'base': _money(b), 'current': _money(c),
                               'entries': len(c_rows), 'tree': _tree(b_rows, c_rows)})
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


def _split(rows, key):
    by = defaultdict(list)
    for r in rows:
        by[key(r)].append(r)
    return by


def build(month='', baseline='', *, today=None):
    """The board: `month`'s spending (default: this month, to today) against `baseline`'s — by head and by cost."""
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

    base_rows = [r for r in _rows(base_start, base_end, closed=base_end < today) if counted(r)]
    cur_rows = [r for r in _rows(cur_start, cur_end, closed=cur_end == cur_month_end and cur_end < today)
                if counted(r)]

    base_by, cur_by = _split(base_rows, department_of), _split(cur_rows, department_of)
    heads = [_head(key, name, base_by[key], cur_by[key])
             for key, name in [(k, n) for k, n, _b in DEPARTMENTS] + [('OTHER', 'Other')]]
    base_cost, cur_cost = _split(base_rows, cost_of), _split(cur_rows, cost_of)
    costs = [_head(key, name, base_cost[key], cur_cost[key]) for key, name in COSTS]

    # Every posting is in exactly one head, so the heads are the whole.
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
        'costs': costs,
    }
