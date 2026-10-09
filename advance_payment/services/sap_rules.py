"""SAP's own checks on an outgoing payment, in one place.

SAP runs its transaction notification on every outgoing payment (OVPM) and
refuses one that misses a field its users fill in by hand. Each check here is
one SAP refused an OMS payment with, what SAP wants, and how OMS meets it:

  460007  Payment Mode   A bank payment says how it leaves: U_Pymnt_Mode, one
                         of NEFT / RTGS / FT. The Payment desk's choice, else
                         worked out from the methods (`payment_mode`).
  4612    Urgency        "After 5:00 PM Urgency field is mandatory": U_URGENCY,
                         whose only value is 'H'. On India's clock — this
                         server's is UTC. SAP carries it on every payment made
                         after 5 PM, of every kind.
  460008  Type of        "Pls Select the type of Advance Payment":
          Advance        U_Type_of_Advance on a payment to a business partner
                         (vendor, imprest holder, customer). OMS pays a request
                         once: One Time Settlement. 'Adjustable in EMI' is a
                         payroll recovery OMS does not arrange. Not on an
                         expense paid straight to G/L accounts: SAP's own
                         expense payments mostly leave it blank.
  460009  Settlement     A one-time advance — a payment to a partner ON ACCOUNT,
          Date           settled later by a bill — must say by when:
                         U_Adv_Settl_Dt. Past it, unsettled, SAP blocks the
                         partner's entries. The request's own expected bill
                         date, else SETTLE_WITHIN_DAYS after posting. SAP's own
                         such payments carry one every time (~15 days, up to 120).

`apply` fills the fields; `problems` checks a body before it is sent, so a gap
is reported by OMS in plain words rather than discovered as a refusal; and
`explain` turns a refusal SAP still returns into what it means and what to do.
A NEW check SAP adds is not in the list: `explain` says so, so it is noticed and
added here rather than retried blind.
"""
import re
from datetime import date, time, timedelta

from django.utils import timezone

from advance_payment.models import PayoutMethod
from advance_payment.services import clock

# ── 460007: Payment Mode ─────────────────────────────────────────────────────

#: SAP's Payment Mode by payment method. Its valid values are NEFT, RTGS and FT
#: (Oil, Beverages; Mart takes text). Cash needs none.
PAYMENT_MODE = {
    PayoutMethod.NEFT: 'NEFT',
    PayoutMethod.RTGS: 'RTGS',
    PayoutMethod.IMPS: 'FT',
    PayoutMethod.UPI: 'FT',
    PayoutMethod.CHEQUE: 'FT',
}


def payment_mode(bank_lines):
    """The Payment Mode of a payment's bank lines: SAP has one field, so the
    method carrying the most money (the first such line on a tie)."""
    biggest = max(bank_lines, key=lambda line: line.amount)
    return PAYMENT_MODE.get(biggest.method, 'FT')


# ── 4612, 460008, 460009 ─────────────────────────────────────────────────────

URGENT_FROM = time(17, 0)
URGENCY = 'H'
TYPE_OF_ADVANCE = 'One Time Settlement'
SETTLE_WITHIN_DAYS = 30

#: Payments to a business partner. An expense is `rAccount`: G/L accounts only.
PARTNER_DOC_TYPES = ('rSupplier', 'rCustomer')


def is_urgent(now=None):
    """Whether SAP's 5 PM urgency rule applies at `now` — on India's clock."""
    return (now or timezone.now()).astimezone(clock.INDIA).time() >= URGENT_FROM


def to_partner(payload):
    return payload.get('DocType') in PARTNER_DOC_TYPES


def on_account(payload):
    """Paid to a partner with no document to apply it to: an advance, settled later."""
    return to_partner(payload) and not payload.get('PaymentInvoices')


def settlement_date(advance, posting_date):
    """When an on-account advance is to be settled by its bill (U_Adv_Settl_Dt).

    The request's own expected bill date (a PO advance's, an imprest's) when it
    is not already past — a past date would block the partner at once — else
    SETTLE_WITHIN_DAYS after posting.
    """
    expected = getattr(advance, 'expected_date', None) or getattr(advance, 'expected_bill_date', None)
    if expected and expected >= posting_date:
        return expected
    return posting_date + timedelta(days=SETTLE_WITHIN_DAYS)


def apply(payload, advance, *, posting_date, now=None):
    """Fill what SAP's checks demand of `payload`, for a payment made at `now`.

    Idempotent, so it is run again just before the body is sent: a post that
    crosses 5 PM while its TDS journal is booked still carries the urgency.
    """
    if is_urgent(now):
        payload['U_URGENCY'] = URGENCY
    if to_partner(payload):
        payload['U_Type_of_Advance'] = TYPE_OF_ADVANCE
        if on_account(payload):
            payload['U_Adv_Settl_Dt'] = settlement_date(advance, posting_date).isoformat()
    return payload


def problems(payload, *, now=None):
    """What SAP would refuse in `payload`, found before asking it. Empty when none."""
    out = []
    if payload.get('TransferSum') and not payload.get('U_Pymnt_Mode'):
        out.append('The bank payment has no SAP Payment Mode (SAP check 460007).')
    if is_urgent(now) and payload.get('U_URGENCY') != URGENCY:
        out.append('After 5 PM the payment must be marked urgent (SAP check 4612).')
    if to_partner(payload) and not payload.get('U_Type_of_Advance'):
        out.append('The payment has no Type of Advance (SAP check 460008).')
    if on_account(payload):
        settle = payload.get('U_Adv_Settl_Dt')
        if not settle:
            out.append('The advance has no settlement date (SAP check 460009).')
        elif payload.get('DocDate') and date.fromisoformat(settle) < date.fromisoformat(payload['DocDate']):
            out.append(f'The settlement date {settle} is before the payment date (SAP check 460009).')
    return out


# ── Refusals ─────────────────────────────────────────────────────────────────

#: What each known check means to the person who pressed Approve.
KNOWN = {
    '460007': 'SAP needs the Payment Mode (NEFT, RTGS or FT) of a bank payment.',
    '4612': 'SAP needs the payment marked urgent after 5 PM.',
    '460008': 'SAP needs the Type of Advance.',
    '460009': 'SAP needs the date the advance will be settled by its bill.',
}

_CODE = re.compile(r'\(\s*-?(\d{3,7})\s*\)')


def code_of(error):
    """SAP's check number in a refusal, as a string; '' when it has none."""
    raw = str(getattr(error, 'sap_code', '') or '').lstrip('-')
    if raw.isdigit():
        return raw
    found = _CODE.search(str(error))
    return found.group(1) if found else ''


def explain(error):
    """A refusal as the desk should read it: SAP's words, then what to do."""
    code = code_of(error)
    if code in KNOWN:
        advice = (f'{KNOWN[code]} OMS fills this in itself, so SAP\'s rule has changed: '
                  f'send this message to IT.')
    elif code:
        advice = (f'Check {code} is a rule in SAP that OMS does not know yet: '
                  f'send this message to IT to add it.')
    else:
        advice = ''
    return f'SAP refused the payment: {error}' + (f' — {advice}' if advice else '')
