"""Payment Purpose: what the money is FOR, and so who approves it.

The list began as the Payment Desk's (`payment-desk/config.json`,
`payment_purposes`, built 24-Sep-2026 from what the three books actually
show), copied with its codes unchanged so a request means the same thing in
both. Since 2026-10-05 it also follows the approval hierarchy, which routes by
purpose: oil (imported or domestic) has its own purpose, and Fixed Assets is
split three ways because each part has a different approver.

A code never changes once it exists: requests store it, and the workflow
queries match on it. Rename a label freely; retire a purpose by moving it to
`RETIRED` (requests that already carry it keep their saved label, and it can no
longer be chosen).

WHO APPROVES is configured in the Workflows page, one workflow per purpose
(`manage.py seed_payment_workflows` writes the hierarchy's). This module only
says which purposes go to the DEPARTMENT HEAD the requester picks on the form
rather than to a fixed person: `HEAD_PURPOSES`, plus every Employee and
Employee Imprest request. Mart routes everything to one approver and asks for
no Department Head.
"""

#: The groups, in the order the form lists them.
PURPOSE_GROUPS = ('Goods', 'Services', 'Back Office', 'People', 'Statutory', 'Finance')

#: `(code, label, group)`, in the order the form lists them.
PAYMENT_PURPOSES = (
    ('OIL_PURCHASE', 'Oil Purchase – Imported or Domestic', 'Goods'),
    ('RAW_MATERIAL', 'Raw Material – Other than Oil (incl. Ghee)', 'Goods'),
    ('PACKING_MATERIAL', 'Packaging Material Purchase', 'Goods'),
    ('FINISHED_GOODS', 'Finished Goods / Trading Purchase', 'Goods'),
    ('SEMI_FINISHED', 'Semi-Finished Goods', 'Goods'),
    ('FA_PM', 'Fixed Assets – Plant & Machinery', 'Goods'),
    ('FA_CIVIL', 'Fixed Assets – Civil', 'Goods'),
    ('FA_OTHERS', 'Fixed Assets – Others', 'Goods'),
    ('FA_CONSUMABLES', 'Fixed Asset Consumables / Spares', 'Goods'),
    ('CONSUMABLES', 'Consumables & Stores', 'Goods'),
    ('LAB', 'Laboratory / QC Materials', 'Goods'),
    ('FREIGHT_IN', 'Freight – Inward', 'Services'),
    ('FREIGHT_OUT', 'Freight – Outward / Transport', 'Services'),
    ('FREIGHT_IMPORT', 'Freight & Clearing – Import', 'Services'),
    ('JOB_WORK', 'Job Work / Refining', 'Services'),
    ('LABOUR', 'Casual Labour / Loading', 'Services'),
    ('ADVERTISING', 'Advertising & Marketing', 'Services'),
    ('COMMISSION', 'Commission & Brokerage', 'Services'),
    ('RENT', 'Rent', 'Services'),
    ('UTILITIES', 'Electricity, Water & Utilities', 'Services'),
    # Paid to the insurer on account before its bill, as SAP's own premiums are (2026-10-17).
    ('INSURANCE', 'Insurance Premium', 'Services'),
    ('PROFESSIONAL', 'Legal & Professional Fees', 'Services'),
    ('REPAIRS', 'Repairs & Maintenance', 'Services'),
    ('FUEL', 'Fuel & Vehicle Running', 'Services'),
    ('TRAVEL', 'Travel & Conveyance', 'Services'),
    ('SECURITY', 'Security & Housekeeping', 'Services'),
    ('IT_SOFTWARE', 'IT, Software & Subscriptions', 'Services'),
    ('OTHER_EXPENSE', 'Other Expense', 'Services'),
    # The Back Office functions (user, 2026-10-20): the head picked, then Nirmal Didi Ji.
    ('BO_HR', 'Back Office – Human Resources (HR)', 'Back Office'),
    ('BO_IT', 'Back Office – Information Technology (IT)', 'Back Office'),
    ('BO_LEGAL', 'Back Office – Legal', 'Back Office'),
    ('BO_ADMIN', 'Back Office – Administration', 'Back Office'),
    ('BO_ACCOUNTS', 'Back Office – Accounts', 'Back Office'),
    ('SALARY', 'Salary & Wages', 'People'),
    ('EMP_ADVANCE', 'Employee Advance', 'People'),
    ('EMP_IMPREST', 'Employee Imprest / Float', 'People'),
    ('EXPENSE_CLAIM', 'Employee Expense Claim', 'People'),
    # Paid to the employee's staff account (ORGV…), as SAP books them: 5630002 / 5630005 (2026-10-14).
    ('INCENTIVE', 'Employee Incentive', 'People'),
    ('TA_DA', 'TA / DA (Travelling & Daily Allowance)', 'People'),
    ('TDS', 'TDS / TCS', 'Statutory'),
    ('GST', 'GST', 'Statutory'),
    ('PF_ESI', 'EPF / ESIC / Labour Welfare', 'Statutory'),
    ('OTHER_STATUTORY', 'Other Statutory / Licence', 'Statutory'),
    ('BANK_CHARGES', 'Bank Charges', 'Finance'),
    ('INTEREST', 'Interest', 'Finance'),
    ('LOAN_REPAY', 'Loan / EMI Repayment', 'Finance'),
    ('INTERCOMPANY', 'Intercompany Transfer', 'Finance'),
    ('CUSTOMER_REFUND', 'Customer Refund', 'Finance'),
    ('CAPITAL', 'Land / Property / Capital', 'Finance'),
)

