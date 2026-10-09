"""The approval hierarchy for payment requests, as workflows: one per purpose.

Agreed with the business on 2026-10-05 (`Budget_Approval_Hierarchy.xlsx`).
Each request goes to exactly one workflow:

    Mart                              -> Prabhjot, whatever it is for
    Employee / Employee Imprest       -> Department Head -> Director
    anything else (Oil, Beverages)    -> the workflow of its Payment Purpose

and every workflow then ends Payment -> Audit -> Final. "Department Head"
is the person the requester picks on the form (see `purposes.HEAD_PURPOSES`),
not a fixed user: the stage is named `DEPARTMENT_HEAD_STAGE` and its
configured user is only a placeholder.

This module is DATA: who approves is named by role (`PEOPLE` keys), and
`manage.py seed_payment_workflows` maps the roles to usernames and writes the
workflows. Change a person there (or on the Workflows page), not here; change
the hierarchy here, and re-run the command.
"""
import re

from advance_payment.models import DEPARTMENT_HEAD_STAGE, EXPENSE_AUDIT_STAGE
from advance_payment.purposes import (
    FACTORY_BUDGETS as FACTORY_BUDGET_CODES, HEAD_PURPOSES, HEAD_REQUEST_TYPES, PAYMENT_PURPOSES,
    STAFF_ADVANCE_LIMIT,
)

#: Who approves, by role, with the username each had on the test server.
PEOPLE = {
    'director': 'Gurpreet Vg',       # Gurpreet Ji: Director Approval
    'himanshu': 'himanshu',          # import expenses
    'shunty': 'Shunty Vg',           # Shunty Veerji: oil purchases (2026-10-08)
    'bhupinder': 'bhupinder',        # RM & PM other than oil, Fixed Assets
    'ziyaul': 'ziyaul',              # salaries
    'factory_oil': 'gagan',          # Gagandeep Singh: Oil factory salaries
    'factory_bev': 'arvinder',       # Arvinder Singh: Beverages factory salaries
    'avtar': 'avtar',                # interest, bank charges, loans, statutory
    'sales_oil': 'Raju Vg',          # Jasvir Singh: Oil customer refunds
    'sales_bev': 'Raju Vg',          # Jasvir Singh: Beverages sales too (was Karanpreet; 2026-10-09)
    'mart': 'prabhjot',              # Prabhjot Singh: all of Mart
    'payment': 'taran',
    'audit': 'parmeet',
    'final': 'kamal1',
    # Expense requests only: owners of budget heads not named above.
    'backoffice': 'nirmal',          # Nirmal Didi Ji: BackOff
    'transport': 'paramdeep',        # Paramdeep Singh: Transprt
    'marketing': 'karanpreet',       # Karanpreet Singh: Med MKT
    'expense_audit': 'bhavani',      # Bhavani: the Expense routes' auditor (Parmeet audits payments)
}

#: The Department Head stage's configured user: never asked to act (the
#: picked head is), but the engine wants one.
HEAD_PLACEHOLDER = 'final'

#: Who sits at the head of the route: a `PEOPLE` role, or HEAD for the picked Department Head.
HEAD = 'HEAD'
OWNER_STAGE = 'Budget Owner Approval'
DIRECTOR_STAGE = 'Director Approval'
#: A second owner who is not the Director (Oil Purchase: Ziyaul).
SECOND_STAGE = 'Second Approval'

OIL_AND_BEV = "company IN ('OIL', 'BEVERAGES')"
NOT_STAFF = "request_type NOT IN ({})".format(', '.join(f"'{t}'" for t in sorted(HEAD_REQUEST_TYPES)))
FACTORY_BUDGETS = '(' + ', '.join(f"'{b}'" for b in FACTORY_BUDGET_CODES) + ')'
ADVANCE = "request_type = 'EMPLOYEE_ADVANCE'"

#: Purposes approved by one fixed owner, and whether the Director follows.
FIXED_OWNER = {
    # Oil purchases: Shunty, then Ziyaul — not the Director (user, 2026-10-08).
    'OIL_PURCHASE': ('shunty', False),
    'FREIGHT_IMPORT': ('himanshu', True),
    'RAW_MATERIAL': ('bhupinder', False),
    'PACKING_MATERIAL': ('bhupinder', False),
    'SEMI_FINISHED': ('bhupinder', False),
    'FA_PM': ('bhupinder', True),
    'FA_CIVIL': ('bhupinder', True),
    'FA_CONSUMABLES': ('bhupinder', True),
    'CAPITAL': ('bhupinder', True),
    'TDS': ('avtar', False),
    'GST': ('avtar', False),
    'PF_ESI': ('avtar', False),
    'OTHER_STATUTORY': ('avtar', False),
    'BANK_CHARGES': ('avtar', False),
    'INTEREST': ('avtar', False),
    'LOAN_REPAY': ('avtar', False),
}

