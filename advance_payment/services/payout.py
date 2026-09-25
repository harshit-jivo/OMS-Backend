"""The payment details: filled in at the Payment stage, checked before approval.

Mirrors `payout.ts` on the approval desk, which is the specification: the
same method limits (UPI below 1 lakh, RTGS above 2 lakh, IMPS below 5 lakh,
cash up to 10,000), the same cash note rule, the same payee checks. Saving
is allowed with gaps (no accounts or cheque details yet), though every line
needs its amount; APPROVING is not, and `problems()` is what it checks.

ONE SAP PAYMENT, SO ONE BANK AND ONE DRAWER
-------------------------------------------
SAP's outgoing payment has a single TransferAccount and a single CashAccount.
So every non-cash line must leave from the same bank G/L, and every cash line
from the same cash G/L. Two banks would be two SAP payments, which is two
requests.
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone

from advance_payment.models import Payout, PayoutLine, PayoutMethod

CENT = Decimal('0.01')

#: As `METHOD_LIMITS` in payout.ts, word for word.
LIMITS = {
    PayoutMethod.UPI: ('below', Decimal('100000')),
    PayoutMethod.RTGS: ('above', Decimal('200000')),
    PayoutMethod.IMPS: ('below', Decimal('500000')),
    PayoutMethod.CASH: ('up_to', Decimal('10000')),
}
TRANSFERS = {PayoutMethod.UPI, PayoutMethod.NEFT, PayoutMethod.RTGS, PayoutMethod.IMPS}
NOTE_DENOMINATIONS = {10, 20, 50, 100, 200, 500}

_IFSC = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
_ACCOUNT = re.compile(r'^\d{9,18}$')
_UTR = re.compile(r'^[A-Z0-9]{8,30}$')


class PayoutInvalid(Exception):
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


def _text(value, limit):
    return (value.strip() if isinstance(value, str) else '')[:limit]


def _notes(raw):
    """`[{"denomination": 500, "quantity": 18}]`, dropping blank rows."""
    out = []
    for row in raw or []:
        try:
            denomination = int(row.get('denomination'))
            quantity = int(row.get('quantity'))
        except (TypeError, ValueError, AttributeError):
            continue
        if denomination in NOTE_DENOMINATIONS and quantity > 0:
            out.append({'denomination': denomination, 'quantity': quantity})
    return out


def save(advance, data, *, user):
    """Write the payout as sent. Lines are matched by `id` so their files stay.

    Returns `(payout, changed)`: `changed` when anything differs from what
    was stored.
    """
    data = data or {}
    lines_in = data.get('lines') or []
    problems = []
    parsed = []
    for index, raw in enumerate(lines_in, start=1):
        raw = raw or {}
        method = _text(raw.get('method'), 10).upper()
        if method not in PayoutMethod.values:
            problems.append(f'Method {index}: choose UPI, NEFT, RTGS, IMPS, Cheque or Cash.')
            continue
        amount = _money(raw.get('amount'))
        if amount is None or amount <= 0:
            problems.append(f'Method {index}: enter an amount above zero.')
            continue
        parsed.append({
            'id': raw.get('id'),
            'method': method,
            'amount': amount,
            'from_account': _text(raw.get('from_account'), 30),
            'from_account_label': _text(raw.get('from_account_label'), 200),
            'cheque_number': _text(raw.get('cheque_number'), 20) if method == PayoutMethod.CHEQUE else '',
            'cheque_bank': _text(raw.get('cheque_bank'), 100) if method == PayoutMethod.CHEQUE else '',
            'cheque_date': (raw.get('cheque_date') or None) if method == PayoutMethod.CHEQUE else None,
            'cash_notes': _notes(raw.get('cash_notes')) if method == PayoutMethod.CASH else None,
        })
    if problems:
        raise PayoutInvalid(problems)

    before = fingerprint(advance)
    payout, _ = Payout.objects.update_or_create(
        request=advance,
        defaults={
            'beneficiary_name': _text(data.get('beneficiary_name'), 200),
            'to_account_number': _text(data.get('to_account_number'), 40),
            'to_ifsc': _text(data.get('to_ifsc'), 11).upper(),
            'to_account_manual': bool(data.get('to_account_manual')),
            'updated_by': user,
        })

    existing = {line.pk: line for line in payout.lines.all()}
    keep = set()
    for item in parsed:
        line_id = item.pop('id')
        try:
            line = existing.get(int(line_id))
        except (TypeError, ValueError):
            line = None
        if line is None:
            line = PayoutLine(payout=payout)
        for name, value in item.items():
            setattr(line, name, value)
        line.save()
        keep.add(line.pk)
    for pk, line in existing.items():
        if pk not in keep:
            for f in line.files.all():
                f.file.delete(save=False)
                f.delete()
            line.delete()
    return payout, fingerprint(advance) != before


def fingerprint(advance):
    """Everything the payment details hold, as one comparable value."""
    payout = Payout.objects.filter(request=advance).first()
    if payout is None:
        return None
    return (
        payout.beneficiary_name, payout.to_account_number, payout.to_ifsc,
        tuple(sorted(
            (l.method, str(l.amount), l.from_account, l.cheque_number, l.cheque_bank,
             str(l.cheque_date or ''), str(l.cash_notes or ''))
            for l in payout.lines.all())),
    )


def problems(advance):
    """Everything that stops the Payment stage approving. Empty when ready."""
    payout = Payout.objects.filter(request=advance).first()
    if payout is None:
        return ['Fill in the payment and bank details.']
    out = []
    lines = list(payout.lines.all())
    if not payout.beneficiary_name.strip():
        out.append('Enter the Beneficiary Name.')
    if any(l.method != PayoutMethod.CASH for l in lines):
        if not _ACCOUNT.match(payout.to_account_number or ''):
            out.append('To Account Number must be 9 to 18 digits.')
        if not _IFSC.match((payout.to_ifsc or '').upper()):
            out.append('IFSC must look like HDFC0001234.')
    if not lines:
        out.append('Add at least one payment method.')
    for n, line in enumerate(lines, start=1):
        if not line.from_account:
            out.append(f'Method {n}: choose the account it is paid from.')
        rule = LIMITS.get(line.method)
        if rule:
            kind, limit = rule
            if (kind == 'below' and not line.amount < limit) or \
                    (kind == 'above' and not line.amount > limit) or \
                    (kind == 'up_to' and line.amount > limit):
                words = {'below': 'below', 'above': 'above', 'up_to': 'up to'}[kind]
                out.append(f'Method {n}: {line.get_method_display()} is only for amounts {words} {limit:,.0f}.')
        if line.method == PayoutMethod.CHEQUE and not (line.cheque_number and line.cheque_date):
            out.append(f'Method {n}: enter the cheque number and date.')
        if line.method == PayoutMethod.CASH:
            notes = sum(Decimal(r['denomination'] * r['quantity']) for r in (line.cash_notes or []))
            if notes != line.amount:
                out.append(f'Method {n}: the cash notes add up to {notes}, not {line.amount}.')
    total = sum((l.amount for l in lines), Decimal('0'))
    if lines and total != advance.amount:
        out.append(f'The payment methods add up to {total}, but the request is for {advance.amount}.')
    banks = {l.from_account for l in lines if l.method != PayoutMethod.CASH and l.from_account}
    drawers = {l.from_account for l in lines if l.method == PayoutMethod.CASH and l.from_account}
    if len(banks) > 1:
        out.append('Every bank payment must leave from the same account: SAP posts one bank per payment.')
    if len(drawers) > 1:
        out.append('Every cash payment must come from the same cash account.')
    return out


def record_utr(line, *, utr, proof, user):
    """Record the bank's reference for one transfer line, after paying."""
    value = (utr or '').strip().upper().replace(' ', '')
    if line.method not in TRANSFERS:
        raise PayoutInvalid(['A UTR is recorded for UPI, NEFT, RTGS and IMPS transfers only.'])
    if not _UTR.match(value):
        raise PayoutInvalid(['A UTR is 8 to 30 letters and digits.'])
    old = line.utr
    line.utr = value
    line.utr_proof = proof if isinstance(proof, dict) else None
    line.utr_recorded_by = user
    line.utr_recorded_on = timezone.now()
    line.save(update_fields=['utr', 'utr_proof', 'utr_recorded_by', 'utr_recorded_on'])
    return old
