"""What SAP shows around a document: its relationship map, its journal entry,
and the partner's balance in each company.

RELATIONSHIP MAP — SAP's "Relationship Map": a purchase order and everything
made from it or against it, in the order SAP builds them:

    Purchase Order ─┬─ Goods Receipt PO ─┬─ A/P Invoice ─┬─ A/P Credit Memo (debit note)
                    │                    │               └─ Outgoing Payment
                    │                    └─ Goods Return
                    ├─ A/P Invoice (copied straight from the PO)
                    ├─ A/P Down Payment ── Outgoing Payment
                    └─ OMS advance (an outgoing payment on account, which SAP
                       does not tie to the PO; OMS knows which PO it paid)

Started from a bill or a goods receipt, the map is built from the POs behind
it, so the same picture appears whichever document is opened. A bill with no
PO behind it is its own root. Every node carries its journal entry (TransId),
so its G/L is one click away.

JOURNAL ENTRY — the G/L lines a posted document made (JDT1): each account by
code and name, the business partner where the line is on one, debit, credit.

BALANCES — the partner's balance in Oil and Beverages: the SAME business
there, under the same card code or another one (decided 2026-10-12). Looked
for in this order, each partner once:

    the same code   when it is the same business: same PAN (its own, or the
                    one inside its GSTIN), else the same name
    a GSTIN         any of its GSTINs on a partner there
    its PAN         as that partner's PAN, or inside its GSTIN
    a bank account  any of its bank account numbers (its bank list, OCRB, or
                    its default account), on a partner there
    its name        the same name, letters and digits only (M/s. A.B. Traders
                    = MS AB TRADERS), unless both have PANs and they differ

A code is not always the same partner across companies (VENDA001056 is
RAUNAK NURSERY in Oil, S.N. INDUSTRIES in Beverages): there, the business is
looked for by GSTIN, PAN and name instead. Only partners of the same type
(vendor / customer) count.
"""
from decimal import Decimal

from advance_payment.services.sap import _date, _money, _run, _s, _schema

#: SAP's documents in a purchase trail: table, object type, label.
DOCS = {
    'po': ('OPOR', 22, 'Purchase Order'),
    'grpo': ('OPDN', 20, 'Goods Receipt PO'),
    'bill': ('OPCH', 18, 'A/P Invoice'),
    'goods_return': ('ORPD', 21, 'Goods Return'),
    'credit_memo': ('ORPC', 19, 'A/P Credit Memo'),
    'down_payment': ('ODPO', 204, 'A/P Down Payment'),
    'payment': ('OVPM', 46, 'Outgoing Payment'),
}
#: The companies a partner's balance is shown in, besides the request's own.
BALANCE_COMPANIES = ('OIL', 'BEVERAGES')


def _ids(rows, key='e'):
    return sorted({int(r[key]) for r in rows if r.get(key) is not None})


def _marks(values):
    return ', '.join('?' * len(values))


def _children(company, line_table, base_type, parents):
    """`(child, parent)` pairs of a lines table copied from `parents` of `base_type`."""
    if not parents:
        return []
    sql = (f'SELECT DISTINCT "DocEntry" AS "e", "BaseEntry" AS "p" FROM "{_schema(company)}"."{line_table}" '
           f'WHERE "BaseType" = ? AND "BaseEntry" IN ({_marks(parents)})')
    return [(int(r['e']), int(r['p'])) for r in _run(company, sql, [base_type, *parents], 'relationship map')]


def _bases(company, line_table, entries, base_type):
    """`(document, base)` pairs: what `entries` of a lines table were copied from."""
    if not entries:
        return []
    sql = (f'SELECT DISTINCT "DocEntry" AS "e", "BaseEntry" AS "p" FROM "{_schema(company)}"."{line_table}" '
           f'WHERE "BaseType" = ? AND "DocEntry" IN ({_marks(entries)})')
    return [(int(r['e']), int(r['p'])) for r in _run(company, sql, [base_type, *entries], 'relationship map')]


