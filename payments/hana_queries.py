"""Live HANA reads for the payment cascade.

There is no open-invoice query anywhere in the existing codebase: every
`DocStatus='O'` query targets ORDR (sales orders), and `get_invoice_status`
(hana/services/connection.py:487) reads OWDD/ODRF — draft APPROVALS, not
posted AR. This module adds the missing OINV read.

Unlike the 21 builders in hana/services/connection.py, every value here is a
BIND PARAMETER. `HANAConnection.execute(sql, params)` has always supported them
(connection.py:42-44); they were simply never used, which is why
`get_customer_details` interpolates request query params straight into SQL.

The schema name still has to be interpolated (HANA cannot bind an identifier),
so it comes from SapCompanyMap — a DB allow-list — and never from a request.
"""
import logging

from django.core.exceptions import ValidationError

from django.core.cache import cache

from hana.services.connection import HANAConnection

from .models import SapCompanyMap

logger = logging.getLogger(__name__)


def _schema_for(company):
    """Allow-listed HANA schema for a company. Raises if not configured."""
    mapping = SapCompanyMap.objects.filter(company=company, is_active=True).first()
    if not mapping:
        raise ValidationError(f'No active SAP mapping for company "{company}".')
    schema = mapping.hana_schema or mapping.company_db
    if not schema:
        raise ValidationError(f'SAP mapping for "{company}" has no HANA schema.')
    return schema


def open_invoices_sql(schema):
    return f'''
        SELECT
            T0."DocEntry"    AS "doc_entry",
            T0."DocNum"      AS "doc_num",
            T0."DocDate"     AS "doc_date",
            T0."DocDueDate"  AS "due_date",
            T0."NumAtCard"   AS "party_ref",
            T0."CardCode"    AS "card_code",
            T0."CardName"    AS "card_name",
            T0."DocCur"      AS "currency",
            T0."DocTotal"    AS "doc_total",
            IFNULL(T0."PaidToDate", 0) AS "paid_to_date",
            T0."DocTotal" - IFNULL(T0."PaidToDate", 0) AS "balance_due",
            DAYS_BETWEEN(T0."DocDueDate", CURRENT_DATE) AS "days_overdue",
            T0."BPLId"       AS "bpl_id",
            IFNULL(T1."BPLName", '') AS "bpl_name"
        FROM "{schema}"."OINV" AS T0
        LEFT JOIN "{schema}"."OBPL" AS T1 ON T1."BPLId" = T0."BPLId"
        WHERE T0."CardCode"  = ?
          AND T0."DocStatus" = 'O'
          AND T0."CANCELED"  = 'N'
          AND T0."DocType"   = 'I'
          AND T0."DocTotal" - IFNULL(T0."PaidToDate", 0) > ?
          AND ( ? = '' OR T0."DocNum" LIKE ? OR T0."NumAtCard" LIKE ? )
        ORDER BY T0."DocDueDate" ASC, T0."DocNum" ASC
        LIMIT ? OFFSET ?
    '''


def fetch_open_invoices(*, company, card_code, search='', min_balance=0.01,
                        limit=100, offset=0):
    """Open A/R invoices for one party in one company.

    Notes on the filters, each of which matters:
      * DocStatus='O' alone is not enough — a cancelled invoice can still read
        'O', hence CANCELED='N'.
      * DocType='I' excludes service invoices, which have no items.
      * PaidToDate can be NULL, so IFNULL is required or the arithmetic yields
        NULL and the row silently disappears.
    """
    schema = _schema_for(company)
    like = f'%{search}%' if search else ''
    params = [card_code, float(min_balance), search or '', like, like,
              int(limit), int(offset)]
    with HANAConnection() as conn:
        rows = conn.execute(open_invoices_sql(schema), params)

    # HANA CHAR columns come back space-padded — always strip.
    for row in rows:
        for key in ('card_code', 'card_name', 'party_ref', 'currency'):
            if isinstance(row.get(key), str):
                row[key] = row[key].strip()
    return rows


def parties_with_open_invoices_sql(schema):
    """Card codes that currently owe money, with their totals.

    The WHERE clause is deliberately IDENTICAL to open_invoices_sql — if the two
    ever drift, a party appears in the picker and then shows an empty invoice
    list (or disappears while genuinely owing). Keep them in step.
    """
    return f'''
        SELECT
            T0."CardCode"                                   AS "card_code",
            COUNT(*)                                        AS "open_count",
            SUM(T0."DocTotal" - IFNULL(T0."PaidToDate", 0)) AS "balance_due"
        FROM "{schema}"."OINV" AS T0
        WHERE T0."DocStatus" = 'O'
          AND T0."CANCELED"  = 'N'
          AND T0."DocType"   = 'I'
          AND T0."DocTotal" - IFNULL(T0."PaidToDate", 0) > ?
        GROUP BY T0."CardCode"
    '''


def fetch_parties_with_open_invoices(*, company, min_balance=0.01):
    """{card_code: {open_count, balance_due}} for every party that owes money.

    ONE aggregate query for the whole company, not one per party: the picker
    needs the entire set to filter against, and 500+ round trips would make it
    unusable. ~500 rows for OIL, so the payload is small.
    """
    schema = _schema_for(company)
    with HANAConnection() as conn:
        rows = conn.execute(parties_with_open_invoices_sql(schema),
                            [float(min_balance)])
    return {
        (row['card_code'] or '').strip(): {
            'open_count': int(row['open_count'] or 0),
            'balance_due': row['balance_due'] or 0,
        }
        for row in rows
        if (row.get('card_code') or '').strip()
    }