#: Purposes a second fixed owner approves after the first, instead of the Director.
SECOND_OWNER = {'OIL_PURCHASE': 'ziyaul'}

#: Department Head purposes the Director approves after the head.
HEAD_THEN_DIRECTOR = frozenset({'UTILITIES', 'FA_OTHERS', 'EMP_ADVANCE', 'EMP_IMPREST', 'EXPENSE_CLAIM'})


def _where(*conditions):
    return 'SELECT id FROM advance_payment_request WHERE ' + ' AND '.join(conditions)


def _purpose(code):
    return f"purpose_code = '{code}'"


def _chain(first, director):
    """The approval stages before Payment: `[(stage name, role)]`."""
    stages = [(DEPARTMENT_HEAD_STAGE, HEAD) if first == HEAD else (OWNER_STAGE, first)]
    if director:
        stages.append((DIRECTOR_STAGE, 'director'))
    return stages


# ---------------------------------------------------------------------------
# Expense requests: by budget head, the budget hierarchy's owners
# ---------------------------------------------------------------------------
#
# An Expense request (paid straight to expense G/Ls) is routed by its budget
# head's OWNER — the Budget hierarchy agreed 2026-10-05/06 (the same owners as
# `budget/hierarchy.py`; copied here by role so Payments does not depend on
# the Budget app) — then the Director, then Payment -> Expense Audit (Bhavani),
# whose approval posts (user, 2026-10-09).
#
#   <head>               owner, then Director (electricity or not)
#   R & D, OTE           Director alone
#   Mart                 Prabhjot, whatever the head

EXPENSE = "request_type = 'EXPENSE'"

#: {company: {budget code as SAP has it: owner role, or None for "the Director alone"}}
EXPENSE_OWNERS = {
    'OIL': {
        'BackOff': 'backoffice', 'Sales': 'sales_oil', 'Sales RE': 'sales_oil',
        'Del Bkhp': 'bhupinder', 'Del Mayp': 'bhupinder', 'Factory': 'factory_oil', 'FACT_COM': 'factory_oil',
        'Interest': 'avtar', 'Med MKT': 'marketing', 'R & D': None, 'OTE': None, 'Transprt': 'transport',
        'NPD1': 'avtar', 'NPD2': 'avtar', 'NPD3': 'avtar',
    },
    'BEVERAGES': {
        'BackOff': 'backoffice', 'Sales': 'sales_bev', 'Sales RE': 'sales_bev',
        'Del Bkhp': 'bhupinder', 'Del Mayp': 'bhupinder', 'Factory': 'factory_bev', 'FACT_COM': 'factory_bev',
        'Interest': 'avtar', 'Med MKT': 'marketing', 'OTE': None, 'Transprt': 'transport',
        'NPD1': 'avtar', 'NPD2': 'avtar', 'NPD3': 'avtar',
    },
}


_COMPANY_TAG = {'OIL': 'OIL', 'BEVERAGES': 'BEV'}


def _head_key(budget_code):
    """'Sales RE' -> 'SALES_RE', 'R & D' -> 'R_D': a budget code as a workflow code part."""
    return re.sub(r'[^A-Z0-9]+', '_', budget_code.upper()).strip('_')


def _expense_chain(owner):
    """The budget head's owner (if it has one), then the Director."""
    return [(OWNER_STAGE, owner)] * bool(owner) + [(DIRECTOR_STAGE, 'director')]


def expense_routes():
    """The Expense workflows: one per company and budget head; and Mart."""
    out = [{'code': 'AP_EXP_MART', 'name': 'Expense · Mart', 'company': 'MART', 'expense': True,
            'query': _where(EXPENSE, "company = 'MART'"), 'stages': [(OWNER_STAGE, 'mart')]}]
    for company, heads in EXPENSE_OWNERS.items():
        tag = _COMPANY_TAG[company]
        for budget_code, owner in heads.items():
            out.append({'code': f'AP_EXP_{tag}_{_head_key(budget_code)}',
                        'name': f'Expense · {company.title()} · {budget_code}', 'company': company, 'expense': True,
                        'query': _where(EXPENSE, f"company = '{company}'", f"budget_code = '{budget_code}'"),
                        'stages': _expense_chain(owner)})
    return out


def retired_expense_routes():
    """The Expense workflows split by Electricity until 2026-10-09: the Director
    now follows every owner, so one workflow per budget head serves both."""
    return [f'AP_EXP_{_COMPANY_TAG[company]}_{_head_key(budget_code)}_ELEC'
            for company, heads in EXPENSE_OWNERS.items() for budget_code in heads]