def _headers(company, kind, entries):
    if not entries:
        return {}
    table = DOCS[kind][0]
    cancelled = '"Canceled"' if kind == 'payment' else '"CANCELED"'
    status = "'C'" if kind == 'payment' else '"DocStatus"'
    ref = "''" if kind == 'payment' else '"NumAtCard"'
    sql = (f'SELECT "DocEntry" AS "e", "DocNum" AS "num", "DocDate" AS "date", "DocTotal" AS "total", '
           f'{status} AS "status", {cancelled} AS "cancelled", "TransId" AS "trans_id", {ref} AS "ref" '
           f'FROM "{_schema(company)}"."{table}" WHERE "DocEntry" IN ({_marks(entries)})')
    return {int(r['e']): r for r in _run(company, sql, list(entries), 'relationship map')}


def _payments_of(company, inv_type, entries):
    """`(payment, document)` pairs: outgoing payments applied to `entries` of `inv_type`."""
    if not entries:
        return []
    sql = (f'SELECT DISTINCT V."DocNum" AS "e", V."DocEntry" AS "p" FROM "{_schema(company)}"."VPM2" V '
           f'JOIN "{_schema(company)}"."OVPM" O ON O."DocEntry" = V."DocNum" '
           f'WHERE V."InvType" = ? AND V."DocEntry" IN ({_marks(entries)}) AND O."Canceled" = \'N\'')
    return [(int(r['e']), int(r['p'])) for r in _run(company, sql, [inv_type, *entries], 'relationship map')]


def _oms_advances(company, pos):
    """`(payment entry, PO entry, request no.)` of OMS advances posted against `pos`."""
    if not pos:
        return []
    from advance_payment.models import RequestDocument, VoucherStatus  # noqa: PLC0415 — app models

    out = []
    rows = (RequestDocument.objects.filter(kind='PO', sap_doc_entry__in=pos, request__company=company)
            .select_related('request'))
    for doc in rows:
        for v in doc.request.vouchers.all():
            if v.status == VoucherStatus.POSTED and v.replaced_by_id is None and v.sap_doc_entry:
                out.append((int(v.sap_doc_entry), int(doc.sap_doc_entry), doc.request.request_no))
    return out


def relationship_map(company, kind, doc_entry):
    """SAP's relationship map around one PO, goods receipt or A/P invoice.

    `{focus: {kind, doc_entry}, nodes: [{key, kind, kind_label, doc_entry,
    doc_num, date, total, status, ref, trans_id, parents: [key], oms}]}`,
    each node's `parents` the documents it was made from or paid against.
    """
    kind = str(kind or '').strip().lower()
    if kind not in ('po', 'grpo', 'bill'):
        raise ValueError('kind must be one of: bill, grpo, po.')
    try:
        entry = int(doc_entry)
    except (TypeError, ValueError):
        raise ValueError('doc_entry must be a number.') from None

    pos, grpos, bills = set(), set(), set()
    if kind == 'po':
        pos.add(entry)
    elif kind == 'grpo':
        grpos.add(entry)
        pos.update(p for _e, p in _bases(company, 'PDN1', [entry], 22))
    else:
        bills.add(entry)
        pos.update(p for _e, p in _bases(company, 'PCH1', [entry], 22))
        behind = [p for _e, p in _bases(company, 'PCH1', [entry], 20)]
        grpos.update(behind)
        pos.update(p for _e, p in _bases(company, 'PDN1', behind, 22))

    edges = []  # (child key, parent key)
    grpo_po = _children(company, 'PDN1', 22, sorted(pos))
    grpos.update(e for e, _p in grpo_po)
    edges += [(('grpo', e), ('po', p)) for e, p in grpo_po]
    edges += [(('grpo', e), ('po', p)) for e, p in _bases(company, 'PDN1', sorted(grpos), 22)]
    bill_po = _children(company, 'PCH1', 22, sorted(pos))
    bill_grpo = _children(company, 'PCH1', 20, sorted(grpos))
    bills.update(e for e, _p in bill_po + bill_grpo)
    edges += [(('bill', e), ('po', p)) for e, p in bill_po] + [(('bill', e), ('grpo', p)) for e, p in bill_grpo]
    returns = _children(company, 'RPD1', 20, sorted(grpos))
    edges += [(('goods_return', e), ('grpo', p)) for e, p in returns]
    memos = _children(company, 'RPC1', 18, sorted(bills))
    edges += [(('credit_memo', e), ('bill', p)) for e, p in memos]
    downs = _children(company, 'DPO1', 22, sorted(pos))
    edges += [(('down_payment', e), ('po', p)) for e, p in downs]
    edges += [(('payment', e), ('bill', p)) for e, p in _payments_of(company, 18, sorted(bills))]
    edges += [(('payment', e), ('down_payment', p)) for e, p in _payments_of(company, 204, sorted({e for e, _p in downs}))]
    advances = _oms_advances(company, sorted(pos))
    edges += [(('payment', e), ('po', p)) for e, p, _no in advances]
    oms = {e: no for e, _p, no in advances}

    keys = {('po', e) for e in pos} | {('grpo', e) for e in grpos} | {('bill', e) for e in bills}
    keys |= {child for child, _parent in edges}
    by_kind = {}
    for k, e in keys:
        by_kind.setdefault(k, set()).add(e)
    headers = {k: _headers(company, k, sorted(es)) for k, es in by_kind.items()}

    nodes = []
    for k in DOCS:
        for e in sorted(by_kind.get(k, ())):
            h = headers[k].get(e)
            if h is None:
                continue
            parents = sorted({f'{pk}:{pe}' for (ck, ce), (pk, pe) in edges if (ck, ce) == (k, e)})
            nodes.append({
                'key': f'{k}:{e}',
                'kind': k,
                'kind_label': DOCS[k][2],
                'doc_entry': e,
                'doc_num': int(h['num']) if h.get('num') is not None else None,
                'date': _date(h.get('date')),
                'total': _money(h.get('total')),
                'status': 'Cancelled' if _s(h.get('cancelled')) == 'Y' else (
                    'Open' if _s(h.get('status')) == 'O' else 'Closed'),
                'ref': _s(h.get('ref')),
                'trans_id': int(h['trans_id']) if h.get('trans_id') is not None else None,
                'parents': parents,
                'oms': oms.get(e, '') if k == 'payment' else '',
            })
    return {'focus': {'kind': kind, 'doc_entry': entry}, 'nodes': nodes}


