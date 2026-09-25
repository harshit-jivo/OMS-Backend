"""Find a payment's UTR in a proof, and check it against what was paid.

Works for ANY bank, so there are no per-bank templates. It relies only on
what every Indian bank shares:

  * the payment rails' reference formats
      RTGS  22 characters: bank code (4 letters) + "R" + 17  e.g. HDFCR52026092312345678
      NEFT  16 or 22: bank code + 12 / "N" + 17, or "N" + 15 e.g. SBIN126267123456
      IMPS / UPI  a 12-digit RRN                               e.g. 626612345678
  * statement narrations name the rail next to its reference:
      "NEFT/HDFCN52026092312345678/GODAMWALE", "IMPS-626612345678-P2A", "UPI/626612345678/..."
  * payment advices and screenshots LABEL it: "UTR No.", "RRN", "Transaction Ref"

A proof is one payment's advice (a handful of rows, usually one reference) or
a whole statement (many rows, many references). Each reference found is
scored against what THIS payment should look like: its amount, the payee's
account, and the invoices it pays. On a statement that evidence must sit on
or next to the reference's own row; on a single advice it may be anywhere on
the page, because an advice prints the amount, the account and the reference
on separate lines.

The best-scoring reference is the answer, with each check reported, so the
screen can say "UTR found, amount matches, account matches" or say exactly
which one does not, rather than trusting a number that happens to look right.
"""

import re
from datetime import datetime

CHANNELS = ('RTGS', 'NEFT', 'IMPS', 'UPI')

#: "UTR No. : X", "RRN: X", "Transaction Ref No X", "Ref No.X", "Reference Number X".
_LABELLED = re.compile(
    r'\b(?:UTR|RRN|U\.T\.R\.?|UNIQUE\s+TRANSACTION\s+REF\w*|TRANSACTION\s+REF\w*|TXN\s*REF\w*'
    r'|TRANSACTION\s+ID|TXN\s*ID|REF(?:ERENCE)?)'
    r'\s*(?:NO\.?|NUMBER|NUM|#)?\s*[:#.\-]?\s*([A-Z0-9]{10,22})\b')
#: A label with its value on the NEXT line, as phone screenshots lay it out:
#: "UPI transaction ID" / "626712345678".
_LABEL_ONLY = re.compile(
    r'(?:UTR|RRN|UNIQUE\s+TRANSACTION\s+REF\w*|TRANSACTION\s+REF\w*|TXN\s*REF\w*'
    r'|TRANSACTION\s+ID|TXN\s*ID|REF(?:ERENCE)?)\s*(?:NO\.?|NUMBER|NUM|#)?\s*[:#.\-]?\s*$')
_TOKEN = re.compile(r'[A-Z0-9]+')
_RTGS = re.compile(r'^[A-Z]{4}R[A-Z0-9]{17}$')
_NEFT_BANK = re.compile(r'^[A-Z]{4}[A-Z0-9]{12}$|^[A-Z]{4}N[A-Z0-9]{17}$')
_NEFT_N = re.compile(r'^N\d{15}$')
_RRN = re.compile(r'^\d{12}$')
_IFSC = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')

#: 1,23,456.00 / 123,456.00 / 288746.00 / 2,88,746 : money, never a bare
#: integer (that would be every reference and account number).
_AMOUNT = re.compile(r'(?<![\d.,])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{1,2})(?![\d,])')
_ACCOUNT_FULL = re.compile(r'(?<!\d)\d{9,18}(?!\d)')
_ACCOUNT_MASKED = re.compile(r'[X*]{2,}\s?(\d{3,6})\b')
_DATE = re.compile(
    r'\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}'
    r'|\d{1,2}[\s\-](?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*[\s\-,]*\d{2,4}'
    r'|\d{4}-\d{2}-\d{2})\b')

#: A proof with at most this many references is one payment's advice: its
#: evidence may be anywhere on the page.
SINGLE_PROOF_REFERENCES = 2
AMOUNT_TOLERANCE = 1.0


def _digits(value):
    return re.sub(r'\D', '', str(value or ''))


