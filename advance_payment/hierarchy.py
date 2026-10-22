"""The approval hierarchy for payment requests, as workflows: one per purpose.

Agreed with the business on 2026-10-05 (`Budget_Approval_Hierarchy.xlsx`).
Each request goes to exactly one workflow:

    Against Bill (any company)        -> straight to Payment (user, 2026-10-19)
    Freight (any company)             -> Navdeep, then Payment, bills included
    Mart                              -> Prabhjot, whatever it is for
    Employee / Employee Imprest       -> Department Head -> Director
    Account Payment (any company)     -> Avtar
    anything else (Oil, Beverages)    -> the workflow of its Payment Purpose

and every workflow starts at Payment, then those approvals, then ends
CA (Navdeep) -> Audit -> Final (2026-10-20). An Expense workflow keeps its
approvals first: approvals -> Payment -> CA -> Expense Audit. "Department Head"
is the person the requester picks on the form (see `purposes.HEAD_PURPOSES`),
not a fixed user: the stage is named `DEPARTMENT_HEAD_STAGE` and its
configured user is only a placeholder.

This module is DATA: who approves is named by role (`PEOPLE` keys), and
`manage.py seed_payment_workflows` maps the roles to usernames and writes the
workflows. Change a person there (or on the Workflows page), not here; change
the hierarchy here, and re-run the command.
"""
import re

