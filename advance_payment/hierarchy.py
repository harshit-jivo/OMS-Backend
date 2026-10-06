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


def routes():
    """Every workflow of the hierarchy: `[{code, name, company, query, stages}]`.

    `stages` are the approval stages only, `[(name, role)]`; Payment, Audit
    and Final are added by `full_stages`. `company` is the workflow's scope
    (the query narrows it further).
    """
    labels = {code: label for code, label, _g in PAYMENT_PURPOSES}
    out = [
        {'code': 'AP_MART', 'name': 'Payments · Mart', 'company': 'MART',
         'query': _where("company = 'MART'"), 'stages': [(OWNER_STAGE, 'mart')]},
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
    return out


def full_stages(route):
    """All stages of a route, in order: `[(name, role)]`, the three fixed ones last."""
    return [*route['stages'], ('Payment Approval', 'payment'), ('Audit Approval', 'audit'),
            ('Final Approval', 'final')]
