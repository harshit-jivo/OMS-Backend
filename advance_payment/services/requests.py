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
  * department and sub-department exist, and a department that has
    sub-departments names one of its own;
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
    Department,
    DocumentKind,
    Employee,
    FilePurpose,
    PaymentAgainst,
    Priority,
    RequestDocument,
    RequestFile,
    RequestType,
    ReturnMethod,
    SubDepartment,
)
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
    (RequestType.VENDOR, PaymentAgainst.AGAINST_PO): {'documents': DocumentKind.PO, 'expected_date': True},
    (RequestType.VENDOR, PaymentAgainst.AGAINST_BILL): {'documents': DocumentKind.BILL},
    (RequestType.EMPLOYEE_ADVANCE, PaymentAgainst.ADVANCE): {'documents': None, 'repayment': True},
    (RequestType.EMPLOYEE_ADVANCE, PaymentAgainst.OTHER): {'documents': None},
    (RequestType.EMPLOYEE_IMPREST, PaymentAgainst.ADVANCE): {'documents': None, 'expected_bill_date': True},
    (RequestType.EMPLOYEE_IMPREST, PaymentAgainst.AGAINST_BILL): {
        'documents': DocumentKind.BILL, 'expected_bill_date': True},
    (RequestType.EMPLOYEE_IMPREST, PaymentAgainst.OTHER): {'documents': None, 'expected_bill_date': True},
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

    department = Department.objects.filter(pk=_int(data.get('department_id')), is_active=True).first()
    sub_department = None
    if department is None:
        problems.append('Choose a Department.')
    else:
        subs = SubDepartment.objects.filter(department=department, is_active=True)
        sub_id = _int(data.get('sub_department_id'))
        if sub_id:
            sub_department = subs.filter(pk=sub_id).first()
            if sub_department is None:
                problems.append(f'That Sub-department is not part of {department.name}.')
        elif subs.exists():
            problems.append(f'Choose a Sub-department of {department.name}.')

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

    expected_date = _date(data.get('expected_date')) if case.get('expected_date') else None
    if case.get('expected_date') and expected_date is None:
        problems.append('Enter the Expected Bill Date.')
    expected_bill_date = _date(data.get('expected_bill_date')) if case.get('expected_bill_date') else None
    if case.get('expected_bill_date') and expected_bill_date is None:
        problems.append('Enter the Expected Bill Date.')

    repayment = _clean_repayment(data, problems) if case.get('repayment') else {}

    if not _text(data.get('remarks')):
        problems.append('Enter the Remarks.')

    purpose = _clean_purpose(company, data, problems) if company in COMPANY_CODES else {}

    priority = _text(data.get('priority')).upper() or Priority.MEDIUM
    if priority not in Priority.values:
        problems.append('Priority must be Low, Medium or High.')

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
            'department': department,
            'sub_department': sub_department,
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
            'priority': priority,
            **purpose,
            'remarks': _text(data.get('remarks')),
            'owner_employee': owner,
            'owner_label': owner_label,
        },
        documents=documents,
    )


def _clean_documents(raw, kind, problems):
    """The document lines, and their total. Appends to `problems`."""
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
        if entry in seen:
            problems.append(f'Document {label} is chosen twice.')
            continue
        seen.add(entry)
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
        total += amount
        lines.append({
            'kind': kind,
            'sap_doc_entry': entry,
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
    return lines, total


def _clean_purpose(company, data, problems):
    """Budget and Sub Budget: both required, both one of the company's in SAP."""
    from advance_payment.services import sap as sap_service

    budget = _text(data.get('budget_code'), 20)
    sub_budget = _text(data.get('sub_budget_code'), 20)
    if not budget:
        problems.append('Choose the Payment Purpose (Budget).')
    if not sub_budget:
        problems.append('Choose the Payment Purpose (Sub Budget).')
    if not (budget and sub_budget):
        return {}
    try:
        known = sap_service.budgets(company)
    except sap_service.SapUnavailable:
        problems.append('Could not check the Payment Purpose with SAP. Try again shortly.')
        return {}
    names = {(r['kind'], r['code']): r['name'] for r in known}
    if ('BUDGET', budget) not in names:
        problems.append(f'Budget "{budget}" is not an active budget in {company}.')
    if ('SUB_BUDGET', sub_budget) not in names:
        problems.append(f'Sub Budget "{sub_budget}" is not an active sub budget in {company}.')
    return {
        'budget_code': budget,
        'budget_name': names.get(('BUDGET', budget), ''),
        'sub_budget_code': sub_budget,
        'sub_budget_name': names.get(('SUB_BUDGET', sub_budget), ''),
    }


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
                advance = AdvanceRequest.objects.create(
                    request_no=_next_request_no(year), created_by=user, **cleaned.fields)
                _write_documents(advance, cleaned.documents)
                add_files(advance, files, user=user)
                return advance
        except IntegrityError as exc:
            if 'request_no' not in str(exc):
                raise
    raise RequestInvalid(['Could not number the request; try again.'])


#: What an edit is compared on, for the EDITED log row.
_COMPARED = (
    'company', 'request_type', 'payment_against', 'payment_against_other', 'department_id',
    'sub_department_id', 'partner_code', 'partner_name', 'amount', 'expected_date',
    'expected_bill_date', 'return_method', 'return_method_other', 'installments', 'emi_amount',
    'expected_from_date', 'expected_to_date', 'payment_date', 'priority', 'remarks',
    'owner_employee_id', 'owner_label', 'budget_code', 'sub_budget_code',
)


def _snapshot(advance):
    out = {}
    for name in _COMPARED:
        value = getattr(advance, name)
        out[name] = None if value is None else str(value)
    out['documents'] = sorted(
        f'{d.kind}:{d.sap_doc_entry}:{d.amount}' for d in advance.documents.all())
    return out


def update(advance, cleaned, *, user, files=(), remove_file_ids=()):
    """Overwrite the request with the edited form. Returns what changed.

    `{field: {"old": .., "new": ..}}`, for the EDITED log row.
    """
    check_files(files)
    before = _snapshot(advance)
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
    after = _snapshot(advance)
    changes = {k: {'old': before[k], 'new': after[k]} for k in after if before[k] != after[k]}
    if removed:
        changes['files_removed'] = removed
    if added:
        changes['files_added'] = [f.name for f in added]
    return changes