# ---------------------------------------------------------------------------
# Journal entry
# ---------------------------------------------------------------------------
_JE_HEADER_SQL = '''
    SELECT "TransId" AS "trans_id", "Number" AS "number", "RefDate" AS "posting_date",
           "TaxDate" AS "document_date", "Memo" AS "memo", "TransType" AS "trans_type",
           "BaseRef" AS "base_ref", "Ref1" AS "ref1", "Ref2" AS "ref2"
    FROM "{schema}"."OJDT" WHERE "TransId" = ?
'''

_JE_LINES_SQL = '''
    SELECT J."Line_ID" AS "line", J."Account" AS "account", A."AcctName" AS "account_name",
           J."ShortName" AS "short_name", P."CardName" AS "partner_name",
           J."Debit" AS "debit", J."Credit" AS "credit", J."LineMemo" AS "memo"
    FROM "{schema}"."JDT1" J
    LEFT JOIN "{schema}"."OACT" A ON A."AcctCode" = J."Account"
    LEFT JOIN "{schema}"."OCRD" P ON P."CardCode" = J."ShortName"
    WHERE J."TransId" = ? ORDER BY J."Line_ID"
'''


def journal_entry(company, trans_id):
    """A journal entry's header and G/L lines, or None. Amounts as strings."""
    try:
        tid = int(trans_id)
    except (TypeError, ValueError):
        raise ValueError('trans_id must be a number.') from None
    schema = _schema(company)
    rows = _run(company, _JE_HEADER_SQL.format(schema=schema), [tid], 'journal entry')
    if not rows:
        return None
    h = rows[0]
    lines = _run(company, _JE_LINES_SQL.format(schema=schema), [tid], 'journal entry lines')
    debit = sum((Decimal(str(r['debit'] or 0)) for r in lines), Decimal('0'))
    credit = sum((Decimal(str(r['credit'] or 0)) for r in lines), Decimal('0'))
    return {
        'header': {
            'trans_id': tid,
            'number': int(h['number']) if h.get('number') is not None else None,
            'posting_date': _date(h.get('posting_date')),
            'document_date': _date(h.get('document_date')),
            'memo': _s(h.get('memo')),
            'trans_type': int(h['trans_type']) if str(h.get('trans_type') or '').lstrip('-').isdigit() else None,
            'base_ref': _s(h.get('base_ref')),
            'ref1': _s(h.get('ref1')),
            'ref2': _s(h.get('ref2')),
            'debit': _money(debit),
            'credit': _money(credit),
        },
        'lines': [{
            'line': int(r['line']),
            'account': _s(r.get('account')),
            'account_name': _s(r.get('account_name')),
            # A line on a business partner: its card code and name, beside the control account.
            'partner': _s(r.get('short_name')) if _s(r.get('short_name')) != _s(r.get('account')) else '',
            'partner_name': _s(r.get('partner_name')),
            'debit': _money(r.get('debit')),
            'credit': _money(r.get('credit')),
            'memo': _s(r.get('memo')),
        } for r in lines],
    }