def invoice_balance_sql(schema):
    return f'''
        SELECT
            T0."DocEntry" AS "doc_entry",
            T0."DocTotal" - IFNULL(T0."PaidToDate", 0) AS "balance_due",
            T0."DocStatus" AS "doc_status",
            T0."CANCELED"  AS "cancelled",
            T0."BPLId"     AS "bpl_id",
            IFNULL(T1."BPLName", '') AS "bpl_name"
        FROM "{schema}"."OINV" AS T0
        LEFT JOIN "{schema}"."OBPL" AS T1 ON T1."BPLId" = T0."BPLId"
        WHERE T0."DocEntry" = ?
    '''


def fetch_invoice_balance(*, company, doc_entry):
    """Current balance of one invoice.

    Called immediately before posting so the worker can compare against the
    snapshot taken at selection time and fail loudly, rather than letting SAP
    reject the whole payment with an opaque -5002.
    """
    schema = _schema_for(company)
    with HANAConnection() as conn:
        rows = conn.execute(invoice_balance_sql(schema), [int(doc_entry)])
    return rows[0] if rows else None


def company_banks_sql(schema):
    """Every House Bank ACCOUNT in this company, with its bank's display name.

    DSC1 is the account table and is the source of truth: a bank present in the
    ODSC master but absent here has no account to pay into or out of, and SAP
    refuses the document. ODSC is joined only for a readable name.

    Note DSC1 holds one row per ACCOUNT, so a bank with three accounts appears
    three times — the GL account, not the bank code, is what identifies a row.
    """
    return f'''
        SELECT
            T0."BankCode"                        AS "bank_code",
            IFNULL(T1."BankName", T0."BankCode") AS "bank_name",
            T0."GLAccount"                       AS "gl_account",
            IFNULL(T0."Account", \'\')             AS "account_number",
            IFNULL(T0."Branch", \'\')              AS "branch",
            IFNULL(T0."ControlKey", \'\')          AS "control_key",
            IFNULL(T0."IBAN", \'\')                AS "iban",
            IFNULL(T1."SwiftNum", \'\')            AS "swift"
        FROM "{schema}"."DSC1" AS T0
        LEFT JOIN "{schema}"."ODSC" AS T1 ON T1."BankCode" = T0."BankCode"
        WHERE T0."GLAccount" IS NOT NULL AND T0."GLAccount" <> \'\'
        ORDER BY T0."BankCode", T0."GLAccount"
    '''


def fetch_company_banks(*, company):
    """Live read of every House Bank Account. Raises if SAP is unreachable.

    Deliberately uncached: caching, and what to do when this raises, belong to
    the service layer that knows whether a stale answer is acceptable.
    """
    schema = _schema_for(company)
    with HANAConnection() as conn:
        rows = conn.execute(company_banks_sql(schema))

    out = []
    for row in rows:
        gl = str(row.get('gl_account') or '').strip()
        code = str(row.get('bank_code') or '').strip()
        if not gl or not code:
            continue
        name = str(row.get('bank_name') or code).strip()
        # SAP has no dedicated IFSC column; Indian localisations keep it in
        # ControlKey, falling back to SWIFT. Blank when neither is filled in —
        # never invented.
        ifsc = (str(row.get('control_key') or '').strip()
                or str(row.get('swift') or '').strip())
        account = str(row.get('account_number') or '').strip()
        out.append({
            'bank_code': code,
            'display_name': name,
            'gl_account': gl,
            'account_number': account,
            'branch': str(row.get('branch') or '').strip(),
            'ifsc': ifsc,
            # Unique per ACCOUNT, because one bank can hold several. This is
            # what a dropdown selects and what a payload resolves from.
            'key': f'{code}:{gl}',
            'label': (f'{name} — {account}' if account else name),
        })
    return out


def invoice_branches_sql(schema):
    """Branch of each of several invoices, in one round trip."""
    return f'''
        SELECT
            T0."DocEntry"                   AS "doc_entry",
            T0."DocNum"                     AS "doc_num",
            T0."BPLId"                      AS "bpl_id",
            IFNULL(T1."BPLName", '')        AS "bpl_name",
            T0."DocStatus"                  AS "doc_status",
            T0."CANCELED"                   AS "cancelled"
        FROM "{schema}"."OINV" AS T0
        LEFT JOIN "{schema}"."OBPL" AS T1 ON T1."BPLId" = T0."BPLId"
        WHERE T0."DocEntry" IN ({{placeholders}})
    '''


def fetch_invoice_branches(*, company, doc_entries):
    """Branch of each invoice. Raises if SAP cannot be reached.

    Deliberately NOT swallowing errors: the branch decides which SAP ledger a
    payment lands in, so "could not check" must never be mistaken for "use the
    default". The caller turns a failure into a refusal to post.
    """
    entries = [int(d) for d in doc_entries if d]
    if not entries:
        return []
    schema = _schema_for(company)
    placeholders = ', '.join(['?'] * len(entries))
    sql = invoice_branches_sql(schema).replace('{placeholders}', placeholders)
    with HANAConnection() as conn:
        rows = conn.execute(sql, entries)
    return [{
        'doc_entry': int(r['doc_entry']),
        'doc_num': r.get('doc_num'),
        'bpl_id': int(r['bpl_id']) if r.get('bpl_id') not in (None, '') else None,
        'bpl_name': str(r.get('bpl_name') or '').strip(),
        'doc_status': str(r.get('doc_status') or '').strip(),
        'cancelled': str(r.get('cancelled') or '').strip(),
    } for r in rows]
