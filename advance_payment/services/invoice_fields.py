"""Read an invoice's key fields out of its text, and check them against SAP.

The text is a bill or PO attachment as `proof_reader.read` returns it: rows,
one per visual line, a PDF's own text or OCR. Vendors' layouts vary without
limit, so there are no per-vendor templates. Each field is found by its LABEL
("Invoice No", "Grand Total", "A/c No") on the same row, or in the same
column of the row below (a header row over a value row, as most invoices
print it); a few by shape (IFSC).

SAP's own values for the document are then used twice:
  * as the CHECK: each field says whether it agrees with SAP;
  * as a HINT: where no label was found but SAP's value is in the text, that
    is the value (an invoice number is unambiguous once you know it).

Every field comes back as `{value, sap, match}`; `match` is True (agrees),
False (the document shows something else) or None (not found, or nothing in
SAP to compare with).
"""
import re
from datetime import date
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------
_AMOUNT = re.compile(r'(?<![\d.,])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{1,2})(?![\d,])')
_ACCOUNT = re.compile(r'(?<!\d)\d{9,18}(?!\d)')
_IFSC = re.compile(r'\b([A-Z]{4})[0O]([A-Z0-9]{6})\b')
_REF_TOKEN = re.compile(r'[A-Z0-9][A-Z0-9/\-_.]{2,29}', re.I)
_MONTHS = {m: i for i, m in enumerate(
    ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'], start=1)}
_DATE = re.compile(
    r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b'                                   # 2026-08-24
    r'|\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b'                      # 24-08-2026, 24/08/26
    r'|\b(\d{1,2})[\s\-]?([A-Za-z]{3})[A-Za-z]*[\s\-,]*(\d{2,4})\b')     # 7-Dec-24, 24 Aug 2026

# ---------------------------------------------------------------------------
# Labels (lower-case matching)
# ---------------------------------------------------------------------------
_INVOICE_LABEL = re.compile(
    r'(?:tax\s+)?invoice\s*(?:no|number|num|#)\.?|inv\.?\s*no\.?|bill\s*(?:no|number)\.?', re.I)
_DATE_LABELS = [re.compile(p, re.I) for p in (
    r'invoice\s*date', r'bill\s*date', r'inv\.?\s*date', r'\bdated\b', r'\bdate\b')]
_TOTAL_LABEL = re.compile(
    r'grand\s*total|total\s*amount|invoice\s*(?:total|value|amount)|amount\s*payable|'
    r'net\s*(?:payable|amount)|total\s*payable|\btotal\b', re.I)
_ACCOUNT_LABEL = re.compile(
    r'a\s*/?\s*c\.?\s*(?:no|number)|account\s*(?:no|number|#)|bank\s*a\s*/?\s*c', re.I)
_COMPANY_WORDS = re.compile(
    r'\b(?:pvt|private|ltd|limited|llp|traders?|trading|enterprises?|logistics|industries|'
    r'corporation|company|agency|agencies|transport|carriers?|movers|solutions|services)\b|&\s*co\b',
    re.I)
#: Our own companies: never the party a bill is FROM.
_OURS = re.compile(r'\bjivo\b', re.I)

_COLUMN_GAP = re.compile(r'\s{3,}|\t|\s*\|\s*')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _alnum(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def amounts_in(text):
    out = []
    for match in _AMOUNT.finditer(text or ''):
        try:
            out.append(float(match.group(1).replace(',', '')))
        except ValueError:
            continue
    return out


def _to_date(match):
    try:
        if match.group(1):
            y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif match.group(4):
            d, m, y = int(match.group(4)), int(match.group(5)), int(match.group(6))
        else:
            d, y = int(match.group(7)), int(match.group(9))
            m = _MONTHS.get(match.group(8)[:3].upper())
            if not m:
                return None
        if y < 100:
            y += 2000
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


def dates_in(text):
    return [d for d in (_to_date(m) for m in _DATE.finditer(text or '')) if d]


def _cells(row):
    return [c for c in _COLUMN_GAP.split(row) if c.strip()]


def _labelled(rows, label, pick, below=2):
    """The first value `pick` finds after `label`: same row first, then the
    same column of the rows below, then anywhere in those rows."""
    for i, row in enumerate(rows):
        match = label.search(row)
        if not match:
            continue
        value = pick(row[match.end():])
        if value is not None:
            return value
        cells = _cells(row)
        column = next((k for k, cell in enumerate(cells) if label.search(cell)), None)
        for nxt in rows[i + 1:i + 1 + below]:
            below_cells = _cells(nxt)
            if column is not None and column < len(below_cells) and len(below_cells) == len(cells):
                value = pick(below_cells[column])
                if value is not None:
                    return value
        for nxt in rows[i + 1:i + 1 + below]:
            value = pick(nxt)
            if value is not None:
                return value
    return None


def _first_reference(text):
    text = (text or '').lstrip(' .:-#)')
    for match in _REF_TOKEN.finditer(text):
        token = match.group(0).strip('.-/')
        if any(ch.isdigit() for ch in token) and not _DATE.fullmatch(token) \
                and not re.fullmatch(r'\d{10}', token) and len(token) >= 3:
            return token
    return None


def _first_date(text):
    found = dates_in(text)
    return found[0] if found else None


def _first_account(text):
    match = _ACCOUNT.search(text or '')
    return match.group(0) if match else None


def _company_key(name):
    """A name reduced to what identifies it: no legal suffixes, no spacing."""
    text = re.sub(r'\b(?:pvt|private|ltd|limited|llp|and|the|m/?s)\b|&', ' ', str(name or ''), flags=re.I)
    return _alnum(text)


def _same_company(a, b):
    ka, kb = _company_key(a), _company_key(b)
    if not ka or not kb:
        return False
    if ka in kb or kb in ka:
        return True
    return SequenceMatcher(None, ka, kb).ratio() >= 0.8


def _field(value, sap=None, match=None):
    return {'value': value, 'sap': sap, 'match': match}


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------
def _invoice_number(rows, whole, expected):
    squashed = _alnum(whole)
    if expected and len(_alnum(expected)) >= 3 and _alnum(expected) in squashed:
        # SAP's number is on the document: that is the invoice number.
        return _field(expected, expected, True)
    found = _labelled(rows, _INVOICE_LABEL, _first_reference)
    if not expected:
        return _field(found)
    return _field(found, expected, None if found is None else _alnum(found) == _alnum(expected))


def _invoice_date(rows, whole, expected):
    found = None
    for label in _DATE_LABELS:
        found = _labelled(rows, label, _first_date)
        if found:
            break
    if expected and expected in dates_in(whole) and found != expected:
        # A labelled date that is not SAP's (a delivery-note date, say) loses
        # to SAP's date when that is printed on the document too.
        found = expected
    value = found.isoformat() if found else None
    if not expected:
        return _field(value)
    return _field(value, expected.isoformat(), None if found is None else found == expected)


def _amount(rows, whole, expected_totals):
    labelled = []
    for i, row in enumerate(rows):
        if _TOTAL_LABEL.search(row):
            for nearby in rows[i:i + 3]:
                labelled.extend(amounts_in(nearby))
    everything = amounts_in(whole)
    for total in expected_totals:
        if any(abs(a - total) <= 1 for a in everything):
            return _field(total, expected_totals[0], True)
    found = max(labelled) if labelled else (max(everything) if everything else None)
    if not expected_totals:
        return _field(found)
    return _field(found, expected_totals[0],
                  None if found is None else any(abs(found - t) <= 1 for t in expected_totals))


#: Labels a vendor's name follows: "Invoice From : X", "Name : X", "Seller : X".
_PARTY_LABEL = re.compile(
    r'(?:invoice\s*from|supplier(?:\s*name)?|seller(?:dtls_lglnm|\s*name)?|vendor(?:\s*name)?|'
    r'sold\s*by|billed\s*by|legal\s*name|trade\s*name|from|name)\s*:\s*', re.I)
#: A company name that ends in its legal form: "Swiggy Private Limited".
_LEGAL_NAME = re.compile(
    r"((?:[A-Z0-9&][\w&'.\-]*\s+){0,6}?"
    r"(?i:private\s+limited|pvt\.?\s*ltd\.?|limited|ltd\.?|llp)(?![\w]))")


def _clean_party(text):
    text = re.split(r'\s{3,}|\s*\|\s*|\(', text or '')[0]
    text = text.strip(' .:-,')
    if len(text) < 3 or len(text) > 90 or not re.search(r'[A-Za-z]{2}', text) or _OURS.search(text):
        return None
    return text


def _party_name(rows, expected):
    """The party the document is FROM, most reliable reading first."""
    labelled, signed, legal, top = [], [], [], []
    for row in rows:
        for match in _PARTY_LABEL.finditer(row):
            before = row[max(0, match.start() - 14):match.start()].lower()
            if any(word in before for word in ('bank', 'holder', 'account', 'a/c')):
                continue  # "Bank Name : ICICI BANK" names a bank, not the party
            value = _clean_party(row[match.end():])
            if value:
                labelled.append(value)
        for match in _LEGAL_NAME.finditer(row):
            value = _clean_party(match.group(1))
            if value:
                legal.append(value)
    # "For <Party>" over the signature names the issuer.
    for row in reversed(rows):
        match = re.match(r'^\s*for\s+(.{3,90}?)\s*$', row, re.I)
        if match:
            value = _clean_party(match.group(1))
            if value:
                signed.append(value)
    for row in rows[:12]:
        text = re.sub(r'^\s*(?:m/?s\.?|to|from)\s*:?\s*', '', row, flags=re.I)
        if _COMPANY_WORDS.search(text):
            value = _clean_party(text)
            if value:
                top.append(value)
    candidates = labelled + signed + legal + top
    if expected:
        for candidate in candidates:
            if _same_company(candidate, expected):
                return _field(candidate, expected, True)
    found = candidates[0] if candidates else None
    if not expected:
        return _field(found)
    return _field(found, expected, None if found is None else _same_company(found, expected))


def _account_number(rows, whole, expected_accounts):
    present = set(_ACCOUNT.findall(whole))
    for account in expected_accounts:
        if account in present:
            return _field(account, account, True)
    found = _labelled(rows, _ACCOUNT_LABEL, _first_account)
    if not expected_accounts:
        return _field(found)
    return _field(found, ', '.join(expected_accounts), None if found is None else found in expected_accounts)


def _ifsc(whole, expected_ifscs):
    found = None
    for match in _IFSC.finditer(whole.upper()):
        # OCR reads zeros as the letter O: the 5th character is always a zero,
        # and in a branch code that is mostly digits an O is one too
        # ("ICICOO03662" is ICIC0003662).
        branch = match.group(2)
        if sum(ch.isdigit() for ch in branch) >= 3:
            branch = branch.replace('O', '0')
        found = f'{match.group(1)}0{branch}'
        if found in expected_ifscs:
            return _field(found, found, True)
        break
    if not expected_ifscs:
        return _field(found)
    return _field(found, ', '.join(expected_ifscs), None if found is None else found in expected_ifscs)


def extract(rows, sap=None):
    """The invoice's fields, each checked against `sap` where given.

    `sap`: {'invoice_number', 'invoice_date' (date), 'totals' (amounts that
    count as a match, e.g. DocTotal and DocTotal + TDS), 'party_name',
    'accounts', 'ifscs'}; any may be missing.
    """
    sap = sap or {}
    rows = [str(r) for r in rows if str(r).strip()]
    whole = '\n'.join(rows)
    return {
        'invoice_number': _invoice_number(rows, whole, sap.get('invoice_number')),
        'invoice_date': _invoice_date(rows, whole, sap.get('invoice_date')),
        'amount': _amount(rows, whole, [t for t in sap.get('totals', []) if t]),
        'party_name': _party_name(rows, sap.get('party_name')),
        'account_number': _account_number(rows, whole, [a for a in sap.get('accounts', []) if a]),
        'ifsc': _ifsc(whole, [i for i in sap.get('ifscs', []) if i]),
    }