#: No longer offered; kept so an old request still reads as it was raised.
#: Fixed Assets was split into FA_PM / FA_CIVIL / FA_OTHERS on 2026-10-05.
RETIRED = {
    'FIXED_ASSETS': 'Fixed Assets Purchase',
}

#: Purposes the hierarchy sends to the Department Head the requester picks
#: ("by department"), not to a fixed approver. Electricity & Utilities then
#: goes on to Director Approval; that is configured on its workflow. (Fixed
#: Assets – Others goes to the plant's approver since 2026-10-16.)
HEAD_PURPOSES = frozenset({
    'FINISHED_GOODS', 'CONSUMABLES', 'LAB',
    'JOB_WORK', 'LABOUR', 'ADVERTISING', 'COMMISSION', 'RENT',
    'UTILITIES', 'INSURANCE', 'PROFESSIONAL', 'REPAIRS', 'FUEL', 'TRAVEL', 'SECURITY', 'IT_SOFTWARE',
    'OTHER_EXPENSE', 'EMP_ADVANCE', 'EMP_IMPREST', 'EXPENSE_CLAIM', 'INTERCOMPANY',
    'INCENTIVE', 'TA_DA',
    # The Back Office functions (2026-10-20): the requester picks the function's
    # head (Sarabjeet, Gurvinder, Karanpreet, Bikramjeet, Avtar), then Nirmal Didi Ji.
    'BO_HR', 'BO_IT', 'BO_LEGAL', 'BO_ADMIN', 'BO_ACCOUNTS',
})

#: Freight (user, 2026-10-19): whatever it is paid against, Navdeep approves
#: it first, then the Payment desk — no Department Head is asked. Not on an
#: Employee / Imprest request, which keeps its own route.
FREIGHT_PURPOSES = ('FREIGHT_IN', 'FREIGHT_OUT', 'FREIGHT_IMPORT')

#: Paid against bills (user, 2026-10-19): straight to the Payment desk — no
#: Department Head, no owner, no Director — whatever it is for (freight aside).
AGAINST_BILL = 'AGAINST_BILL'

#: Purposes paid by a due date (user, 2026-10-17): the requester types the
#: Bill Due Date — the due date printed on the utility's bill, the policy's
#: renewal date — and attaches the bill or policy that shows it. SAP cannot
#: say: these vendors' bills carry their posting date as the due date.
#:
#: Due DUE_SOON_DAYS days or fewer after the day it is raised, or already past,
#: the request skips its approval stages and goes straight to Payment Approval;
#: due later, it takes its usual route (Department Head, then Director). Due
#: 20 Oct: raised on the 17th or before, the full route; on the 18th or later,
#: straight to Payment (user, 2026-10-19). The Payment desk checks the date.
DUE_SOON_PURPOSES = frozenset({'UTILITIES', 'INSURANCE'})
DUE_SOON_DAYS = 2

#: The purposes paid by a due date ON ACCOUNT, as SAP pays electricity boards
#: and insurers (2026-10-17: 134 of 143 of their payments since April 2025).
#: Since 2026-10-21 a Vendor may be paid On Account for ANY purpose, through
#: the Department Head the requester picks (`needs_department_head`).
VENDOR_ON_ACCOUNT_PURPOSES = DUE_SOON_PURPOSES

#: Raw and Packing Material: Bhupinder first, then Payment, in every company and
#: however it is paid (user, 2026-10-21) — no Department Head is asked.
RM_PM_PURPOSES = frozenset({'RAW_MATERIAL', 'PACKING_MATERIAL'})