# ---------------------------------------------------------------------------
# One partner, every company
# ---------------------------------------------------------------------------
def _pan(company, card_code):
    """The partner's PAN: its own, else the one inside its GSTIN (characters 3-12)."""
    schema = _schema(company)
    rows = _run(company, f'''SELECT "TaxId0" AS "pan" FROM "{schema}"."CRD7"
                             WHERE "CardCode" = ? AND IFNULL("TaxId0", '') <> \'\'''', [card_code], 'partner PAN')
    for r in rows:
        pan = _s(r.get('pan')).upper()
        if len(pan) == 10:
            return pan
    rows = _run(company, f'''SELECT "GSTRegnNo" AS "g" FROM "{schema}"."CRD1"
                             WHERE "CardCode" = ? AND IFNULL("GSTRegnNo", '') <> \'\'''', [card_code], 'partner GSTIN')
    for r in rows:
        gstin = _s(r.get('g')).upper()
        if len(gstin) == 15:
            return gstin[2:12]
    return ''


def _partner(company, card_code):
    rows = _run(company, f'''SELECT "CardCode" AS "code", "CardName" AS "name", "CardType" AS "type",
                                    "Balance" AS "balance"
                             FROM "{_schema(company)}"."OCRD" WHERE "CardCode" = ?''', [card_code], 'partner')
    return rows[0] if rows else None


def _gstins(company, card_code):
    rows = _run(company, f'''SELECT DISTINCT UPPER("GSTRegnNo") AS "g" FROM "{_schema(company)}"."CRD1"
                             WHERE "CardCode" = ? AND IFNULL("GSTRegnNo", '') <> \'\'''', [card_code], 'partner GSTINs')
    return sorted({_s(r.get('g')) for r in rows if len(_s(r.get('g'))) == 15})


def _accounts(company, card_code):
    """The partner's bank account numbers, upper case, letters and digits only; 9 characters or more."""
    schema = _schema(company)
    rows = _run(company, f'''SELECT "Account" AS "a" FROM "{schema}"."OCRB" WHERE "CardCode" = ?
                             UNION SELECT "DflAccount" AS "a" FROM "{schema}"."OCRD" WHERE "CardCode" = ?''',
                [card_code, card_code], 'partner bank accounts')
    keys = {_name_key(_s(r.get('a'))) for r in rows}
    return sorted(k for k in keys if len(k) >= 9)


def _name_key(name):
    """A name as letters and digits only, upper case: 'M/s. A.B. Traders' -> 'MSABTRADERS'."""
    return ''.join(ch for ch in (name or '').upper() if ch.isalnum())


#: The most partners shown for one company, however many match.
MATCH_LIMIT = 5

_PARTNER_COLUMNS = 'O."CardCode" AS "code", O."CardName" AS "name", O."CardType" AS "type", O."Balance" AS "balance"'


_ACCOUNT_KEY = 'UPPER(REPLACE_REGEXPR(\'[^A-Za-z0-9]\' IN {} WITH \'\' OCCURRENCE ALL))'


