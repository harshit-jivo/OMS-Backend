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
PURPOSE_GROUPS = ('Goods', 'Services', 'People', 'Statutory', 'Finance')

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
    ('PROFESSIONAL', 'Legal & Professional Fees', 'Services'),
    ('REPAIRS', 'Repairs & Maintenance', 'Services'),
    ('FUEL', 'Fuel & Vehicle Running', 'Services'),
    ('TRAVEL', 'Travel & Conveyance', 'Services'),
    ('SECURITY', 'Security & Housekeeping', 'Services'),
    ('IT_SOFTWARE', 'IT, Software & Subscriptions', 'Services'),
    ('OTHER_EXPENSE', 'Other Expense', 'Services'),
    ('SALARY', 'Salary & Wages', 'People'),
    ('EMP_ADVANCE', 'Employee Advance', 'People'),
    ('EMP_IMPREST', 'Employee Imprest / Float', 'People'),
    ('EXPENSE_CLAIM', 'Employee Expense Claim', 'People'),
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
#: ("by department"), not to a fixed approver. Electricity & Utilities and
#: Fixed Assets – Others then go on to Director Approval; that is configured
#: on their workflows.
HEAD_PURPOSES = frozenset({
    'FINISHED_GOODS', 'FA_OTHERS', 'CONSUMABLES', 'LAB',
    'FREIGHT_IN', 'FREIGHT_OUT', 'JOB_WORK', 'LABOUR', 'ADVERTISING', 'COMMISSION', 'RENT',
    'UTILITIES', 'PROFESSIONAL', 'REPAIRS', 'FUEL', 'TRAVEL', 'SECURITY', 'IT_SOFTWARE',
    'OTHER_EXPENSE', 'EMP_ADVANCE', 'EMP_IMPREST', 'EXPENSE_CLAIM', 'INTERCOMPANY',
})

#: Request types that always go to the requester's Department Head, then the
#: Director (Staff Advance / Imprest), whatever the purpose.
HEAD_REQUEST_TYPES = frozenset({'EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST'})

#: Mart has one approver for everything: no Department Head is asked.
NO_HEAD_COMPANIES = frozenset({'MART'})

_LABELS = {code: label for code, label, _group in PAYMENT_PURPOSES}


def purpose_label(code):
    """The label of a purpose that can be chosen, or '' for any other code."""
    return _LABELS.get(code, '')


def saved_label(code):
    """The label of any purpose a request may carry, retired ones included."""
    return _LABELS.get(code) or RETIRED.get(code, '')


def needs_department_head(company, request_type, purpose_code):
    """Whether this request is approved by the Department Head picked on the form."""
    if company in NO_HEAD_COMPANIES:
        return False
    return request_type in HEAD_REQUEST_TYPES or purpose_code in HEAD_PURPOSES


def purposes():
    """`[{code, label, group, needs_head}]`, as the form lists them.

    `needs_head`: this purpose (outside Mart) is approved by the Department
    Head the requester picks.
    """
    return [{'code': code, 'label': label, 'group': group, 'needs_head': code in HEAD_PURPOSES}
            for code, label, group in PAYMENT_PURPOSES]