#: Request types that always go to the requester's Department Head, then the
#: Director (Staff Advance / Imprest), whatever the purpose.
HEAD_REQUEST_TYPES = frozenset({'EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST'})

#: Mart has one approver for everything: no Department Head is asked.
NO_HEAD_COMPANIES = frozenset({'MART'})

#: The plant's budget heads. A staff salary advance (Employee Advance) under
#: one goes to the plant's fixed approver, not the Department Head — the
#: approval matrix of 2026-10-09: up to `STAFF_ADVANCE_LIMIT`, Shunty (Oil) or
#: Arvinder (Beverages); above it, Gurpreet Ji ("Sonu Veerji").
FACTORY_BUDGETS = ('Factory', 'FACT_COM')
STAFF_ADVANCE_LIMIT = 20000

_LABELS = {code: label for code, label, _group in PAYMENT_PURPOSES}


def purpose_label(code):
    """The label of a purpose that can be chosen, or '' for any other code."""
    return _LABELS.get(code, '')


def saved_label(code):
    """The label of any purpose a request may carry, retired ones included."""
    return _LABELS.get(code) or RETIRED.get(code, '')


def needs_department_head(company, request_type, purpose_code, budget_code='', payment_against=''):
    """Whether this request is approved by the Department Head picked on the form."""
    if company in NO_HEAD_COMPANIES or request_type == 'ACCOUNT':
        return False  # Mart's one approver; an Account Payment's fixed route
    if payment_against == AGAINST_BILL:
        return False  # straight to the Payment desk (user, 2026-10-19)
    if request_type == 'EMPLOYEE_IMPREST' and payment_against == 'AGAINST_JOURNAL':
        return False  # Navdeep alone, every company, any amount (user, 2026-10-21)
    if purpose_code in FREIGHT_PURPOSES and request_type not in HEAD_REQUEST_TYPES:
        return False  # Navdeep, then the Payment desk
    if request_type == 'EMPLOYEE_ADVANCE' and budget_code in FACTORY_BUDGETS:
        return False  # the plant's fixed approver, by amount
    if purpose_code in RM_PM_PURPOSES and request_type not in HEAD_REQUEST_TYPES:
        return False  # Bhupinder, however it is paid, in every company (user, 2026-10-21)
    if request_type == 'VENDOR' and payment_against == 'ON_ACCOUNT':
        return True  # any purpose, through the head picked (user, 2026-10-21)
    return request_type in HEAD_REQUEST_TYPES or purpose_code in HEAD_PURPOSES


def is_due_soon(bill_due_date, today):
    """Due DUE_SOON_DAYS days or fewer after the day it is raised, or already
    past: straight to Payment — no Department Head, no Director, and paid the
    day it is raised (user, 2026-10-19). Due 20 Oct: raised on the 18th or
    later, straight to Payment; on the 17th or before, the head and the
    Director. The SQL twin is `hierarchy.BILL_DUE_SOON`."""
    return bill_due_date is not None and (bill_due_date - today).days <= DUE_SOON_DAYS


def needs_bill_due_date(company, request_type, purpose_code, payment_against=''):
    """Whether the request names its Bill Due Date: a `DUE_SOON_PURPOSES`
    purpose on a route that reads it — the purpose routes of Oil and
    Beverages, and Mart's (since 2026-10-21: due soon, straight to Payment;
    else Prabhjot). The Employee / Imprest route, Account Payments and
    Expenses are not routed by purpose, so they are not asked."""
    if request_type in HEAD_REQUEST_TYPES:
        return False
    if request_type in ('ACCOUNT', 'EXPENSE'):
        return False
    if payment_against == AGAINST_BILL:
        return False  # straight to the Payment desk whenever it is due
    return purpose_code in DUE_SOON_PURPOSES


def purposes():
    """`[{code, label, group, needs_head, due_date}]`, as the form lists them.

    `needs_head`: this purpose (outside Mart) is approved by the Department
    Head the requester picks. `due_date`: it is paid by a Bill Due Date the
    requester types (`DUE_SOON_PURPOSES`), and a Vendor may be paid on account
    for it (`VENDOR_ON_ACCOUNT_PURPOSES`).
    """
    return [{'code': code, 'label': label, 'group': group, 'needs_head': code in HEAD_PURPOSES,
             'due_date': code in DUE_SOON_PURPOSES}
            for code, label, group in PAYMENT_PURPOSES]