def _alnum(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def _channel_in(text):
    return next((c for c in CHANNELS if re.search(rf'\b{c}\b', text)), None)


def _classify(token, channel, labelled):
    """The rail a token is a reference of, or None if it is not one."""
    digits = sum(ch.isdigit() for ch in token)
    if _IFSC.match(token) or digits < 8:
        return None
    if _RTGS.match(token):
        return 'RTGS'
    if _NEFT_N.match(token):
        return 'NEFT'
    if _NEFT_BANK.match(token) and token[:4].isalpha():
        # 4 letters + 12 is also the shape of plenty of other codes: only a
        # NEFT row, or a label, makes it a NEFT reference.
        if channel == 'NEFT' or labelled:
            return 'NEFT'
        return None
    if _RRN.match(token):
        # 12 digits is also an account number (ICICI's are 12 digits): only an
        # IMPS / UPI row, or a label, makes it an RRN.
        if channel in ('IMPS', 'UPI') or labelled:
            return channel if channel in ('IMPS', 'UPI') else 'IMPS/UPI'
        return None
    if labelled and 10 <= len(token) <= 22:
        return channel or 'REF'
    return None


def find_references(rows, exclude_digits=()):
    """Every reference in the rows: `[{utr, channel, row, labelled}]`, in order.

    `exclude_digits` are account numbers known not to be references (ours and
    the payee's), since a 12-digit account looks exactly like an RRN.
    """
    excluded = {d for d in (_digits(x) for x in exclude_digits) if d}
    found, seen = [], set()
    carried = None  # (channel) of a label whose value is on this row
    for index, raw in enumerate(rows):
        row = str(raw).upper()
        channel = _channel_in(row) or (carried or None)
        labelled = {m.group(1) for m in _LABELLED.finditer(row)}
        tokens = [m.group(0) for m in _TOKEN.finditer(row)]
        if carried is not None and tokens:
            labelled.add(tokens[0])
        carried = (_channel_in(row) or '') if _LABEL_ONLY.search(row) else None
        for token in tokens:
            if token in seen or (token.isdigit() and token in excluded):
                continue
            rail = _classify(token, channel, token in labelled)
            if rail:
                seen.add(token)
                found.append({'utr': token, 'channel': rail, 'row': index,
                              'labelled': token in labelled})
    return found


def amounts_in(text):
    out = []
    for match in _AMOUNT.finditer(text):
        try:
            out.append(float(match.group(1).replace(',', '')))
        except ValueError:
            continue
    return out


def _account_match(text, expected_accounts):
    """The expected account seen in `text`, in full or masked to its last digits."""
    upper = str(text).upper()
    full = set(_ACCOUNT_FULL.findall(upper))
    for account in expected_accounts:
        if account in full:
            return account
    for suffix in _ACCOUNT_MASKED.findall(upper):
        for account in expected_accounts:
            if len(suffix) >= 4 and account.endswith(suffix):
                return account
    return None


def _invoice_match(text, invoices):
    squashed = _alnum(text)
    return next((inv for inv in invoices if len(_alnum(inv)) >= 4 and _alnum(inv) in squashed), None)


def _date_in(text):
    match = _DATE.search(str(text).upper())
    return match.group(1) if match else ''


def extract(rows, *, amount=None, accounts=(), other_accounts=(), own_accounts=(), invoices=()):
    """The best reference for THIS payment, with every check spelled out.

    `amount`         what this payment line paid
    `accounts`       the payee account it was paid TO (a match is a pass)
    `other_accounts` the payee's OTHER SAP accounts: a match is "a different
                     account of the same payee", which fails the check
    `own_accounts`   our house-bank account numbers: only ever kept out of the
                     references (a 12-digit account reads like an RRN)
    `invoices`       the invoice / bill numbers the request pays

    Returns `{utr, channel, amount, date, row_text, checks, score, candidates,
    kind}`; `utr` None when nothing reference-like was found. A check is True
    (matches), False (the proof shows something else) or None (nothing to
    check, or the proof does not show it).
    """
    rows = [str(r) for r in rows if str(r).strip()]
    expected = [d for d in (_digits(a) for a in accounts) if d]
    others = [d for d in (_digits(a) for a in other_accounts) if d and d not in expected]
    invoices = [i for i in invoices if len(_alnum(i)) >= 4]
    target = float(amount) if amount not in (None, '') else None

    references = find_references(rows, exclude_digits=expected + others + list(own_accounts))
    single = len(references) <= SINGLE_PROOF_REFERENCES
    whole = '\n'.join(rows)
    kind = 'advice' if single else 'statement'

    scored = []
    for ref in references:
        i = ref['row']
        own = rows[i]
        # A statement's narration wraps onto the next row; its amount may be on
        # the row before. An advice's evidence is anywhere on it.
        window = whole if single else '\n'.join(rows[max(i - 1, 0): i + 2])

        own_amounts, window_amounts = amounts_in(own), amounts_in(window)
        amount_ok = None
        if target is not None:
            if any(abs(a - target) <= AMOUNT_TOLERANCE for a in own_amounts):
                amount_ok = 'row'
            elif any(abs(a - target) <= AMOUNT_TOLERANCE for a in window_amounts):
                amount_ok = 'near'
            elif window_amounts:
                amount_ok = False

        account = _account_match(window, expected) if expected else None
        other = None if account else (_account_match(window, others) if others else None)
        invoice = _invoice_match(window, invoices) if invoices else None

        score = (4 if amount_ok == 'row' else 3 if amount_ok == 'near' else 0)
        score += 3 if account else 0
        score += 2 if invoice else 0
        score += 1 if ref['labelled'] else 0
        score += 1 if ref['channel'] in CHANNELS else 0

        # The amount paid: the one matching this payment if shown, else the
        # row's own (a statement), else the page's first (an advice).
        shown = own_amounts or (window_amounts if single else [])
        paid = next((a for a in shown if target is not None and abs(a - target) <= AMOUNT_TOLERANCE),
                    shown[0] if shown else None)
        scored.append({
            'utr': ref['utr'],
            'channel': ref['channel'],
            'amount': paid,
            'date': _date_in(own) or _date_in(window),
            'row_text': own.strip(),
            'score': score,
            'checks': {
                'amount': None if amount_ok is None else bool(amount_ok),
                # False only on positive evidence of ANOTHER payee account: any
                # other long number nearby may just be an RRN.
                'account': True if account else (False if other else None),
                'account_other': other,
                'invoice': (True if invoice else None) if invoices else None,
            },
        })

    scored.sort(key=lambda s: (-s['score'], s['utr']))
    best = scored[0] if scored else None
    return {
        'kind': kind,
        'utr': best['utr'] if best else None,
        'channel': best['channel'] if best else None,
        'amount': best['amount'] if best else None,
        'date': best['date'] if best else '',
        'row_text': best['row_text'] if best else '',
        'score': best['score'] if best else 0,
        'checks': best['checks'] if best else {
            'amount': None, 'account': None, 'account_other': None, 'invoice': None},
        # The runners-up, for the screen to offer when the best is not it.
        'candidates': [{k: s[k] for k in ('utr', 'channel', 'amount', 'date', 'row_text', 'score')}
                       for s in scored[1:6]],
        'references_found': len(scored),
        'read_at': datetime.now().isoformat(timespec='seconds'),
    }
