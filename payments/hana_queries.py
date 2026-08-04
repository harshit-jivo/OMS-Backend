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
            DAYS_BETWEEN(T0."DocDueDate", CURRENT_DATE) AS "days_overdue"
        FROM "{schema}"."OINV" AS T0
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


def invoice_balance_sql(schema):
    return f'''
        SELECT
            T0."DocEntry" AS "doc_entry",
            T0."DocTotal" - IFNULL(T0."PaidToDate", 0) AS "balance_due",
            T0."DocStatus" AS "doc_status",
            T0."CANCELED"  AS "cancelled"
        FROM "{schema}"."OINV" AS T0
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