from advance_payment.models import ACCOUNTS_STAGE, DEPARTMENT_HEAD_STAGE, EXPENSE_AUDIT_STAGE
from advance_payment.purposes import (
    DUE_SOON_DAYS, DUE_SOON_PURPOSES, ECOM_BUDGET, ECOM_PURPOSES, FACTORY_BUDGETS as FACTORY_BUDGET_CODES,
    FREIGHT_PURPOSES, HEAD_PURPOSES,
    HEAD_REQUEST_TYPES, PAYMENT_PURPOSES, STAFF_ADVANCE_LIMIT,
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
    'avtar': 'Avtarsingh',           # Avtar Singh: interest, bank charges, loans, statutory; Account Payments
    'sales_oil': 'Raju Vg',          # Jasvir Singh: Oil customer refunds
    'sales_bev': 'Raju Vg',          # Jasvir Singh: Beverages sales too (was Karanpreet; 2026-10-09)
    'mart': 'prabhjot',              # Prabhjot Singh: all of Mart
    'navdeep': 'Navdeep',            # Navdeep Singh (Bhati): incentives and TA/DA, after the Department Head
    'payment': 'taran',
    'audit': 'parmeet',
    'final': 'kamal1',
    # Expense requests only: owners of budget heads not named above.
    'backoffice': 'nirmal',          # Nirmal Didi Ji: BackOff; after the Back Office functions' heads
    'transport': 'paramdeep',        # Paramdeep Singh: Transprt
    'marketing': 'karanpreet',       # Karanpreet Singh: Med MKT
    'expense_audit': 'bhavani',      # Bhavani: the Expense routes' auditor (Parmeet audits payments)
    'accounts': 'satnamsingh',       # Satnam Singh: the Accounts desk, the Expense routes' Payment desk (2026-10-20)
    'ca': 'Navdeep',                 # Navdeep Singh: the CA desk, after Payment on every route (2026-10-19)
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
#: Freight's one approver before Payment: Navdeep (user, 2026-10-19).
FREIGHT_STAGE = 'Freight Approval'
#: The CA desk: Navdeep, right after Payment on every route (user, 2026-10-19).
CA_STAGE = 'CA Approval'
#: Mart's small imprests: Navdeep, before Payment (user, 2026-10-19).
IMPREST_STAGE = 'Imprest Approval'
#: The requests already waiting at an approval stage on 2026-10-19, moved to
#: Navdeep at the business's request: his approval, then Payment.
MOVED_STAGE = 'Navdeep Approval'
#: Those routes are never chosen for a request: their query matches nothing.
#: They exist for the requests moved onto them, and so re-seeding gives them CA.
NEVER_CHOSEN = 'SELECT id FROM advance_payment_request WHERE 1 = 0'

OIL_AND_BEV = "company IN ('OIL', 'BEVERAGES')"
NOT_STAFF = "request_type NOT IN ({})".format(', '.join(f"'{t}'" for t in sorted(HEAD_REQUEST_TYPES)))
FACTORY_BUDGETS = '(' + ', '.join(f"'{b}'" for b in FACTORY_BUDGET_CODES) + ')'
ADVANCE = "request_type = 'EMPLOYEE_ADVANCE'"
#: Employee Imprest against the journal entries crediting the staff account —
#: the monthly TA/DA and incentive: the picked Department Head, then Navdeep
#: (user, 2026-10-14), whatever the purpose. Kept out of the Staff Advance route.
IMPREST_JOURNAL = "(request_type = 'EMPLOYEE_IMPREST' AND payment_against = 'AGAINST_JOURNAL')"

#: An Account Payment (one G/L account, no partner — Katebaa, TPT, BSU): Avtar,
#: then Payment, in every company (user, 2026-10-15). Kept out of every other route.
ACCOUNT = "request_type = 'ACCOUNT'"
#: ...ticked for the Director by the requester: the Director instead of Avtar (2026-10-21).
DIRECTOR_TICKED = 'director_approval'
NOT_ACCOUNT = "request_type <> 'ACCOUNT'"

#: Paid against bills (user, 2026-10-19): straight to the Payment desk, in
#: every company and for every type and purpose — freight aside, which goes to
#: Navdeep first. Kept out of every other route.
FREIGHT = "purpose_code IN ({})".format(', '.join(f"'{p}'" for p in FREIGHT_PURPOSES))
#: Freight on its own route: any company, any Payment Against — not an
#: Employee / Imprest request (those keep their own route) or an Account Payment.
FREIGHT_ROUTED = f'({FREIGHT} AND {NOT_STAFF} AND {NOT_ACCOUNT})'
AGAINST_BILL = "payment_against = 'AGAINST_BILL'"
NOT_BILL = "payment_against <> 'AGAINST_BILL'"
#: Mart's Employee Imprest up to the limit (user, 2026-10-19): Navdeep, then
#: Payment; above it, Prabhjot as every other Mart request. Not against bills.
MART_SMALL_IMPREST = f"(request_type = 'EMPLOYEE_IMPREST' AND amount <= {STAFF_ADVANCE_LIMIT})"

#: Purposes approved by one fixed owner, and whether the Director follows.
FIXED_OWNER = {
    # Oil purchases: Shunty, then Ziyaul — not the Director (user, 2026-10-08).
    'OIL_PURCHASE': ('shunty', False),
    'RAW_MATERIAL': ('bhupinder', False),
    'PACKING_MATERIAL': ('bhupinder', False),
    'SEMI_FINISHED': ('bhupinder', False),
    # Civil works stay with Bhupinder; the plant's own assets go to the plant (below).
    'FA_CIVIL': ('bhupinder', True),
    'CAPITAL': ('bhupinder', True),
    'TDS': ('avtar', False),
    'GST': ('avtar', False),
    'PF_ESI': ('avtar', False),
    'OTHER_STATUTORY': ('avtar', False),
    'BANK_CHARGES': ('avtar', False),
    'INTEREST': ('avtar', False),
    'LOAN_REPAY': ('avtar', False),
}

#: Fixed assets of the plant — plant & machinery, spares, others — go to the
#: plant's own approver, then the Director (user, 2026-10-16): Oil to Gagan,
#: Beverages to Arvinder. One workflow per company. Civil stays with Bhupinder.
PLANT_PURPOSES = ('FA_PM', 'FA_CONSUMABLES', 'FA_OTHERS')
PLANT_APPROVERS = (('OIL', 'OIL', 'Oil', 'factory_oil'), ('BEVERAGES', 'BEV', 'Beverages', 'factory_bev'))

#: Every Fixed Assets purpose (user, 2026-10-22): its owner (or, paid On
#: Account, the Department Head), then the Director, THEN Payment, CA, Audit,
#: Final — whatever it is paid against, bills included. Oil and Beverages; Mart
#: keeps its one approver. "Land / Property / Capital" is not one of them.
FA_PURPOSES = ('FA_PM', 'FA_CIVIL', 'FA_OTHERS', 'FA_CONSUMABLES')

#: Workflows the hierarchy no longer has, deactivated when it is seeded:
#: the plant's fixed assets had one workflow for both companies until 2026-10-16.
RETIRED_ROUTES = ('AP_FA_PM', 'AP_FA_CONSUMABLES', 'AP_FA_OTHERS',
                  # Mart's own RM/PM route: one route per purpose in every company since 2026-10-21.
                  'AP_MART_RM_PM')

#: The Back Office functions (user, 2026-10-20): the Department Head the
#: requester picks — the function's head: HR Sarabjeet Kaur, IT Gurvinderjeet
#: Singh, Legal Karanpreet Singh, Administration Bikramjeet Singh, Accounts
#: Avtar Singh Rupal — then Nirmal Didi Ji, and no Director. Any department;
#: Mart keeps its one approver.
BACK_OFFICE_PURPOSES = ('BO_HR', 'BO_IT', 'BO_LEGAL', 'BO_ADMIN', 'BO_ACCOUNTS')
BACK_OFFICE_STAGE = 'Back Office Approval'

#: Jivo Mart's Raw Material and Packing Material are bought centrally, through
#: Jivo Wellness, under Bhupinder (user, 2026-10-20): Mart's requests for them
#: go to him, not to Prabhjot.
MART_CENTRAL_PURPOSES = ('RAW_MATERIAL', 'PACKING_MATERIAL')
MART_CENTRAL = "purpose_code IN ('RAW_MATERIAL', 'PACKING_MATERIAL')"
#: Raw and Packing Material, in EVERY company and against anything (user,
#: 2026-10-21): Bhupinder, then Payment, CA, Audit, Final. Not an Employee /
#: Imprest request (its own route) or an Account Payment.
RM_PM_ROUTED = f'({MART_CENTRAL} AND {NOT_STAFF} AND {NOT_ACCOUNT})'

#: The E-commerce department's purposes (user, 2026-10-22), in every company,
#: whatever the type (an Account Payment, an Expense and an imprest against a
#: journal entry aside), bills included: Prabhjot under the ECOM budget head and
#: in Mart, else the Department Head picked. Kept out of every other route.
ECOM_ROUTED = ("(purpose_code IN ({}) AND {} AND request_type <> 'EXPENSE' AND NOT {})"
               .format(', '.join(f"'{p}'" for p in sorted(ECOM_PURPOSES)), NOT_ACCOUNT, IMPREST_JOURNAL))
ECOM_BY_PRABHJOT = f"(company = 'MART' OR budget_code = '{ECOM_BUDGET}')"
ECOM_BY_HEAD = f"({OIL_AND_BEV} AND budget_code <> '{ECOM_BUDGET}')"
#: ...the approver FIRST, then Payment: paid On Account, against a PO or an
#: Advance, or an Employee Imprest (user, 2026-10-22). Else Payment first.
ECOM_FIRST = "(payment_against IN ('ON_ACCOUNT', 'AGAINST_PO', 'ADVANCE') OR request_type = 'EMPLOYEE_IMPREST')"

#: A Fixed Assets request on its own routes (bills included), not on the bills' route.
FA_ROUTED = "(purpose_code IN ({}) AND {} AND {} AND {})".format(
    ', '.join(f"'{p}'" for p in FA_PURPOSES), OIL_AND_BEV, NOT_STAFF, NOT_ACCOUNT)

#: Purposes the CA desk does not see (user, 2026-10-21): Oil Purchase — its own
#: route, a bill for it and a vendor paid on account for it all skip CA.
NO_CA_PURPOSES = ('OIL_PURCHASE',)
OIL_PURCHASE_ROUTED = f"(purpose_code = 'OIL_PURCHASE' AND {NOT_STAFF} AND {NOT_ACCOUNT})"

#: Paid against an ADVANCE (an employee's advance or imprest), or a VENDOR paid
#: against a PO: the approvers FIRST, then Payment, CA, Audit, Final (user,
#: 2026-10-21). Every route with approvers is split in two by it
#: (`_approvals_first`); the rest keep Payment first.
AGAINST_ADVANCE = "payment_against = 'ADVANCE'"
VENDOR_AGAINST_PO = "(request_type = 'VENDOR' AND payment_against = 'AGAINST_PO')"
APPROVALS_FIRST = f'({AGAINST_ADVANCE} OR {VENDOR_AGAINST_PO})'
APPROVALS_FIRST_SUFFIX = '_APPROVALS_FIRST'
#: Routes no such request reaches (an Account Payment, an imprest against a
#: journal entry, a vendor on account, Mart's small imprests: Navdeep alone).
NOT_SPLIT = frozenset({'AP_ACCOUNT', 'AP_ACCOUNT_DIRECTOR', 'AP_IMPREST_JOURNAL', 'AP_VENDOR_ON_ACCOUNT',
                       'AP_VENDOR_ON_ACCOUNT_DIRECTOR', 'AP_MART_IMPREST_SMALL', 'AP_OIL_PURCHASE_ON_ACCOUNT',
                       # A customer's refund: never against an Advance or a PO.
                       'AP_CUSTOMER_REFUND_OIL', 'AP_CUSTOMER_REFUND_BEV'})

#: A Vendor paid On Account, for any purpose (user, 2026-10-21): the picked
#: Department Head, then the Director where the purpose's own route has him.
#: Only the purposes routed to a fixed owner need a route of their own for it;
#: a Department Head purpose (Utilities, Insurance, Back Office…) already goes
#: through the head, and freight stays with Navdeep.
VENDOR_ON_ACCOUNT = "(request_type = 'VENDOR' AND payment_against = 'ON_ACCOUNT')"


def _on_account_by_head():
    """`(purposes routed by the head when paid On Account, those of them the Director then approves)`."""
    fixed = [c for c, _l, _g in PAYMENT_PURPOSES if c not in HEAD_PURPOSES and c not in FREIGHT_PURPOSES
             and c not in MART_CENTRAL_PURPOSES  # RM / PM: Bhupinder, however it is paid
             and c not in NO_CA_PURPOSES  # Oil Purchase on account: its own route, without CA
             and c not in ECOM_PURPOSES]  # E-commerce's: Prabhjot or the head, on their own routes
    director = [c for c in fixed if c in PLANT_PURPOSES or (c in FIXED_OWNER and FIXED_OWNER[c][1])]
    return tuple(fixed), tuple(director)


#: Purposes a second fixed owner approves after the first, instead of the Director.
SECOND_OWNER = {'OIL_PURCHASE': 'ziyaul'}

#: Department Head purposes a fixed second approver takes after the head,
#: instead of the Director: incentives and TA/DA go to Navdeep (user, 2026-10-14).
HEAD_THEN_SECOND = {'INCENTIVE': 'navdeep', 'TA_DA': 'navdeep'}

#: Department Head purposes the Director approves after the head.
HEAD_THEN_DIRECTOR = frozenset({'UTILITIES', 'INSURANCE', 'EMP_ADVANCE', 'EMP_IMPREST', 'EXPENSE_CLAIM'})


#: The request's Bill Due Date is DUE_SOON_DAYS days or fewer from today, or
#: already past (`purposes.DUE_SOON_PURPOSES`): its route skips the approval
#: stages. "Today" is India's (`clock.today()`), not the database session's
#: UTC date. Evaluated when the request is submitted or resubmitted. A request
#: with no Bill Due Date (raised before it was asked) is never "due soon", so
#: `NOT BILL_DUE_SOON` takes it: the usual route.
BILL_DUE_SOON = (
    "(bill_due_date IS NOT NULL "
    f"AND bill_due_date <= (now() AT TIME ZONE 'Asia/Kolkata')::date + {DUE_SOON_DAYS})"
)
DUE_SOON_SUFFIX = '_DUE_SOON'


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


def moved_routes():
    """The routes requests were moved onto, never chosen for a new one: those
    waiting on 2026-10-19 (Navdeep, then Payment), and the Fixed Assets at
    Payment on 2026-10-20 (the Director, then Payment)."""
    return [
        {'code': 'AP_MOVED_TO_NAVDEEP', 'name': 'Payments · moved to Navdeep (19 Oct 2026)', 'company': 'ALL',
         'query': NEVER_CHOSEN, 'stages': [(MOVED_STAGE, 'navdeep')], 'never_chosen': True},
        {'code': 'AP_EXP_MOVED_TO_NAVDEEP', 'name': 'Expense · moved to Navdeep (19 Oct 2026)', 'company': 'ALL',
         'query': NEVER_CHOSEN, 'stages': [(MOVED_STAGE, 'navdeep')], 'expense': True, 'never_chosen': True},
        # Fixed Assets waiting at Payment that had passed their owner (user,
        # 2026-10-20): the Director first, then Payment fills the payment —
        # approvals first, as routes were before Payment came first.
        {'code': 'AP_MOVED_TO_DIRECTOR', 'name': 'Payments · Fixed Assets moved to the Director (20 Oct 2026)',
         'company': 'ALL', 'query': NEVER_CHOSEN, 'stages': [(DIRECTOR_STAGE, 'director')],
         'never_chosen': True, 'approvals_first': True},
    ]


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
         'query': _where("company = 'MART'", f'NOT {IMPREST_JOURNAL}', "request_type NOT IN ('EXPENSE', 'ACCOUNT')", NOT_BILL,
                         f'NOT {FREIGHT_ROUTED}', f'NOT {MART_SMALL_IMPREST}', f'NOT {RM_PM_ROUTED}',
                         f'NOT {BILL_DUE_SOON}', f'NOT {ECOM_ROUTED}'),
         'stages': [(OWNER_STAGE, 'mart')]},
        # Mart's utilities and insurance due within DUE_SOON_DAYS days, or overdue
        # (2026-10-21): straight to Payment, as Oil's and Beverages' are.
        {'code': f'AP_MART{DUE_SOON_SUFFIX}',
         'name': f'Payments · Mart · bill due in {DUE_SOON_DAYS} days or less', 'company': 'MART',
         'query': _where("company = 'MART'", f'NOT {IMPREST_JOURNAL}', "request_type NOT IN ('EXPENSE', 'ACCOUNT')", NOT_BILL,
                         f'NOT {FREIGHT_ROUTED}', f'NOT {MART_SMALL_IMPREST}', f'NOT {RM_PM_ROUTED}',
                         BILL_DUE_SOON, f'NOT {ECOM_ROUTED}'),
         'stages': []},
        {'code': 'AP_MART_IMPREST_SMALL', 'name': f'Payments · Mart · Employee Imprest · up to {STAFF_ADVANCE_LIMIT:,}',
         'company': 'MART', 'query': _where("company = 'MART'", f'NOT {IMPREST_JOURNAL}', NOT_BILL, MART_SMALL_IMPREST,
                                          f'NOT {ECOM_ROUTED}'),
         'stages': [(IMPREST_STAGE, 'navdeep')]},
        {'code': 'AP_ACCOUNT', 'name': 'Payments · Account Payment', 'company': 'ALL',
         'query': _where(ACCOUNT, f'NOT {DIRECTOR_TICKED}'), 'stages': [(OWNER_STAGE, 'avtar')]},
        {'code': 'AP_ACCOUNT_DIRECTOR', 'name': 'Payments · Account Payment · Director', 'company': 'ALL',
         'query': _where(ACCOUNT, DIRECTOR_TICKED), 'stages': [(DIRECTOR_STAGE, 'director')]},
        # Imprest, and a salary advance outside the plant: the picked Department Head, then the Director.
        {'code': 'AP_STAFF_ADVANCE', 'name': 'Payments · Staff Advance / Imprest', 'company': 'ALL',
         'query': _where(OIL_AND_BEV, NOT_STAFF.replace('NOT IN', 'IN'), NOT_BILL,
                         f"(request_type = 'EMPLOYEE_IMPREST' OR budget_code NOT IN {FACTORY_BUDGETS})",
                         f'NOT {IMPREST_JOURNAL}', f'NOT {ECOM_ROUTED}'),
         'stages': _chain(HEAD, True)},
        # Against a journal entry (TA/DA, incentives), in EVERY company and at any
        # amount: Navdeep alone (user, 2026-10-21) — no Department Head, and in
        # Mart no 20,000 split and no Prabhjot. He holds the CA desk too, so he
        # approves it once, there. Mart's other imprests still go by amount.
        {'code': 'AP_IMPREST_JOURNAL', 'name': 'Payments · Imprest against Journal Entry (TA/DA, incentive)',
         'company': 'ALL', 'query': _where(IMPREST_JOURNAL),
         'stages': [(SECOND_STAGE, 'navdeep')]},
        # A plant's staff salary advance (approval matrix, 2026-10-09): up to the
        # limit, the plant's approver; above it, the Director ("Sonu Veerji") alone.
        {'code': 'AP_STAFF_ADVANCE_FACTORY_OIL', 'name': 'Payments · Staff Advance · Oil plant · up to 20,000',
         'company': 'OIL',
         'query': _where("company = 'OIL'", ADVANCE, f'budget_code IN {FACTORY_BUDGETS}',
                         f'amount <= {STAFF_ADVANCE_LIMIT}', f'NOT {ECOM_ROUTED}'),
         'stages': [(OWNER_STAGE, 'shunty')]},
        {'code': 'AP_STAFF_ADVANCE_FACTORY_BEV', 'name': 'Payments · Staff Advance · Beverages plant · up to 20,000',
         'company': 'BEVERAGES',
         'query': _where("company = 'BEVERAGES'", ADVANCE, f'budget_code IN {FACTORY_BUDGETS}',
                         f'amount <= {STAFF_ADVANCE_LIMIT}', f'NOT {ECOM_ROUTED}'),
         'stages': [(OWNER_STAGE, 'factory_bev')]},
        {'code': 'AP_STAFF_ADVANCE_FACTORY_HIGH', 'name': 'Payments · Staff Advance · plant · above 20,000',
         'company': 'ALL',
         'query': _where(OIL_AND_BEV, ADVANCE, f'budget_code IN {FACTORY_BUDGETS}',
                         f'amount > {STAFF_ADVANCE_LIMIT}', f'NOT {ECOM_ROUTED}'),
         'stages': [(DIRECTOR_STAGE, 'director')]},
    ]
    # E-commerce's purposes (user, 2026-10-22): Prabhjot (ECOM head, Mart) or the
    # head picked; the approver first when paid On Account, against a PO or an
    # Advance, or for an Employee Imprest — else Payment first. Split here, not
    # by `_approvals_first`, whose rule differs.
    for tag, scope, stages, who in (
            ('PRABHJOT', ECOM_BY_PRABHJOT, [(OWNER_STAGE, 'mart')], 'Prabhjot'),
            ('HEAD', ECOM_BY_HEAD, [(DEPARTMENT_HEAD_STAGE, HEAD)], 'Department Head')):
        name = f'Payments · E-commerce (ASSPL) · {who}'
        out += [
            {'code': f'AP_ECOM_{tag}', 'name': name, 'company': 'ALL',
             'query': _where(ECOM_ROUTED, scope, f'NOT {ECOM_FIRST}'), 'stages': stages, 'no_split': True},
            {'code': f'AP_ECOM_{tag}{APPROVALS_FIRST_SUFFIX}',
             'name': f'{name} · On Account / PO / Advance / Imprest (approvals, then Payment)', 'company': 'ALL',
             'query': _where(ECOM_ROUTED, scope, ECOM_FIRST), 'stages': stages, 'approvals_first': True},
        ]
    by_head, with_director = _on_account_by_head()
    for code, label, _group in PAYMENT_PURPOSES:
        # A fixed owner's purpose paid On Account by a vendor goes by the head instead (below).
        on_account = (f'NOT {VENDOR_ON_ACCOUNT}',) if code in by_head else ()
        # A Fixed Assets purpose takes its bills too (2026-10-22).
        bills = () if code in FA_PURPOSES else (NOT_BILL,)
        first = {'always_first': True} if code in FA_PURPOSES else {}
        base = (OIL_AND_BEV, NOT_STAFF, NOT_ACCOUNT, *bills, _purpose(code), *on_account)
        if code in ECOM_PURPOSES:
            continue  # their own routes, above
        if code in FREIGHT_PURPOSES:
            # Every company and Payment Against, bills included: Navdeep first.
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(NOT_STAFF, NOT_ACCOUNT, _purpose(code)),
                        'stages': [(FREIGHT_STAGE, 'navdeep')]})
            continue
        if code in MART_CENTRAL_PURPOSES:
            # Raw / Packing Material (2026-10-21): every company, bills and on
            # account included — Bhupinder FIRST, then Payment, CA, Audit, Final.
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(NOT_STAFF, NOT_ACCOUNT, _purpose(code)),
                        'stages': [(OWNER_STAGE, 'bhupinder')], 'approvals_first': True})
            continue
        if code == 'SALARY':
            out += [
                {'code': 'AP_SALARY', 'name': f'Payments · {label}', 'company': 'ALL',
                 'query': _where(*base, f'budget_code NOT IN {FACTORY_BUDGETS}'),
                 'stages': _chain('ziyaul', False)},
                {'code': 'AP_SALARY_FACTORY_OIL', 'name': f'Payments · {label} · Oil factory', 'company': 'OIL',
                 'query': _where("company = 'OIL'", NOT_STAFF, NOT_ACCOUNT, NOT_BILL, _purpose(code),
                                 f'budget_code IN {FACTORY_BUDGETS}', *on_account),
                 'stages': _chain('factory_oil', False)},
                {'code': 'AP_SALARY_FACTORY_BEV', 'name': f'Payments · {label} · Beverages factory',
                 'company': 'BEVERAGES',
                 'query': _where("company = 'BEVERAGES'", NOT_STAFF, NOT_ACCOUNT, NOT_BILL, _purpose(code),
                                 f'budget_code IN {FACTORY_BUDGETS}', *on_account),
                 'stages': _chain('factory_bev', False)},
            ]
        elif code == 'CUSTOMER_REFUND':
            out += [
                {'code': f'AP_{code}_{tag}', 'name': f'Payments · {label} · {title}', 'company': company,
                 'query': _where(f"company = '{company}'", NOT_STAFF, NOT_ACCOUNT, NOT_BILL, _purpose(code),
                                 *on_account),
                 'stages': _chain(owner, False)}
                for company, tag, title, owner in (('OIL', 'OIL', 'Oil', 'sales_oil'),
                                                   ('BEVERAGES', 'BEV', 'Beverages', 'sales_bev'))
            ]
        elif code in PLANT_PURPOSES:
            out += [
                {'code': f'AP_{code}_{tag}', 'name': f'Payments · {label} · {title}', 'company': company,
                 'query': _where(f"company = '{company}'", NOT_STAFF, NOT_ACCOUNT, *bills, _purpose(code),
                                 *on_account),
                 'stages': _chain(owner, True), **first}
                for company, tag, title, owner in PLANT_APPROVERS
            ]
        elif code in BACK_OFFICE_PURPOSES:
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': [(DEPARTMENT_HEAD_STAGE, HEAD),
                                                           (BACK_OFFICE_STAGE, 'backoffice')]})
        elif code in FIXED_OWNER:
            owner, director = FIXED_OWNER[code]
            stages = _chain(owner, director)
            if code in SECOND_OWNER:
                stages.append((SECOND_STAGE, SECOND_OWNER[code]))
            if code in NO_CA_PURPOSES:
                # No CA (2026-10-21): on its own route, a bill for it, a vendor on account.
                no_ca = {'no_ca': True}
                out += [
                    {'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                     'query': _where(*base, f'NOT {VENDOR_ON_ACCOUNT}'), 'stages': stages, **no_ca},
                    {'code': f'AP_{code}_AGAINST_BILL', 'name': f'Payments · {label} · Against Bill',
                     'company': 'ALL', 'query': _where(NOT_STAFF, NOT_ACCOUNT, AGAINST_BILL, _purpose(code)),
                     'stages': [], **no_ca},
                    {'code': f'AP_{code}_ON_ACCOUNT', 'name': f'Payments · {label} · Vendor On Account',
                     'company': 'ALL', 'query': _where(OIL_AND_BEV, NOT_STAFF, NOT_ACCOUNT, NOT_BILL,
                                                       VENDOR_ON_ACCOUNT, _purpose(code)),
                     'stages': [(DEPARTMENT_HEAD_STAGE, HEAD)], **no_ca},
                ]
                continue
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': stages, **first})
        elif code in DUE_SOON_PURPOSES:
            # Two routes, kept apart by the Bill Due Date: the usual one, and
            # straight to Payment for a bill due in DUE_SOON_DAYS days or fewer.
            # Every such purpose is a Department Head one (pinned by the tests).
            out += [
                {'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                 'query': _where(*base, f'NOT {BILL_DUE_SOON}'),
                 'stages': _chain(HEAD, code in HEAD_THEN_DIRECTOR)},
                {'code': f'AP_{code}{DUE_SOON_SUFFIX}',
                 'name': f'Payments · {label} · bill due in {DUE_SOON_DAYS} days or less',
                 'company': 'ALL', 'query': _where(*base, BILL_DUE_SOON), 'stages': []},
            ]
        elif code in HEAD_PURPOSES:
            stages = _chain(HEAD, code in HEAD_THEN_DIRECTOR)
            if code in HEAD_THEN_SECOND:
                stages.append((SECOND_STAGE, HEAD_THEN_SECOND[code]))
            out.append({'code': f'AP_{code}', 'name': f'Payments · {label}', 'company': 'ALL',
                        'query': _where(*base), 'stages': stages})
        else:
            raise ValueError(f'Purpose {code} has no route in the hierarchy.')
    # A vendor paid On Account for a fixed owner's purpose: the picked head (2026-10-21),
    # then the Director where that purpose's own route has him.
    def _in(codes):
        return 'purpose_code IN ({})'.format(', '.join(f"'{c}'" for c in codes))
    plain = [c for c in by_head if c not in with_director]
    fixed_assets = [c for c in with_director if c in FA_PURPOSES]
    with_director = [c for c in with_director if c not in FA_PURPOSES]
    out += [
        {'code': 'AP_VENDOR_ON_ACCOUNT', 'name': 'Payments · Vendor On Account · Department Head',
         'company': 'ALL', 'query': _where(OIL_AND_BEV, NOT_STAFF, NOT_ACCOUNT, NOT_BILL, VENDOR_ON_ACCOUNT, _in(plain)),
         'stages': [(DEPARTMENT_HEAD_STAGE, HEAD)]},
        {'code': 'AP_VENDOR_ON_ACCOUNT_DIRECTOR', 'name': 'Payments · Vendor On Account · Department Head, Director',
         'company': 'ALL', 'query': _where(OIL_AND_BEV, NOT_STAFF, NOT_ACCOUNT, NOT_BILL, VENDOR_ON_ACCOUNT, _in(with_director)),
         'stages': _chain(HEAD, True)},
        # Fixed Assets paid On Account (2026-10-22): the head, the Director, then Payment.
        {'code': 'AP_VENDOR_ON_ACCOUNT_FIXED_ASSETS',
         'name': 'Payments · Vendor On Account · Fixed Assets · Department Head, Director', 'company': 'ALL',
         'query': _where(OIL_AND_BEV, NOT_STAFF, NOT_ACCOUNT, NOT_BILL, VENDOR_ON_ACCOUNT, _in(fixed_assets)),
         'stages': _chain(HEAD, True), 'approvals_first': True},
    ]
    # Every bill, whatever it is for (freight, RM/PM, oil, Fixed Assets aside): straight to the Payment desk.
    out.append({'code': 'AP_AGAINST_BILL', 'name': 'Payments · Against Bill', 'company': 'ALL',
                'query': _where(AGAINST_BILL, f'NOT {FREIGHT_ROUTED}', f'NOT {RM_PM_ROUTED}',
                                f'NOT {OIL_PURCHASE_ROUTED}', f'NOT {ECOM_ROUTED}', f'NOT {FA_ROUTED}'),
                'stages': []})
    return _approvals_first(out) + expense_routes() + moved_routes()


def _approvals_first(routes):
    """Split each route with approvers in two (user, 2026-10-21): paid against
    an Advance, or a vendor against a PO (`APPROVALS_FIRST`), its approvers
    first, then Payment; anything else as before, Payment first. A route with
    no approver of its own (straight to Payment, or Navdeep's alone, who
    approves at CA), one already approvals-first, and `NOT_SPLIT` stay whole."""
    out = []
    for route in routes:
        approvers = [s for s in route['stages'] if not _held_by_ca(s[1])]
        if not approvers or route.get('approvals_first') or route.get('no_split') or route['code'] in NOT_SPLIT:
            out.append(route)
            continue
        # Fixed Assets (2026-10-22): both halves approvals-first; the split stays
        # only so the workflows (and the requests on them) keep their codes.
        always = {'approvals_first': True} if route.get('always_first') else {}
        out.append({**route, 'query': f"{route['query']} AND NOT {APPROVALS_FIRST}", **always})
        out.append({**route, 'code': route['code'] + APPROVALS_FIRST_SUFFIX,
                    'name': f"{route['name']} · Advance / against PO (approvals, then Payment)",
                    'query': f"{route['query']} AND {APPROVALS_FIRST}", 'approvals_first': True})
    return out


def _held_by_ca(role):
    """An approval the CA desk's own person would make: dropped, so they approve
    the request once, at CA (user, 2026-10-20) — freight, Mart's small imprests,
    TA/DA and incentives, the moved requests: Navdeep's approval IS his CA one."""
    return role != HEAD and PEOPLE.get(role) == PEOPLE['ca']


def full_stages(route):
    """All stages of a route, in order: `[(name, role)]`, the fixed ones last.

    Payment first, then the route's approvals, then CA -> Audit -> Final
    (user, 2026-10-20). An Expense route keeps its approvals FIRST, then
    Accounts (its own Payment desk, Satnam Singh) -> CA -> Expense Audit (its
    own auditor), whose approval posts (user, 2026-10-20). The CA desk is on
    every route (2026-10-19).
    """
    approvals = [(name, role) for name, role in route['stages'] if not _held_by_ca(role)]
    payment, ca = ('Payment Approval', 'payment'), (CA_STAGE, 'ca')
    if route.get('no_ca'):  # Oil Purchase (2026-10-21): Payment, its approvals, Audit, Final
        tail = [('Audit Approval', 'audit'), ('Final Approval', 'final')]
        return [*approvals, payment, *tail] if route.get('approvals_first') else [payment, *approvals, *tail]
    if route.get('expense'):
        return [*approvals, (ACCOUNTS_STAGE, 'accounts'), ca, (EXPENSE_AUDIT_STAGE, 'expense_audit')]
    if route.get('approvals_first'):  # a moved route: its approvals, then Payment
        return [*approvals, payment, ca, ('Audit Approval', 'audit'), ('Final Approval', 'final')]
    return [payment, *approvals, ca, ('Audit Approval', 'audit'), ('Final Approval', 'final')]
