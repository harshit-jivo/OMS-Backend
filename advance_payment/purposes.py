"""Payment Purpose: what the money is FOR.

The list the Payment Desk uses (`payment-desk/config.json`, `payment_purposes`,
built 24-Sep-2026 from what the three books actually show), copied here with
its codes unchanged so a request means the same thing in both. A code never
changes once it exists: requests store it, and workflow queries may match on
it. Rename a label freely; retire a purpose by removing it here (requests that
already carry it keep their saved label).
"""

#: The groups, in the order the form lists them.
PURPOSE_GROUPS = ('Goods', 'Services', 'People', 'Statutory', 'Finance')

#: `(code, label, group)`, in the order the form lists them.
PAYMENT_PURPOSES = (
    ('RAW_MATERIAL', 'Raw Material Purchase', 'Goods'),
    ('PACKING_MATERIAL', 'Packaging Material Purchase', 'Goods'),
    ('FINISHED_GOODS', 'Finished Goods / Trading Purchase', 'Goods'),
    ('SEMI_FINISHED', 'Semi-Finished Goods', 'Goods'),
    ('FIXED_ASSETS', 'Fixed Assets Purchase', 'Goods'),
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

_LABELS = {code: label for code, label, _group in PAYMENT_PURPOSES}


def purpose_label(code):
    """The label of a known purpose code, or '' for an unknown one."""
    return _LABELS.get(code, '')


def purposes():
    """`[{code, label, group}]`, as the form lists them."""
    return [{'code': code, 'label': label, 'group': group} for code, label, group in PAYMENT_PURPOSES]
