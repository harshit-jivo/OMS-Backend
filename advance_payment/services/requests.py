"""Raising and changing a payment request: what the creator's form becomes.

WHAT THE SERVER CHECKS AGAIN
----------------------------
The form (`rules.ts`) already refuses anything incomplete, but the browser is
not the authority: whatever reaches this module becomes a payment instruction.
So `clean()` re-checks everything that decides money or routing:

  * the type / Payment Against pair is one the form offers (`CASES`);
  * a case with documents pays through them, each line within what SAP still
    has open, a percentage line worth what its percentage says, and the
    request's amount is their total;
  * a case without documents carries a typed amount above zero;
  * a customer refund against the ledger is its credits less its debits,
    and that is above zero;
  * no line takes more than is still available once what other OMS
    requests hold is counted (`services/reservations.py`), checked again
    inside the save under a per-document lock;
  * the Department is one of the company's budget heads in SAP, and the
    Payment Purpose one of `advance_payment.purposes`;
  * a request approved by department names its Department Head, an HOD of
    the employee master with an OMS login to approve with;
  * the dates the case asks for are there.

It does NOT re-read SAP for the documents. The snapshot is what the creator
saw and what the approvers are shown; the SAP-side truth is checked when it
matters, at the Audit stage, when the payment is posted and SAP itself
refuses a closed bill or an over-payment.

Files are handled here too, because a request and the files raised with it
are saved in one transaction: a request whose supporting files failed to save
is not one the approvers should see.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from advance_payment.models import (
    AdvanceRequest,
    ExpenseLine,
    AllocationMode,
    DocumentKind,
    LedgerDirection,
    Employee,
    FilePurpose,
    PaymentAgainst,
    RequestDocument,
    RequestFile,
    RequestType,
    ReturnMethod,
)
from advance_payment.purposes import needs_department_head, purpose_label
from core.companies import COMPANY_CODES

#: The form's `NOT_IN_SAP_PREFIX`: an employee from the master with no SAP
#: advance account yet, e.g. `NOSAP:JWPL0999`.
NOT_IN_SAP_PREFIX = 'NOSAP:'

#: Upload cap, as the form states it (`MAX_FILE_SIZE_MB`).
MAX_FILE_BYTES = 10 * 1024 * 1024

#: The pairs the form offers (`CASE_RULES` in rules.ts), and what each asks.
#: `documents`: the kind of SAP document it pays through, or None for a typed
#: amount.
CASES = {
    # The Expected Bill Date is asked, but optional (since 2026-10-07).
    (RequestType.VENDOR, PaymentAgainst.AGAINST_PO): {'documents': DocumentKind.PO, 'expected_date': True},
    (RequestType.VENDOR, PaymentAgainst.AGAINST_BILL): {'documents': DocumentKind.BILL},
    (RequestType.EMPLOYEE_ADVANCE, PaymentAgainst.ADVANCE): {'documents': None, 'repayment': True},
    (RequestType.EMPLOYEE_ADVANCE, PaymentAgainst.OTHER): {'documents': None},
    (RequestType.EMPLOYEE_IMPREST, PaymentAgainst.ADVANCE): {'documents': None, 'expected_bill_date': True},
    # The bills are the expense already: there is no bill still to expect.
    (RequestType.EMPLOYEE_IMPREST, PaymentAgainst.AGAINST_BILL): {'documents': DocumentKind.BILL},
    (RequestType.EMPLOYEE_IMPREST, PaymentAgainst.OTHER): {'documents': None, 'expected_bill_date': True},
    # A refund: what the customer is owed back. Against their open ledger
    # items, or a typed amount applied to nothing.
    (RequestType.CUSTOMER, PaymentAgainst.AGAINST_LEDGER): {'documents': DocumentKind.LEDGER},
    (RequestType.CUSTOMER, PaymentAgainst.ON_ACCOUNT): {'documents': None},
    # Paid straight to expense G/L accounts: its lines (ExpenseLine) are the
    # payment, each with Month / Budget / Sub Budget. No SAP partner.
    (RequestType.EXPENSE, PaymentAgainst.DIRECT_EXPENSE): {'documents': None, 'expense': 'DIRECT'},
    (RequestType.EXPENSE, PaymentAgainst.INDIRECT_EXPENSE): {'documents': None, 'expense': 'INDIRECT'},
}

#: An Expense request takes at most this many lines.
MAX_EXPENSE_LINES = 50

#: An Expense line's GST, for the record only (SAP is not told it):
#: `{code: (label, rate %)}`.
EXPENSE_GST = {
    '': ('No GST', Decimal('0')),
    'CGST_SGST_5': ('CGST + SGST 5%', Decimal('5')),
    'CGST_SGST_18': ('CGST + SGST 18%', Decimal('18')),
    'IGST_5': ('IGST 5%', Decimal('5')),
    'IGST_18': ('IGST 18%', Decimal('18')),
}
#: A line's TDS choice meaning "no TDS on this line", whatever the request's.
NO_TDS = 'NONE'
_PAISA = Decimal('0.01')

#: The ledger items a refund may be applied to, by SAP object type, and the
#: PaymentInvoices type each posts as. A/R invoices and credit memos are
#: addressed by their own DocEntry; receipts and journal entries through the
#: journal (TransId + line). Read off SAP's own refunds (Oil, Sept 2026).
LEDGER_OBJECTS = {
    13: {'invoice_type': 'it_Invoice', 'by_journal': False},
    14: {'invoice_type': 'it_CredItnote', 'by_journal': False},
    24: {'invoice_type': 'it_Receipt', 'by_journal': True},
    30: {'invoice_type': 'it_JournalEntry', 'by_journal': True},
}

CENT = Decimal('0.01')

#: `Preshit Singh (JWPL0030)` -> JWPL0030.
_OWNER_CODE = re.compile(r'\(([A-Za-z0-9]+)\)\s*$')


class RequestInvalid(Exception):
    """The submitted request breaks a rule. `problems` lists every one found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(' '.join(self.problems))


