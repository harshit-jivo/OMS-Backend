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

from advance_payment.models import DEPARTMENT_HEAD_STAGE
from advance_payment.purposes import HEAD_PURPOSES, HEAD_REQUEST_TYPES, PAYMENT_PURPOSES

#: Who approves, by role, with the username each had on the test server.
PEOPLE = {
    'director': 'Gurpreet Vg',       # Gurpreet Ji: Director Approval
    'himanshu': 'himanshu',          # oil purchases and import expenses
    'bhupinder': 'bhupinder',        # RM & PM other than oil, Fixed Assets
    'ziyaul': 'ziyaul',              # salaries
    'factory_oil': 'gagan',          # Gagandeep Singh: Oil factory salaries
    'factory_bev': 'arvinder',       # Arvinder Singh: Beverages factory salaries
    'avtar': 'avtar',                # interest, bank charges, loans, statutory
    'sales_oil': 'Raju Vg',          # Jasvir Singh: Oil customer refunds
    'sales_bev': 'karanpreet',       # Karanpreet Singh: Beverages customer refunds
    'mart': 'prabhjot',              # Prabhjot Singh: all of Mart
    'payment': 'taran',
    'audit': 'parmeet',
    'final': 'kamal1',
    # Expense requests only: owners of budget heads not named above.
    'backoffice': 'nirmal',          # Nirmal Didi Ji: BackOff
    'transport': 'paramdeep',        # Paramdeep Singh: Transprt
    'marketing': 'karanpreet',       # Karanpreet Singh: Med MKT
}

#: The Department Head stage's configured user: never asked to act (the
#: picked head is), but the engine wants one.
HEAD_PLACEHOLDER = 'final'

#: Who sits at the head of the route: a `PEOPLE` role, or HEAD for the picked Department Head.
HEAD = 'HEAD'
OWNER_STAGE = 'Budget Owner Approval'
DIRECTOR_STAGE = 'Director Approval'

OIL_AND_BEV = "company IN ('OIL', 'BEVERAGES')"
NOT_STAFF = "request_type NOT IN ({})".format(', '.join(f"'{t}'" for t in sorted(HEAD_REQUEST_TYPES)))
FACTORY_BUDGETS = "('Factory', 'FACT_COM')"

#: Purposes approved by one fixed owner, and whether the Director follows.
FIXED_OWNER = {
    'OIL_PURCHASE': ('himanshu', True),
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
# the Budget app) — then Payment -> Audit, where Audit's approval posts.
#
#   <head>               owner                (Director after, for NPD)
#   <head>, electricity  owner, then Director (the requester ticks Electricity)
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

#: Heads whose owner is always followed by the Director.
EXPENSE_DIRECTOR_AFTER = frozenset({'NPD1', 'NPD2', 'NPD3'})

_COMPANY_TAG = {'OIL': 'OIL', 'BEVERAGES': 'BEV'}


def _head_key(budget_code):
    """'Sales RE' -> 'SALES_RE', 'R & D' -> 'R_D': a budget code as a workflow code part."""
    return re.sub(r'[^A-Z0-9]+', '_', budget_code.upper()).strip('_')


def _expense_chain(owner, director):
    stages = [(OWNER_STAGE, owner)] if owner else []
    if director or not owner:
        stages.append((DIRECTOR_STAGE, 'director'))
    return stages


def expense_routes():
    """The Expense workflows: per company and budget head, plain and electricity; and Mart."""
    out = [{'code': 'AP_EXP_MART', 'name': 'Expense · Mart', 'company': 'MART', 'expense': True,
            'query': _where(EXPENSE, "company = 'MART'"), 'stages': [(OWNER_STAGE, 'mart')]}]
    for company, heads in EXPENSE_OWNERS.items():
        tag = _COMPANY_TAG[company]
        for budget_code, owner in heads.items():
            base = (EXPENSE, f"company = '{company}'", f"budget_code = '{budget_code}'")
            director = budget_code in EXPENSE_DIRECTOR_AFTER
            out += [
                {'code': f'AP_EXP_{tag}_{_head_key(budget_code)}', 'name': f'Expense · {company.title()} · {budget_code}',
                 'company': company, 'expense': True,
                 'query': _where(*base, 'is_electricity = false'), 'stages': _expense_chain(owner, director)},
                {'code': f'AP_EXP_{tag}_{_head_key(budget_code)}_ELEC',
                 'name': f'Expense · {company.title()} · {budget_code} · electricity',
                 'company': company, 'expense': True,
                 'query': _where(*base, 'is_electricity = true'), 'stages': _expense_chain(owner, True)},
            ]
    return out


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
        {'code': 'AP_STAFF_ADVANCE', 'name': 'Payments · Staff Advance / Imprest', 'company': 'ALL',
         'query': _where(OIL_AND_BEV, NOT_STAFF.replace('NOT IN', 'IN')),
         'stages': _chain(HEAD, True)},
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
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': _chain(owner, director)})
        elif code in HEAD_PURPOSES:
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': _chain(HEAD, code in HEAD_THEN_DIRECTOR)})
        else:
            raise ValueError(f'Purpose {code} has no route in the hierarchy.')
    return out + expense_routes()


def full_stages(route):
    """All stages of a route, in order: `[(name, role)]`, the fixed ones last.

    Payment -> Audit -> Final; an Expense route ends at Audit, whose approval posts.
    """
    tail = [('Payment Approval', 'payment'), ('Audit Approval', 'audit')]
    if not route.get('expense'):
        tail.append(('Final Approval', 'final'))
    return [*route['stages'], *tail]
