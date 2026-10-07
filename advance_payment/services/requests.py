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
}

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


def clean(data):
    """Validate the form's JSON. Returns `CleanRequest`; raises `RequestInvalid`."""
    problems = []
    data = data or {}

    company = _text(data.get('company')).upper()
    if company not in COMPANY_CODES:
        problems.append('Company must be one of: ' + ', '.join(COMPANY_CODES) + '.')

    request_type = _text(data.get('request_type')).upper()
    against = _text(data.get('payment_against')).upper()
    case = CASES.get((request_type, against))
    if case is None:
        problems.append('That Type and Payment Against pair is not offered.')
        case = {'documents': None}

    partner_code = _text(data.get('partner_code'), 50)
    partner_name = _text(data.get('partner_name'), 200)
    if not partner_code or not partner_name:
        problems.append('Choose who is being paid.')
    not_in_sap = partner_code.startswith(NOT_IN_SAP_PREFIX)
    if not_in_sap and request_type != RequestType.EMPLOYEE_ADVANCE:
        problems.append('Only an Employee advance may be raised for someone not yet in SAP.')

    documents = []
    if case['documents']:
        documents, amount = _clean_documents(data.get('documents'), case['documents'], problems)
    else:
        if data.get('documents'):
            problems.append('This Payment Against takes a typed amount, not documents.')
        amount = _money(data.get('amount'))
        if amount is None or amount <= 0:
            problems.append('Enter an amount above zero.')

    payment_date = _date(data.get('payment_date'))
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

    if not _text(data.get('remarks')):
        problems.append('Enter the Remarks.')

    routing = _clean_routing(company, data, problems)
    routing['department_head_employee'], routing['department_head'] = _clean_department_head(
        company, request_type, routing.get('purpose_code', ''), data, problems)

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
    elif not owner_label:
        problems.append('Choose the Ownership HOD or Sub-HOD.')

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
    )


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


def _clean_routing(company, data, problems):
    """Department (SAP's budget head) and Payment Purpose: both required.

    The budget head must be one of the company's active dimension-3 cost
    centres in SAP; the purpose one of `advance_payment.purposes`. Sub Budget
    is no longer asked, so it is cleared.
    """
    from advance_payment.services import sap as sap_service

    out = {}
    purpose = _text(data.get('purpose_code'), 30).upper()
    if not purpose:
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

    if not needs_department_head(company, request_type, purpose):
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
    rows = []
    for upload in files:
        rows.append(RequestFile.objects.create(
            request=advance, payout_line=payout_line, purpose=purpose, file=upload,
            name=upload.name[:255], size=upload.size, uploaded_by=user))
    return rows


def _write_documents(advance, documents):
    advance.documents.all().delete()
    RequestDocument.objects.bulk_create(
        [RequestDocument(request=advance, **doc) for doc in documents])


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
    before, docs_before = _snapshot(advance), _documents(advance)
    for name, value in cleaned.fields.items():
        setattr(advance, name, value)
    advance.save()
    _write_documents(advance, cleaned.documents)
    removed = list(advance.files.filter(
        pk__in=list(remove_file_ids), purpose=FilePurpose.SUPPORTING).values_list('name', flat=True))
    for row in advance.files.filter(pk__in=list(remove_file_ids), purpose=FilePurpose.SUPPORTING):
        row.file.delete(save=False)
        row.delete()
    added = add_files(advance, files, user=user)
    advance.refresh_from_db()  # the department / sub-department names, as saved
    after = _snapshot(advance)
    changes = {k: {'old': before[k], 'new': after[k]} for k in after if before[k] != after[k]}
    documents = _document_changes(docs_before, _documents(advance))
    if documents:
        changes['documents'] = documents
    if removed:
        changes['files_removed'] = removed
    if added:
        changes['files_added'] = [f.name for f in added]
    return changes