def _money(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text(value, limit=None):
    text = value.strip() if isinstance(value, str) else ('' if value is None else str(value))
    return text[:limit] if limit else text


def _int(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


@dataclass
class CleanRequest:
    """A validated request, ready to be written."""

    fields: dict
    documents: list = field(default_factory=list)
    #: Expense only: `[{line_no, amount, gl_account, gl_name, effect_month, remarks}]`.
    expense_lines: list = field(default_factory=list)


def clean(data, *, desk=False, user=None):
    """Validate the form's JSON. Returns `CleanRequest`; raises `RequestInvalid`.

    `desk`: the Payment desk's correction of an Expense, which may also set
    its TDS (the requester's form never does). `user`: who is raising it — an
    Expense with no vendor is paid to them.
    """
    problems = []
    data = data or {}

    company = _text(data.get('company')).upper()
    if company not in COMPANY_CODES:
        problems.append('Company must be one of: ' + ', '.join(COMPANY_CODES) + '.')

    request_type = _text(data.get('request_type')).upper()
    against = _text(data.get('payment_against')).upper()
    if request_type == RequestType.EXPENSE:
        # Not asked: direct or indirect follows from the lines' G/L accounts
        # (`_clean_expense`). Indirect until a direct one is chosen.
        against = PaymentAgainst.INDIRECT_EXPENSE
    case = CASES.get((request_type, against))
    if case is None:
        problems.append('That Type and Payment Against pair is not offered.')
        case = {'documents': None}

    partner_code = _text(data.get('partner_code'), 50)
    partner_name = _text(data.get('partner_name'), 200)
    if case.get('expense'):
        # The money goes to G/L accounts. Who is paid is named; a SAP vendor is
        # optional (Payment is then offered its bank accounts), and names them.
        partner_name = _clean_expense_vendor(company, partner_code, partner_name, problems)
        if not partner_name and user is not None:
            # Not asked: no vendor, so the person raising it is who is paid.
            partner_name = (getattr(user, 'name', '') or user.get_username())[:200]
        if not partner_name:
            problems.append('Say who is being paid (Pay To).')
    elif not partner_code or not partner_name:
        problems.append('Choose who is being paid.')
    not_in_sap = partner_code.startswith(NOT_IN_SAP_PREFIX)
    if not_in_sap and request_type != RequestType.EMPLOYEE_ADVANCE:
        problems.append('Only an Employee advance may be raised for someone not yet in SAP.')

    documents = []
    expense_lines = []
    expense = {}
    if case.get('expense'):
        if data.get('documents'):
            problems.append('An Expense request pays its lines, not documents.')
        expense = _clean_expense(company, data, problems, desk=desk, partner_code=partner_code)
        expense_lines = expense.pop('lines', [])
        against = PaymentAgainst.DIRECT_EXPENSE if expense.pop('kind', '') == 'DIRECT' \
            else PaymentAgainst.INDIRECT_EXPENSE
        amount = sum((ln['amount'] for ln in expense_lines), Decimal('0'))
    elif case['documents']:
        documents, amount = _clean_documents(data.get('documents'), case['documents'], problems)
    else:
        if data.get('documents'):
            problems.append('This Payment Against takes a typed amount, not documents.')
        amount = _money(data.get('amount'))
        if amount is None or amount <= 0:
            problems.append('Enter an amount above zero.')

    payment_date = _date(data.get('payment_date'))
    if payment_date is None and case.get('expense') and not _text(data.get('payment_date')):
        payment_date = timezone.localdate()  # not asked: the day it is raised
    if payment_date is None:
        problems.append('Enter the Payment Date.')

    # Vendor -> Against PO's Expected Bill Date is optional: blank is fine, but
    # something typed must be a date.
    expected_date = _date(data.get('expected_date')) if case.get('expected_date') else None
    if case.get('expected_date') and expected_date is None and _text(data.get('expected_date')):
        problems.append('The Expected Bill Date is not a valid date.')
    expected_bill_date = _date(data.get('expected_bill_date')) if case.get('expected_bill_date') else None
    if case.get('expected_bill_date') and expected_bill_date is None:
        problems.append('Enter the Expected Bill Date.')

    repayment = _clean_repayment(data, problems) if case.get('repayment') else {}

    if not _text(data.get('remarks')) and not case.get('expense'):
        problems.append('Enter the Remarks.')  # optional on an Expense

    routing = _clean_routing(company, data, problems, expense=bool(case.get('expense')))
    routing['department_head_employee'], routing['department_head'] = _clean_department_head(
        company, request_type, routing.get('purpose_code', ''), data, problems)
    routing.update(effect_month=expense.get('effect_month', ''),
                   is_electricity=bool(expense.get('is_electricity')),
                   expense_tds_code=expense.get('tds_code', ''))

    # The form shows owners as "Preshit Singh (JWPL0030)": the code in the
    # brackets finds them in the employee master. A label with no code (a
    # request raised before owners came from the master) is kept as text.
    owner = None
    owner_label = _text(data.get('owner_label'), 200)
    owner_code = _OWNER_CODE.search(owner_label)
    if owner_code:
        owner = Employee.objects.alive().filter(employee_code=owner_code.group(1).upper()).first()
        if owner is None:
            problems.append('That Ownership employee is not in the employee master.')
    elif not owner_label and not case.get('expense'):
        problems.append('Choose the Ownership HOD or Sub-HOD.')  # not asked on an Expense

    against_other = _text(data.get('payment_against_other'), 120) if against == PaymentAgainst.OTHER else ''
    if against == PaymentAgainst.OTHER and not against_other:
        problems.append('Say what the payment is against.')

    if problems:
        raise RequestInvalid(problems)

    return CleanRequest(
        fields={
            'company': company,
            'request_type': request_type,
            'payment_against': against,
            'payment_against_other': against_other,
            'partner_code': partner_code,
            'partner_name': partner_name,
            'partner_not_in_sap': not_in_sap,
            'amount': amount,
            'expected_date': expected_date,
            'expected_bill_date': expected_bill_date,
            'return_method': repayment.get('return_method', ''),
            'return_method_other': repayment.get('return_method_other', ''),
            'installments': repayment.get('installments'),
            'emi_amount': repayment.get('emi_amount'),
            'expected_from_date': repayment.get('expected_from_date'),
            'expected_to_date': repayment.get('expected_to_date'),
            'payment_date': payment_date,
            **routing,
            'remarks': _text(data.get('remarks')),
            'owner_employee': owner,
            'owner_label': owner_label,
        },
        documents=documents,
        expense_lines=expense_lines,
    )


def _clean_expense_vendor(company, partner_code, partner_name, problems):
    """An Expense's optional vendor must be one of SAP's. Returns who is paid:
    the name typed, else the vendor's own."""
    if not partner_code or company not in COMPANY_CODES:
        return partner_name
    from advance_payment.services import sap as sap_service

    try:
        found = sap_service.partner(company, partner_code)
    except sap_service.SapUnavailable:
        problems.append('Could not check the vendor with SAP. Try again shortly.')
        return partner_name
    if found is None or found['party_type'] != 'vendor':
        problems.append(f'{partner_code} is not a vendor in {company}\'s SAP.')
        return partner_name
    return partner_name or found['card_name']


def form_of(advance):
    """An Expense request as its form sends it: what the Payment desk's
    corrections are laid over before `clean` checks the whole again."""
    return {
        'company': advance.company, 'request_type': advance.request_type,
        'payment_against': advance.payment_against, 'partner_code': advance.partner_code,
        'partner_name': advance.partner_name,
        'payment_date': advance.payment_date.isoformat() if advance.payment_date else '',
        'remarks': advance.remarks, 'owner_label': advance.owner_label,
        'budget_code': advance.budget_code, 'sub_budget_code': advance.sub_budget_code,
        'effect_month': advance.effect_month, 'is_electricity': advance.is_electricity,
        'expense_tds_code': advance.expense_tds_code,
        'expense_lines': [{'amount': str(ln.amount), 'gst_code': ln.gst_code,
                           'gl_account': ln.gl_account, 'effect_month': ln.effect_month,
                           'remarks': ln.remarks, 'tds_override': ln.tds_override}
                          for ln in advance.expense_lines.all()],
    }


#: What the Payment desk may correct on an Expense request.
#: Not the company, the budget head, who is paid (the vendor) or any amount:
#: those are the requester's (return it to them to change one). The lines'
#: amounts are checked unchanged by `flow.edit_expense`.
EXPENSE_EDITABLE = ('sub_budget_code', 'effect_month', 'is_electricity', 'expense_tds_code', 'expense_lines')
#: ...and the request's fields that follow from them (direct or indirect, by the G/Ls).
_EXPENSE_FIELDS = ('payment_against', 'sub_budget_code', 'sub_budget_name', 'effect_month', 'is_electricity',
                   'expense_tds_code')


def edit_expense(advance, cleaned):
    """Write the Payment desk's corrections (a `clean`ed form). Returns what
    changed, as `update` does: `{field: {old, new}}` and `expense_lines`."""
    before, lines_before = _snapshot(advance), _expense_lines(advance)
    for name in _EXPENSE_FIELDS:
        setattr(advance, name, cleaned.fields[name])
    advance.save()
    _write_expense_lines(advance, cleaned.expense_lines)
    after, lines_after = _snapshot(advance), _expense_lines(advance)
    changes = {k: {'old': before[k], 'new': after[k]} for k in after if before[k] != after[k]}
    if lines_after != lines_before:
        changes['expense_lines'] = {k: {'old': lines_before.get(k), 'new': lines_after.get(k)}
                                    for k in sorted(set(lines_before) | set(lines_after))
                                    if lines_before.get(k) != lines_after.get(k)}
    return changes


def _clean_expense(company, data, problems, *, desk=False, partner_code=''):
    """An Expense request's month, electricity flag, TDS and lines.

    `{effect_month, is_electricity, tds_code, kind, lines: [...]}`. Each line
    is an amount (the invoice value) to a G/L account; without one, the
    remarks say what it is for and the Payment desk picks it. `kind` — DIRECT
    or INDIRECT — follows from the G/L accounts; one request is one kind.
    The month is the day it is raised (the Payment desk may change it).

    `desk`: the Payment desk's own fields — each line's GST (the record of how
    much of the amount is tax: the taxable amount is backed out of it) and the
    TDS (the request's `expense_tds_code`, each line's `tds_override`) on the
    taxable amount. The requester's form never carries them.
    """
    from advance_payment.services import payout as payout_service
    from advance_payment.services import sap as sap_service

    out = {'is_electricity': bool(data.get('is_electricity')), 'lines': [], 'tds_code': '', 'kind': ''}
    month = _text(data.get('effect_month'), 20)
    asked_month = bool(month)
    if not month and not desk:
        month = month_code(timezone.localdate())
    raw = data.get('expense_lines')
    if not isinstance(raw, list) or not raw:
        problems.append('Add at least one expense line.')
        raw = []
    if len(raw) > MAX_EXPENSE_LINES:
        problems.append(f'At most {MAX_EXPENSE_LINES} expense lines.')
        raw = raw[:MAX_EXPENSE_LINES]
    accounts, months, codes = {}, set(), {}
    if company in COMPANY_CODES:
        try:
            accounts = {a['code']: a for a in sap_service.expense_accounts(company)}
            months = set(sap_service.expense_months(company))
            if desk:
                codes = {c['code']: c for c in sap_service.tds_codes(company, partner_code)}
        except sap_service.SapUnavailable:
            problems.append('Could not check the expense accounts with SAP. Try again shortly.')
            return {**out, 'effect_month': month}
    if asked_month and months and month not in months:
        problems.append(f'Month "{month}" is not one of SAP\'s Effective Months.')

    def tds_of(code, where):
        """The TDS fields for a code, or None (with a problem) when SAP has no such code."""
        chosen = codes.get(code)
        if chosen is None:
            problems.append(f'{where}: {code} is not an active TDS code at 1, 2, 5 or 10% in SAP.')
        return chosen

    request_tds = _text(data.get('expense_tds_code'), 20) if desk else ''
    if request_tds and codes:
        tds_of(request_tds, 'TDS')
    out['tds_code'] = request_tds
    kinds = {}
    for n, line in enumerate(raw, start=1):
        line = line if isinstance(line, dict) else {}
        amount = _money(line.get('amount'))
        gst_code = _text(line.get('gst_code'), 20).upper() if desk else ''
        gl = _text(line.get('gl_account'), 20)
        line_month = _text(line.get('effect_month'), 20)
        remarks = _text(line.get('remarks'), 254)
        if amount is None or amount <= 0:
            problems.append(f'Line {n}: enter an amount above zero.')
            amount = None
        if gst_code not in EXPENSE_GST:
            problems.append(f'Line {n}: "{gst_code}" is not a GST choice.')
            gst_code = ''
        if gl and accounts and gl not in accounts:
            problems.append(f'Line {n}: {gl} is not an expense account in SAP.')
        elif gl and gl in accounts:
            kinds.setdefault(accounts[gl]['kind'], []).append(n)
        if not gl and not remarks:
            problems.append(f'Line {n}: choose the G/L account, or say in the remarks what it is for.')
        if line_month and months and line_month not in months:
            problems.append(f'Line {n}: month "{line_month}" is not one of SAP\'s Effective Months.')
        amount = amount or Decimal('0')
        rate = EXPENSE_GST[gst_code][1]
        taxable = (amount * 100 / (100 + rate)).quantize(_PAISA, rounding=ROUND_HALF_UP)
        cleaned = {'line_no': n, 'taxable_amount': taxable, 'gst_code': gst_code, 'gst_amount': amount - taxable,
                   'amount': amount, 'gl_account': gl,
                   'gl_name': accounts[gl]['name'] if gl in accounts else '',
                   # The request's month is every line's unless it names its own.
                   'effect_month': '' if line_month == month else line_month,
                   'remarks': remarks,
                   'tds_override': '', 'tds_code': '', 'tds_label': '', 'tds_rate': None, 'tds_account': '',
                   'tds_amount': Decimal('0')}
        if desk:
            override = _text(line.get('tds_override'), 20)
            cleaned['tds_override'] = override
            code = '' if override == NO_TDS else (override or request_tds)
            chosen = tds_of(code, f'Line {n}') if code and codes else None
            if chosen:
                rate = Decimal(chosen['rate'])
                cleaned.update(tds_code=code, tds_label=chosen['name'][:150], tds_rate=rate,
                               tds_account=chosen['account'],
                               tds_amount=payout_service.tds_amount(taxable, rate))
        out['lines'].append(cleaned)
    if len(kinds) > 1:
        problems.append(f'One request is either direct or indirect expenses: lines '
                        f'{", ".join(map(str, kinds["DIRECT"]))} are direct, lines '
                        f'{", ".join(map(str, kinds["INDIRECT"]))} indirect. Raise them separately.')
    out['kind'] = 'DIRECT' if set(kinds) == {'DIRECT'} else 'INDIRECT'
    return {**out, 'effect_month': month}


def month_code(day):
    """SAP's Effective Month code for a date: 2026-10-08 -> "10-2026"."""
    return f'{day.month:02d}-{day.year}'


def _ledger_fields(doc, label, problems):
    """A ledger item's SAP object, line and side, or None (with a problem)."""
    obj = _int(doc.get('sap_object'))
    direction = _text(doc.get('direction')).upper()
    if obj not in LEDGER_OBJECTS:
        problems.append(f'Ledger item {label}: only incoming payments, credit memos, invoices and '
                        f'journal entries can be refunded against.')
        return None
    if direction not in LedgerDirection.values:
        problems.append(f'Ledger item {label} must be a credit or a debit.')
        return None
    line = _int(doc.get('sap_line')) or 0
    if line < 0 or (line and not LEDGER_OBJECTS[obj]['by_journal']):
        problems.append(f'Ledger item {label} has a journal line it cannot have.')
        return None
    return {'sap_object': obj, 'sap_line': line, 'direction': direction}


def _clean_documents(raw, kind, problems):
    """The document lines, and their total. Appends to `problems`.

    For a customer's ledger items the total is SIGNED: credits (owed to the
    customer) add, debits (invoices they owe) subtract, exactly as SAP nets
    an outgoing payment to a customer.
    """
    if not isinstance(raw, list) or not raw:
        problems.append('Choose at least one document to pay.')
        return [], None
    lines, seen, total = [], set(), Decimal('0')
    for index, doc in enumerate(raw, start=1):
        doc = doc or {}
        entry = _int(doc.get('sap_doc_entry'))
        label = _text(doc.get('sap_doc_num')) or str(entry or index)
        if not entry:
            problems.append(f'Document {index} has no SAP entry.')
            continue
        ledger = _ledger_fields(doc, label, problems) if kind == DocumentKind.LEDGER else None
        if kind == DocumentKind.LEDGER and ledger is None:
            continue
        key = (entry, ledger['sap_line'] if ledger else 0)
        if key in seen:
            problems.append(f'Document {label} is chosen twice.')
            continue
        seen.add(key)
        if _text(doc.get('kind')).upper() not in ('', kind):
            problems.append(f'Document {label} is not a {DocumentKind(kind).label}.')
            continue
        original = _money(doc.get('original_amount')) or Decimal('0')
        paid = _money(doc.get('paid_amount')) or Decimal('0')
        open_amount = _money(doc.get('open_amount'))
        amount = _money(doc.get('amount'))
        mode = _text(doc.get('mode')).upper() or AllocationMode.FIXED
        percentage = None
        if open_amount is None or open_amount <= 0:
            problems.append(f'Document {label} has nothing open to pay.')
            continue
        if mode not in AllocationMode.values or (mode == AllocationMode.PERCENT and kind != DocumentKind.PO):
            problems.append(f'Document {label}: a bill is paid by amount only.')
            continue
        if mode == AllocationMode.PERCENT:
            percentage = _money(doc.get('percentage'))
            if percentage is None or not (0 < percentage <= 100):
                problems.append(f'Document {label}: the percentage must be above 0 and at most 100.')
                continue
            worth = (open_amount * percentage / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            if amount is None or abs(amount - worth) > CENT:
                amount = worth
        if amount is None or amount <= 0:
            problems.append(f'Document {label}: enter an amount above zero.')
            continue
        if amount > open_amount:
            problems.append(f'Document {label}: {amount} is more than the {open_amount} still open.')
            continue
        total += -amount if ledger and ledger['direction'] == LedgerDirection.DEBIT else amount
        lines.append({
            'kind': kind,
            'sap_doc_entry': entry,
            **(ledger or {}),
            'sap_doc_num': _text(doc.get('sap_doc_num'), 30),
            'vendor_ref': _text(doc.get('vendor_ref'), 100),
            'doc_date': _date(doc.get('doc_date')),
            'due_date': _date(doc.get('due_date')),
            'original_amount': original,
            'paid_amount': paid,
            'open_amount': open_amount,
            'mode': mode,
            'percentage': percentage,
            'amount': amount,
            'attachment_file': _text(doc.get('attachment_file'), 255),
            'attachment_count': max(0, _int(doc.get('attachment_count')) or 0),
            'attachment_date': _date(doc.get('attachment_date')),
            'attachment_check': doc.get('attachment_check') if isinstance(doc.get('attachment_check'), dict) else None,
        })
    if kind == DocumentKind.LEDGER and lines and total <= 0:
        problems.append('The credits chosen must come to more than the invoices: the refund is what '
                        'the customer is owed, credits less debits.')
    return lines, total


def _clean_routing(company, data, problems, *, expense=False):
    """Department (SAP's budget head) and Payment Purpose: both required.

    The budget head must be one of the company's active dimension-3 cost
    centres in SAP; the purpose one of `advance_payment.purposes`. Sub Budget
    is no longer asked, so it is cleared — except on an Expense request, which
    is routed by its budget head (no purpose) and needs its Sub Budget, as
    every expense line in SAP carries one.
    """
    from advance_payment.services import sap as sap_service

    out = {}
    purpose = _text(data.get('purpose_code'), 30).upper()
    if expense:
        out.update(purpose_code='', purpose_label='')
    elif not purpose:
        problems.append('Choose the Payment Purpose.')
    elif not purpose_label(purpose):
        problems.append(f'"{purpose}" is not a Payment Purpose.')
    else:
        out.update(purpose_code=purpose, purpose_label=purpose_label(purpose))

    budget = _text(data.get('budget_code'), 20)
    if not budget:
        problems.append('Choose the Department.')
        return out
    if company not in COMPANY_CODES:
        return out
    try:
        known = sap_service.budgets(company)
    except sap_service.SapUnavailable:
        problems.append('Could not check the Department with SAP. Try again shortly.')
        return out
    names = {r['code']: r['name'] for r in known if r['kind'] == 'BUDGET'}
    if budget not in names:
        problems.append(f'Department "{budget}" is not an active budget head in {company}.')
        return out
    out.update(budget_code=budget, budget_name=names[budget], sub_budget_code='', sub_budget_name='')
    if expense:
        subs = {r['code']: r['name'] for r in known if r['kind'] == 'SUB_BUDGET'}
        sub_budget = _text(data.get('sub_budget_code'), 20)
        allowed = sap_service.SUB_BUDGET_RULES.get(company, {}).get(budget)
        if not sub_budget:
            pass  # the Payment desk's to set (`flow.expense_problems`)
        elif sub_budget not in subs:
            problems.append(f'Sub Budget "{sub_budget}" is not an active sub budget in {company}.')
        elif allowed is not None and sub_budget not in allowed:
            # SAP refuses the payment otherwise: its budget rules tie these heads to these sub budgets.
            problems.append(f'Under {budget}, SAP allows the Sub Budget {", ".join(sorted(allowed))} '
                            f'— not "{sub_budget}".')
        else:
            out.update(sub_budget_code=sub_budget, sub_budget_name=subs[sub_budget])
    return out


def _clean_department_head(company, request_type, purpose, data, problems):
    """`(employee, user)`: the HOD the requester picked and the login that approves; Nones where none is asked.

    The head is an HOD of the employee master, sent by employee code; the
    route's "Department Head Approval" stage goes to their OMS login, matched
    here (`services.heads`). Asked only when the purpose (or an Employee /
    Imprest request) is approved by department; otherwise any value sent is
    dropped, so a request that changes purpose does not carry a stale head.
    """
    from advance_payment.services import heads

    if not needs_department_head(company, request_type, purpose, _text(data.get('budget_code'), 20)):
        return None, None
    code = _text(data.get('department_head_code'), 20)
    if not code:
        problems.append('Choose the Department Head who approves this request.')
        return None, None
    employee, user = heads.head_by_code(code)
    if employee is None:
        problems.append(f'"{code}" is not an active HOD in the employee master.')
    elif user is None:
        problems.append(f'{employee.employee_name} ({employee.employee_code}) has no OMS login to approve '
                        f'with. Ask an administrator to create one, or choose another Department Head.')
    return employee, user


def _clean_repayment(data, problems):
    """Employee Advance: how the money comes back."""
    method = _text(data.get('return_method')).upper()
    out = {'return_method': method}
    if method not in ReturnMethod.values:
        problems.append('Choose how the advance is returned.')
        return out
    out['expected_to_date'] = _date(data.get('expected_to_date'))
    if method == ReturnMethod.EMI:
        out['installments'] = _int(data.get('installments'))
        out['emi_amount'] = _money(data.get('emi_amount'))
        out['expected_from_date'] = _date(data.get('expected_from_date'))
        if not out['installments'] or out['installments'] < 1:
            problems.append('Enter the number of installments.')
        if not out['emi_amount'] or out['emi_amount'] <= 0:
            problems.append('Enter the EMI amount.')
        if out['expected_from_date'] is None:
            problems.append('Enter the EMI Start Date.')
    elif method == ReturnMethod.CUSTOM:
        out['return_method_other'] = _text(data.get('return_method_other'), 120)
        out['expected_from_date'] = _date(data.get('expected_from_date'))
        if not out['return_method_other']:
            problems.append('Say how the advance is returned.')
        if out['expected_from_date'] is None:
            problems.append('Enter the Expected From Date.')
    if out['expected_to_date'] is None:
        problems.append('Enter the date it is fully returned by.')
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _next_request_no(year):
    prefix = f'AP-{year}-'
    last = (AdvanceRequest.objects.filter(request_no__startswith=prefix)
            .order_by('-request_no').values_list('request_no', flat=True).first())
    number = int(last.rsplit('-', 1)[1]) + 1 if last else 1
    return f'{prefix}{number:04d}'


def check_files(files):
    """Refuse files over the cap before anything is written."""
    too_big = [f.name for f in files if f.size > MAX_FILE_BYTES]
    if too_big:
        raise RequestInvalid([f'Over the 10 MB limit: {", ".join(too_big)}.'])


def add_files(advance, files, *, user, purpose=FilePurpose.SUPPORTING, payout_line=None):
    from advance_payment.services import sap_attachments

    rows = []
    for upload in files:
        rows.append(RequestFile.objects.create(
            request=advance, payout_line=payout_line, purpose=purpose, file=upload,
            name=upload.name[:255], size=upload.size, uploaded_by=user))
    if rows:
        # Onto the company's SAP attachments share, once this save commits.
        sap_attachments.share_after_commit(advance)
    return rows


def _write_documents(advance, documents):
    advance.documents.all().delete()
    RequestDocument.objects.bulk_create(
        [RequestDocument(request=advance, **doc) for doc in documents])


def _write_expense_lines(advance, lines):
    advance.expense_lines.all().delete()
    ExpenseLine.objects.bulk_create([ExpenseLine(request=advance, **line) for line in lines])


def _reserve(cleaned, *, company=None, exclude_request=None):
    """Refuse a line that takes more than is still available. Inside the save's transaction.

    `company` is the request's own, for an edit whose fields do not carry it.
    """
    from advance_payment.services import reservations

    found = reservations.problems(cleaned.fields.get('company', company), cleaned.documents,
                                  exclude_request=exclude_request)
    if found:
        raise RequestInvalid(found)


def create(cleaned, *, user, files=()):
    """Insert the request, its documents and files. The caller submits it.

    Numbered AP-<year>-<nnnn>. Two requests raised at the same instant can
    compute the same number; the unique index refuses the second, which is
    retried with the next number.
    """
    check_files(files)
    year = timezone.localdate().year
    for _attempt in range(5):
        try:
            with transaction.atomic():
                _reserve(cleaned)
                advance = AdvanceRequest.objects.create(
                    request_no=_next_request_no(year), created_by=user, **cleaned.fields)
                _write_documents(advance, cleaned.documents)
                _write_expense_lines(advance, cleaned.expense_lines)
                add_files(advance, files, user=user)
                return advance
        except IntegrityError as exc:
            if 'request_no' not in str(exc):
                raise
    raise RequestInvalid(['Could not number the request; try again.'])


#: What an edit is compared on, for the EDITED log row — as the request page
#: reads each value, so the history says "Finance", not department 4.
#: `{key: how to read it}`; the keys are what the page labels.
_COMPARED = {
    'company': lambda a: a.company,
    'request_type': lambda a: a.request_type,
    'payment_against': lambda a: a.payment_against,
    'payment_against_other': lambda a: a.payment_against_other,
    # Legacy rows only: the form now picks a budget head, not an OMS department.
    'department': lambda a: a.department.name if a.department_id else None,
    'sub_department': lambda a: a.sub_department.name if a.sub_department_id else None,
    'partner': lambda a: ' — '.join(v for v in (a.partner_code, a.partner_name) if v) or None,
    'amount': lambda a: a.amount,
    'expected_date': lambda a: a.expected_date,
    'expected_bill_date': lambda a: a.expected_bill_date,
    'return_method': lambda a: a.return_method,
    'return_method_other': lambda a: a.return_method_other,
    'installments': lambda a: a.installments,
    'emi_amount': lambda a: a.emi_amount,
    'expected_from_date': lambda a: a.expected_from_date,
    'expected_to_date': lambda a: a.expected_to_date,
    'payment_date': lambda a: a.payment_date,
    'remarks': lambda a: a.remarks,
    'owner': lambda a: a.owner_label,
    'budget': lambda a: ' — '.join(v for v in (a.budget_code, a.budget_name) if v) or None,
    'sub_budget': lambda a: ' — '.join(v for v in (a.sub_budget_code, a.sub_budget_name) if v) or None,
    'effect_month': lambda a: a.effect_month or None,
    'electricity': lambda a: 'Yes' if a.is_electricity else None,
    'tds': lambda a: a.expense_tds_code or None,
    'purpose': lambda a: a.purpose_label or a.purpose_code or None,
    'department_head': lambda a: (f'{a.department_head_employee.employee_name} '
                                  f'({a.department_head_employee.employee_code})')
    if a.department_head_employee_id else None,
}


def _shown(value):
    """A value as the EDITED row stores it: text, or None when empty."""
    if value is None or value == '':
        return None
    if isinstance(value, Decimal):
        return f'{value:.2f}'  # 1000 and 1000.00 are the same amount
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _snapshot(advance):
    return {name: _shown(read(advance)) for name, read in _COMPARED.items()}


def _documents(advance):
    """`{"A/P invoice 10256": "40000.00", ...}` — each document and its amount.

    A ledger item is one line of a document (a refund can take two lines of
    one journal entry), so its line number is part of the name.
    """
    return {f'{d.get_kind_display()} {d.sap_doc_num or d.sap_doc_entry}'
            + (f' line {d.sap_line}' if d.sap_line else ''): _shown(d.amount)
            for d in advance.documents.all()}


def _expense_lines(advance):
    """`{"Line 1": "5680011 · 10-2026 · 10000.00 + CGST + SGST 18% = 11800.00 · TDS C194 200.00 · rent"}`
    — each line as history shows it."""
    def shown(ln):
        gst = EXPENSE_GST.get(ln.gst_code, (ln.gst_code, 0))[0]
        value = f'{_shown(ln.taxable_amount)} + {gst} = {_shown(ln.amount)}' if ln.gst_code else _shown(ln.amount)
        tds = f'TDS {ln.tds_code} {_shown(ln.tds_amount)}' if ln.tds_amount else None
        return ' · '.join(v for v in (ln.gl_account or 'no G/L', ln.effect_month or None, value, tds,
                                      ln.remarks or None) if v)
    return {f'Line {ln.line_no}': shown(ln) for ln in advance.expense_lines.all()}


def _document_changes(before, after):
    added = [{'doc': k, 'amount': after[k]} for k in after if k not in before]
    removed = [{'doc': k, 'amount': before[k]} for k in before if k not in after]
    changed = [{'doc': k, 'old': before[k], 'new': after[k]}
               for k in after if k in before and before[k] != after[k]]
    out = {'added': added, 'removed': removed, 'changed': changed}
    return {k: v for k, v in out.items() if v}


def update(advance, cleaned, *, user, files=(), remove_file_ids=()):
    """Overwrite the request with the edited form. Returns what changed.

    `{field: {"old": .., "new": ..}}` for the fields, plus `documents`
    (added / removed / changed amounts) and `files_added` / `files_removed` —
    the EDITED log row, which the request page shows as Was → Now.
    """
    check_files(files)
    _reserve(cleaned, company=advance.company, exclude_request=advance.pk)
    before, docs_before, lines_before = _snapshot(advance), _documents(advance), _expense_lines(advance)
    for name, value in cleaned.fields.items():
        setattr(advance, name, value)
    advance.save()
    _write_documents(advance, cleaned.documents)
    _write_expense_lines(advance, cleaned.expense_lines)
    from advance_payment.services import sap_attachments

    removed = list(advance.files.filter(
        pk__in=list(remove_file_ids), purpose=FilePurpose.SUPPORTING).values_list('name', flat=True))
    unshared = []
    for row in advance.files.filter(pk__in=list(remove_file_ids), purpose=FilePurpose.SUPPORTING):
        unshared.append(row.share_file_id)
        row.file.delete(save=False)
        row.delete()
    sap_attachments.unshare_after_commit(unshared)
    added = add_files(advance, files, user=user)
    if before.get('company') != _snapshot(advance).get('company'):
        # A new company: its files belong on that company's share.
        sap_attachments.share_after_commit(advance)
    advance.refresh_from_db()  # the department / sub-department names, as saved
    after = _snapshot(advance)
    changes = {k: {'old': before[k], 'new': after[k]} for k in after if before[k] != after[k]}
    documents = _document_changes(docs_before, _documents(advance))
    if documents:
        changes['documents'] = documents
    lines_after = _expense_lines(advance)
    if lines_after != lines_before:
        changes['expense_lines'] = {k: {'old': lines_before.get(k), 'new': lines_after.get(k)}
                                    for k in sorted(set(lines_before) | set(lines_after))
                                    if lines_before.get(k) != lines_after.get(k)}
    if removed:
        changes['files_removed'] = removed
    if added:
        changes['files_added'] = [f.name for f in added]
    return changes