def _found_by(company, card_type, gstins, pan, name, accounts=()):
    """`[(row, matched_by)]`: partners in `company` that are the same business by GSTIN, PAN, bank account or name."""
    schema = _schema(company)
    found = []
    if gstins:
        found += [(r, 'GSTIN') for r in _run(company, f'''
            SELECT DISTINCT {_PARTNER_COLUMNS} FROM "{schema}"."OCRD" O
            JOIN "{schema}"."CRD1" A ON A."CardCode" = O."CardCode"
            WHERE O."CardType" = ? AND UPPER(A."GSTRegnNo") IN ({_marks(gstins)})''',
            [card_type, *gstins], 'partner by GSTIN')]
    if pan:
        found += [(r, 'PAN') for r in _run(company, f'''
            SELECT DISTINCT {_PARTNER_COLUMNS} FROM "{schema}"."OCRD" O
            WHERE O."CardType" = ? AND (
                EXISTS (SELECT 1 FROM "{schema}"."CRD7" T WHERE T."CardCode" = O."CardCode" AND UPPER(T."TaxId0") = ?)
                OR EXISTS (SELECT 1 FROM "{schema}"."CRD1" A WHERE A."CardCode" = O."CardCode"
                           AND SUBSTRING(UPPER(A."GSTRegnNo"), 3, 10) = ?))''',
            [card_type, pan, pan], 'partner by PAN')]
    if accounts:
        found += [(r, 'bank account') for r in _run(company, f'''
            SELECT DISTINCT {_PARTNER_COLUMNS} FROM "{schema}"."OCRD" O
            WHERE O."CardType" = ? AND (
                {_ACCOUNT_KEY.format('O."DflAccount"')} IN ({_marks(accounts)})
                OR EXISTS (SELECT 1 FROM "{schema}"."OCRB" B WHERE B."CardCode" = O."CardCode"
                           AND {_ACCOUNT_KEY.format('B."Account"')} IN ({_marks(accounts)})))''',
            [card_type, *accounts, *accounts], 'partner by bank account')]
    key = _name_key(name)
    if key:
        for r in _run(company, f'''
                SELECT {_PARTNER_COLUMNS} FROM "{schema}"."OCRD" O
                WHERE O."CardType" = ? AND UPPER(REPLACE_REGEXPR('[^A-Za-z0-9]' IN O."CardName" WITH '' OCCURRENCE ALL)) = ?''',
                [card_type, key], 'partner by name'):
            other_pan = _pan(company, _s(r['code'])) if pan else ''
            if not (pan and other_pan and other_pan != pan):
                found.append((r, 'name'))
    return found


def _same_partner(company, card_code, pan, name):
    """`(row, matched_by)` of `card_code` in `company` when it is the SAME business, else `(None, '')`.

    Only the same card code is looked at — a partner under another code is not
    shown — and the code must be the same business: the same PAN, or (where
    either has no PAN) the same name. VENDA001056 is RAUNAK NURSERY in Oil and
    S.N. INDUSTRIES in Beverages: same code, different business, not shown.
    """
    row = _partner(company, card_code)
    if row is None:
        return None, ''
    other_pan = _pan(company, card_code)
    if pan and other_pan:
        return (row, 'PAN') if pan == other_pan else (None, '')
    same_name = _s(row.get('name')).strip().upper() == (name or '').strip().upper()
    return (row, 'name') if same_name else (None, '')


def partner_balances(company, card_code):
    """The partner's SAP balance in its own company, and in Oil and Beverages
    wherever the same business is there, under any code (see the module notes).

    `[{company, card_code, card_name, balance, matched_by}]`, its own company
    first; `matched_by` is 'self', 'PAN' or 'name' (the same code), or 'GSTIN',
    'PAN', 'bank account' or 'name' (another code: `card_code` says which). A company where
    the business is not found is left out; one that cannot be read is
    reported with `error`, without failing the others.
    """
    own = _partner(company, card_code)
    if own is None:
        return []
    pan = _pan(company, card_code)
    gstins = _gstins(company, card_code)
    accounts = _accounts(company, card_code)
    out = [{'company': company, 'card_code': _s(own['code']), 'card_name': _s(own['name']),
            'balance': _money(own.get('balance')), 'matched_by': 'self'}]
    for other in BALANCE_COMPANIES:
        if other == company:
            continue
        try:
            row, how = _same_partner(other, card_code, pan, _s(own.get('name')))
            found = [(row, how)] if row is not None else []
            found += _found_by(other, _s(own.get('type')) or 'S', gstins, pan, _s(own.get('name')), accounts)
        except Exception as exc:  # noqa: BLE001 — one company down, the rest still shown
            out.append({'company': other, 'card_code': card_code, 'card_name': '', 'balance': None,
                        'matched_by': '', 'error': str(exc)[:200]})
            continue
        seen = set()
        for row, how in found:
            code = _s(row['code'])
            if code in seen or len(seen) >= MATCH_LIMIT:
                continue
            seen.add(code)
            out.append({'company': other, 'card_code': code, 'card_name': _s(row['name']),
                        'balance': _money(row.get('balance')), 'matched_by': how})
    return out