def routes():
    """Every workflow of the hierarchy: `[{code, name, company, query, stages}]`.

    `stages` are the approval stages only, `[(name, role)]`; Payment, Audit
    and Final are added by `full_stages`. `company` is the workflow's scope
    (the query narrows it further).
    """
    labels = {code: label for code, label, _g in PAYMENT_PURPOSES}
    out = [
        {'code': 'AP_MART', 'name': 'Payments · Mart', 'company': 'MART',
         'query': _where("company = 'MART'", "request_type <> 'EXPENSE'"), 'stages': [(OWNER_STAGE, 'mart')]},
        # Imprest, and a salary advance outside the plant: the picked Department Head, then the Director.
        {'code': 'AP_STAFF_ADVANCE', 'name': 'Payments · Staff Advance / Imprest', 'company': 'ALL',
         'query': _where(OIL_AND_BEV, NOT_STAFF.replace('NOT IN', 'IN'),
                         f"(request_type = 'EMPLOYEE_IMPREST' OR budget_code NOT IN {FACTORY_BUDGETS})"),
         'stages': _chain(HEAD, True)},
        # A plant's staff salary advance (approval matrix, 2026-10-09): up to the
        # limit, the plant's approver; above it, the Director ("Sonu Veerji") alone.
        {'code': 'AP_STAFF_ADVANCE_FACTORY_OIL', 'name': 'Payments · Staff Advance · Oil plant · up to 20,000',
         'company': 'OIL',
         'query': _where("company = 'OIL'", ADVANCE, f'budget_code IN {FACTORY_BUDGETS}',
                         f'amount <= {STAFF_ADVANCE_LIMIT}'),
         'stages': [(OWNER_STAGE, 'shunty')]},
        {'code': 'AP_STAFF_ADVANCE_FACTORY_BEV', 'name': 'Payments · Staff Advance · Beverages plant · up to 20,000',
         'company': 'BEVERAGES',
         'query': _where("company = 'BEVERAGES'", ADVANCE, f'budget_code IN {FACTORY_BUDGETS}',
                         f'amount <= {STAFF_ADVANCE_LIMIT}'),
         'stages': [(OWNER_STAGE, 'factory_bev')]},
        {'code': 'AP_STAFF_ADVANCE_FACTORY_HIGH', 'name': 'Payments · Staff Advance · plant · above 20,000',
         'company': 'ALL',
         'query': _where(OIL_AND_BEV, ADVANCE, f'budget_code IN {FACTORY_BUDGETS}',
                         f'amount > {STAFF_ADVANCE_LIMIT}'),
         'stages': [(DIRECTOR_STAGE, 'director')]},
    ]
    for code, label, _group in PAYMENT_PURPOSES:
        base = (OIL_AND_BEV, NOT_STAFF, _purpose(code))
        if code == 'SALARY':
            out += [
                {'code': 'AP_SALARY', 'name': f'Payments · {label}', 'company': 'ALL',
                 'query': _where(*base, f'budget_code NOT IN {FACTORY_BUDGETS}'),
                 'stages': _chain('ziyaul', False)},
                {'code': 'AP_SALARY_FACTORY_OIL', 'name': f'Payments · {label} · Oil factory', 'company': 'OIL',
                 'query': _where("company = 'OIL'", NOT_STAFF, _purpose(code), f'budget_code IN {FACTORY_BUDGETS}'),
                 'stages': _chain('factory_oil', False)},
                {'code': 'AP_SALARY_FACTORY_BEV', 'name': f'Payments · {label} · Beverages factory',
                 'company': 'BEVERAGES',
                 'query': _where("company = 'BEVERAGES'", NOT_STAFF, _purpose(code),
                                 f'budget_code IN {FACTORY_BUDGETS}'),
                 'stages': _chain('factory_bev', False)},
            ]
        elif code == 'CUSTOMER_REFUND':
            out += [
                {'code': f'AP_{code}_{tag}', 'name': f'Payments · {label} · {title}', 'company': company,
                 'query': _where(f"company = '{company}'", NOT_STAFF, _purpose(code)),
                 'stages': _chain(owner, False)}
                for company, tag, title, owner in (('OIL', 'OIL', 'Oil', 'sales_oil'),
                                                   ('BEVERAGES', 'BEV', 'Beverages', 'sales_bev'))
            ]
        elif code in FIXED_OWNER:
            owner, director = FIXED_OWNER[code]
            stages = _chain(owner, director)
            if code in SECOND_OWNER:
                stages.append((SECOND_STAGE, SECOND_OWNER[code]))
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': stages})
        elif code in HEAD_PURPOSES:
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': _chain(HEAD, code in HEAD_THEN_DIRECTOR)})
        else:
            raise ValueError(f'Purpose {code} has no route in the hierarchy.')
    return out + expense_routes()


def full_stages(route):
    """All stages of a route, in order: `[(name, role)]`, the fixed ones last.

    Payment -> Audit -> Final; an Expense route ends Payment -> Expense Audit
    (its own auditor), whose approval posts.
    """
    if route.get('expense'):
        return [*route['stages'], ('Payment Approval', 'payment'), (EXPENSE_AUDIT_STAGE, 'expense_audit')]
    return [*route['stages'], ('Payment Approval', 'payment'), ('Audit Approval', 'audit'),
            ('Final Approval', 'final')]
