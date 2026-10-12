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

BALANCES — the partner's balance in Oil and Beverages, shown only where the
SAME card code is the SAME business there: same PAN (its own, or the one in
its GSTIN), else the same name. A code is not always the same partner across
companies (VENDA001056 is RAUNAK NURSERY in Oil, S.N. INDUSTRIES in
Beverages), and a partner kept under another code is not looked for.
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
    where the SAME card code is the same business there.

    `[{company, card_code, card_name, balance, matched_by}]`, its own company
    first; `matched_by` is 'self', 'PAN' or 'name'. A company where the code is
    missing or is another business is left out. A company that cannot be read
    is reported with `error`, without failing the others.
    """
    own = _partner(company, card_code)
    if own is None:
        return []
    pan = _pan(company, card_code)
    out = [{'company': company, 'card_code': _s(own['code']), 'card_name': _s(own['name']),
            'balance': _money(own.get('balance')), 'matched_by': 'self'}]
    for other in BALANCE_COMPANIES:
        if other == company:
            continue
        try:
            row, how = _same_partner(other, card_code, pan, _s(own.get('name')))
        except Exception as exc:  # noqa: BLE001 — one company down, the rest still shown
            out.append({'company': other, 'card_code': card_code, 'card_name': '', 'balance': None,
                        'matched_by': '', 'error': str(exc)[:200]})
            continue
        if row is not None:
            out.append({'company': other, 'card_code': _s(row['code']), 'card_name': _s(row['name']),
                        'balance': _money(row.get('balance')), 'matched_by': how})
    return out