# ---------------------------------------------------------------------------
# Ledgers: the partner's whole account, and one bill's
# ---------------------------------------------------------------------------
def _doc_label(trans_type):
    from advance_payment.services.sap import DOC_TYPES  # noqa: PLC0415 — one name table

    try:
        code = int(trans_type)
    except (TypeError, ValueError):
        return str(trans_type or '')
    return DOC_TYPES.get(code, f'Type {code}')


def _int(value):
    return int(value) if str(value if value is not None else '').lstrip('-').isdigit() else None


_LEDGER_SQL = '''
    SELECT J."TransId" AS "trans_id", J."Line_ID" AS "line_id", J."TransType" AS "trans_type",
           J."SourceID" AS "doc_entry", J."BaseRef" AS "doc_num", J."Ref1" AS "ref1", J."Ref2" AS "ref2",
           J."RefDate" AS "posting_date", J."DueDate" AS "due_date", J."Debit" AS "debit",
           J."Credit" AS "credit", J."LineMemo" AS "memo"
    FROM "{schema}"."JDT1" J
    WHERE J."ShortName" = ? {since} {until}
    ORDER BY J."RefDate", J."TransId", J."Line_ID"
    LIMIT {limit}
'''

#: The most lines one full ledger returns; narrow the dates for more.
LEDGER_LIMIT = 2000


def full_ledger(company, card_code, date_from=None, date_to=None):
    """Every journal line on the partner's account, in date order, with a running balance.

    `{opening, rows: [...], closing, truncated}`. Balances are signed
    Debit-minus-Credit (positive Dr, negative Cr), and `opening` carries
    everything before `date_from`, so the running balance is the account's
    real balance on each date.
    """
    code = _s(card_code)
    if not code:
        raise ValueError('card_code is required.')
    schema = _schema(company)
    opening = Decimal('0')
    if date_from:
        rows = _run(company, f'SELECT IFNULL(SUM("Debit" - "Credit"), 0) AS "b" FROM "{schema}"."JDT1" '
                             f'WHERE "ShortName" = ? AND "RefDate" < ?', [code, date_from], 'ledger opening')
        opening = Decimal(str(rows[0]['b'] or 0)) if rows else Decimal('0')
    params = [code] + ([date_from] if date_from else []) + ([date_to] if date_to else [])
    sql = _LEDGER_SQL.format(schema=schema, since='AND J."RefDate" >= ?' if date_from else '',
                             until='AND J."RefDate" <= ?' if date_to else '', limit=LEDGER_LIMIT + 1)
    found = _run(company, sql, params, 'ledger')
    truncated = len(found) > LEDGER_LIMIT
    balance = opening
    out = []
    for r in found[:LEDGER_LIMIT]:
        debit, credit = Decimal(str(r['debit'] or 0)), Decimal(str(r['credit'] or 0))
        balance += debit - credit
        out.append({
            'trans_id': int(r['trans_id']),
            'line_id': _int(r.get('line_id')),
            'doc_type_code': _int(r.get('trans_type')),
            'doc_type': _doc_label(r.get('trans_type')),
            'doc_entry': _int(r.get('doc_entry')),
            'doc_num': _s(r.get('doc_num')) or _s(r.get('ref1')) or str(r['trans_id']),
            'ref': _s(r.get('ref2')),
            'posting_date': _date(r.get('posting_date')),
            'due_date': _date(r.get('due_date')),
            'debit': _money(debit),
            'credit': _money(credit),
            'balance': _money(balance),
            'memo': _s(r.get('memo')),
        })
    return {'opening': _money(opening), 'rows': out, 'closing': _money(balance), 'truncated': truncated}


#: Each reconciliation that cleared part of the bill on the vendor's account:
#: the bill's own reconciled amount (one payment can clear several bills at
#: once, so a counterpart's total is not this bill's), and the documents on
#: the other side of it — what paid it.
_BILL_RECONS_SQL = '''
    SELECT H."ReconNum" AS "recon", H."ReconDate" AS "recon_date", I1."ReconSum" AS "amount",
           I1."IsCredit" AS "side"
    FROM "{schema}"."ITR1" I1
    JOIN "{schema}"."OITR" H ON H."ReconNum" = I1."ReconNum"
    WHERE I1."SrcObjTyp" = '18' AND I1."SrcObjAbs" = ? AND I1."ShortName" = ? AND H."Canceled" = 'N'
    ORDER BY H."ReconDate", H."ReconNum"
'''

_RECON_OTHER_SIDE_SQL = '''
    SELECT I2."ReconNum" AS "recon", I2."SrcObjTyp" AS "trans_type", I2."SrcObjAbs" AS "doc_entry",
           J."BaseRef" AS "doc_num", J."RefDate" AS "posting_date", J."TransId" AS "trans_id"
    FROM "{schema}"."ITR1" I2
    LEFT JOIN "{schema}"."OJDT" J ON J."TransId" = I2."TransId"
    WHERE I2."ReconNum" IN ({marks}) AND I2."ShortName" = ? AND I2."IsCredit" <> ?
    ORDER BY I2."ReconNum", I2."LineSeq"
'''

_BILL_ROW_SQL = '''
    SELECT "DocEntry" AS "e", "DocNum" AS "num", "DocDate" AS "date", "DocDueDate" AS "due",
           "NumAtCard" AS "ref", "DocTotal" AS "total", "PaidToDate" AS "paid",
           "CardCode" AS "card", "TransId" AS "trans_id"
    FROM "{schema}"."OPCH" WHERE "DocEntry" = ?
'''


def bill_ledger(company, doc_entry):
    """One A/P invoice's own ledger: the bill, then what settled it, and what is left.

    `{bill, rows, open, sap_open}`. Settlements are SAP's reconciliations: a
    payment applied to the bill, a payment on account reconciled to it later,
    a credit memo, a journal, each with the date it settled and the amount it
    took. `open` is what is left; `sap_open` is SAP's own DocTotal-PaidToDate,
    returned beside it so the two can be seen to agree.
    """
    try:
        entry = int(doc_entry)
    except (TypeError, ValueError):
        raise ValueError('doc_entry must be a number.') from None
    schema = _schema(company)
    rows = _run(company, _BILL_ROW_SQL.format(schema=schema), [entry], 'bill')
    if not rows:
        return None
    b = rows[0]
    total = Decimal(str(b['total'] or 0))
    left = total
    out = [{'kind': 'bill', 'doc_type': 'A/P Invoice', 'doc_type_code': 18, 'doc_num': str(b['num']),
            'doc_entry': entry, 'trans_id': _int(b.get('trans_id')), 'date': _date(b.get('date')),
            'posted': _date(b.get('date')), 'debit': '0', 'credit': _money(total), 'open': _money(total)}]
    card = _s(b.get('card'))
    recons = _run(company, _BILL_RECONS_SQL.format(schema=schema), [entry, card], 'bill reconciliations')
    others = {}
    if recons:
        nums = [int(r['recon']) for r in recons]
        side = _s(recons[0].get('side'))
        for o in _run(company, _RECON_OTHER_SIDE_SQL.format(schema=schema, marks=_marks(nums)),
                      [*nums, card, side], 'bill settled by'):
            others.setdefault(int(o['recon']), []).append(o)
    for r in recons:
        amount = Decimal(str(r['amount'] or 0))
        left -= amount
        by = others.get(int(r['recon']), [])
        first = by[0] if by else {}
        out.append({
            'kind': 'settlement',
            'doc_type': _doc_label(first.get('trans_type')) if first else 'Reconciliation',
            'doc_type_code': _int(first.get('trans_type')),
            'doc_num': (_s(first.get('doc_num')) or str(first.get('doc_entry'))) if first else str(r['recon']),
            'doc_entry': _int(first.get('doc_entry')),
            'trans_id': _int(first.get('trans_id')),
            # Several documents on the other side: all named, the first opened.
            'also': [f"{_doc_label(o.get('trans_type'))} {_s(o.get('doc_num')) or o.get('doc_entry')}"
                     for o in by[1:]],
            'date': _date(r.get('recon_date')),
            'posted': _date(first.get('posting_date')) if first else None,
            'debit': _money(amount),
            'credit': '0',
            'open': _money(left),
        })
    return {
        'bill': {'doc_entry': entry, 'doc_num': int(b['num']), 'vendor_ref': _s(b.get('ref')),
                 'card_code': _s(b.get('card')), 'date': _date(b.get('date')), 'due_date': _date(b.get('due')),
                 'total': _money(total)},
        'rows': out,
        'open': _money(left),
        'sap_open': _money(total - Decimal(str(b['paid'] or 0))),
    }
