"""Reading the things an advance payment can be raised against.

WHAT THIS FILE IS
-----------------
Five read-only lookups against the SAP company databases:

    open purchase orders    OPOR (+ POR1 for the open portion)
    open invoices           OPCH (vendor) / OINV (customer)
    vendors                 OCRD  CardType = 'S'
    customers               OCRD  CardType = 'C'
    employees               OACT  children of the employee-advance parent

Direct HANA, never the Service Layer — OMS creates no document here, so there
is nothing for SAP's document validation to do. `production/services/sap.py`
and `tracker/sap.py` read the same way for the same reason.

THE SCHEMA IS THE ONE INTERPOLATED THING
-----------------------------------------
HANA cannot bind an identifier, so the schema name is formatted into the SQL.
It must therefore come from `Queries._schema_for_branch()`, which resolves
against settings and refuses anything else, and NEVER from a request. Every
other value binds with `?`.

COMPANY IS REQUIRED, WITH NO DEFAULT
-------------------------------------
Not a convenience omitted. The three company databases hold different
documents under the same DocNum, so a missing company that quietly fell back
to OIL would not fail — it would answer with another company's vendors and
another company's open POs, and an advance raised against one of them would be
wrong in a way nothing downstream could detect. `_schema_for_branch` raises on
a blank branch and this module turns that into a 400.
"""
import logging
import re
from decimal import Decimal

from django.conf import settings

from hana.services.connection import HANAConnection, HanaSchemaError, Queries

logger = logging.getLogger(__name__)


class SapUnavailable(Exception):
    """HANA could not be reached, or the query failed.

    Raised rather than returning `[]`, because an empty list already means
    something here — "this vendor has no open POs" — and the two answers lead
    to opposite actions.
    """


class UnknownCompany(ValueError):
    """The caller named a company that is not configured."""


#: How many rows a lookup returns at most, and the default when the caller does
#: not say. These feed pickers, not reports: Oil has 335 employee-advance
#: accounts and thousands of business partners, and a picker that ships all of
#: them is slower to use than one that is searched.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: Parent of the per-employee advance accounts. `1113000 EMPLOYEES ADVANCES`
#: in all three companies (Oil 335 postable children, Mart 149, Beverages 112),
#: which is why one constant covers them — but it is settings-overridable
#: because it is a chart-of-accounts fact, not a law, and a company added later
#: may number it differently.
EMPLOYEE_ADVANCE_PARENT = str(
    getattr(settings, 'ADVANCE_PAYMENT_EMPLOYEE_GL_PARENT', '1113000')).strip()

#: `RAVINDER SINGH SHUNTY ADVANCE JWPL0035` -> name + code.
#:
#: Matched from the RIGHT: the separator is the literal word ADVANCE and a
#: person can be called anything, so the last occurrence is the one that
#: precedes the code.
_ADVANCE_MARKER = ' ADVANCE '

#: The shape of a payroll code, used only to recognise one when an account is
#: named without the ADVANCE marker. Deliberately narrow — a wrong guess here
#: puts someone else's code on an advance.
_EMP_CODE = re.compile(r'^[A-Z]{2,6}\d{2,6}$')

#: The LIKE escape clause, spelled once.
#:
#: In Python "\\" is ONE backslash, so this renders as: ESCAPE '\'
#: That is what makes the \% and \_ inserted by `_like` mean a literal percent
#: and underscore. A constant rather than inline, because inline it is a row of
#: escaped quotes in four different queries and is unreadable in all of them.
_ESC = " ESCAPE '\\'"


def _schema(company):
    """The HANA schema for `company`, or `UnknownCompany`."""
    try:
        return Queries._schema_for_branch(company)
    except HanaSchemaError as exc:
        raise UnknownCompany(str(exc)) from exc


def _limit(value):
    """A sane row cap. Formatted into the SQL, so it must be an int — it is
    coerced here rather than trusted, and that coercion is the whole defence."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _like(search):
    """A bound, case-insensitive `contains` pattern, or None.

    The wildcards go in the VALUE, not into the SQL, so the pattern is still a
    bound parameter. `%` and `_` typed by the user are escaped — without that,
    searching for "50%" matches every row in the table.
    """
    text = (search or '').strip()
    if not text:
        return None
    escaped = (text.replace('\\', '\\\\')
                   .replace('%', '\\%')
                   .replace('_', '\\_'))
    return f'%{escaped.upper()}%'


def _run(company, sql, params, what):
    """Execute, turning any driver failure into `SapUnavailable`."""
    try:
        with HANAConnection() as conn:
            return conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
        logger.warning('ADVANCE: reading %s for %s failed: %s',
                       what, company, exc, exc_info=True)
        raise SapUnavailable(
            f'Could not read {what} for {company}: '
            f'{type(exc).__name__}: {exc}') from exc


def _s(value):
    """A trimmed string, never None."""
    return value.strip() if isinstance(value, str) else (value or '')


def _money(value):
    """Amounts as STRINGS.

    SAP money is `numeric(19,6)`; a JSON number loses the tail, and an advance
    is a payment instruction. The rest of the project sends amounts the same
    way — `production`'s `planned_qty` had to be fixed for exactly this.
    """
    return '0' if value is None else str(value)


def _date(value):
    return value.date().isoformat() if hasattr(value, 'date') else value


# ---------------------------------------------------------------------------
# Business partners
# ---------------------------------------------------------------------------
#: `validFor` / `frozenFor` are the two switches that retire a partner without
#: deleting it. A frozen vendor must not appear in a picker that raises a
#: payment to them: SAP would refuse the document, and the refusal would arrive
#: after the request had been approved.
_PARTNER_SQL = '''
    SELECT
        "CardCode"   AS "card_code",
        "CardName"   AS "card_name",
        "LicTradNum" AS "gstin",
        "Currency"   AS "currency",
        "Balance"    AS "balance",
        "Phone1"     AS "phone",
        "E_Mail"     AS "email"
    FROM "{schema}"."OCRD"
    WHERE "CardType" = ?
      AND IFNULL("validFor",  'Y') = 'Y'
      AND IFNULL("frozenFor", 'N') = 'N'
      {search}
    ORDER BY "CardName"
    LIMIT {limit}
'''


def _partners(company, card_type, search, limit, what):
    schema = _schema(company)
    pattern = _like(search)
    sql = _PARTNER_SQL.format(
        schema=schema,
        search=(f'AND (UPPER("CardCode") LIKE ?{_ESC} '
                f'OR UPPER("CardName") LIKE ?{_ESC})') if pattern else '',
        limit=_limit(limit),
    )
    params = [card_type] + ([pattern, pattern] if pattern else [])
    return [{
        'card_code': _s(r.get('card_code')),
        'card_name': _s(r.get('card_name')),
        'gstin': _s(r.get('gstin')),
        'currency': _s(r.get('currency')),
        # SAP's sign convention, passed through rather than reversed: reversing
        # it here would disagree with every SAP screen the user can check it
        # against.
        'balance': _money(r.get('balance')),
        'phone': _s(r.get('phone')),
        'email': _s(r.get('email')),
    } for r in _run(company, sql, params, what)]


def vendors(company, search=None, limit=None):
    """Active, unfrozen suppliers (`OCRD.CardType = 'S'`)."""
    return _partners(company, 'S', search, limit, 'vendors')


def customers(company, search=None, limit=None):
    """Active, unfrozen customers (`OCRD.CardType = 'C'`)."""
    return _partners(company, 'C', search, limit, 'customers')


# ---------------------------------------------------------------------------
# Open purchase orders
# ---------------------------------------------------------------------------
#: `DocStatus = 'O'` is SAP's own definition of open, and `CANCELED` is a
#: SEPARATE flag — a cancelled order keeps its status, so testing one without
#: the other lists orders nobody will ever receive.
#:
#: OPEN AMOUNT = `DocTotal - PaidToDate`, both tax-inclusive. Verified, and it
#: replaces an earlier version of this query that was wrong:
#:
#:   * `OPOR.PaidToDate` is the value already RECEIVED against the order.
#:     PO 220926081 has 38.6 of 40 units received; its GRPO lines total
#:     5,867,200 and with 5% GST that is 6,160,560 — exactly `PaidToDate`.
#:     It is NOT advances: in Oil there are no A/P down payments (`ODPO` is
#:     empty) and no outgoing payment has ever been applied to a PO
#:     (`VPM2.InvType = 22` has no rows).
#:   * `POR1.OpenSum` is NOT reduced by receipts here. The same PO still shows
#:     6,080,000 open on its lines while 96.5% has arrived, so summing it —
#:     which the previous version did — reported a nearly-delivered PO as
#:     nearly untouched.
#:
#: 6,384,000 - 6,160,560 = 223,440 = 1.4/40 of the order, to the rupee.
#:
#: A PO with nothing left to receive is left out: status 'O' with the full
#: value received happens (220826059 is one), and offering it in a picker is
#: offering a document there is nothing to advance against.
_OPEN_PO_SQL = '''
    SELECT
        T0."DocEntry"   AS "doc_entry",
        T0."DocNum"     AS "doc_num",
        T0."CardCode"   AS "card_code",
        T0."CardName"   AS "card_name",
        T0."NumAtCard"  AS "vendor_ref",
        T0."DocDate"    AS "doc_date",
        T0."DocDueDate" AS "due_date",
        T0."DocCur"     AS "currency",
        T0."DocTotal"   AS "doc_total",
        IFNULL(T0."VatSum", 0)     AS "tax_amount",
        IFNULL(T0."PaidToDate", 0) AS "received_amount",
        T0."DocTotal" - IFNULL(T0."PaidToDate", 0) AS "open_amount",
        T0."Comments"   AS "remarks"
    FROM "{schema}"."OPOR" T0
    WHERE T0."DocStatus" = 'O'
      AND IFNULL(T0."CANCELED", 'N') = 'N'
      AND T0."DocTotal" - IFNULL(T0."PaidToDate", 0) > 0
      {card}
      {search}
    ORDER BY T0."DocDate" DESC, T0."DocEntry" DESC
    LIMIT {limit}
'''


def open_purchase_orders(company, card_code=None, search=None, limit=None):
    """Purchase orders with something still to receive, newest first.

    `card_code` narrows to one vendor — the normal call, since an advance is
    raised against a vendor and then against one of their orders.
    """
    schema = _schema(company)
    code = _s(card_code)
    pattern = _like(search)
    sql = _OPEN_PO_SQL.format(
        schema=schema,
        card='AND T0."CardCode" = ?' if code else '',
        search=(f'AND (UPPER(T0."CardName") LIKE ?{_ESC} '
                f'OR UPPER(IFNULL(T0."NumAtCard", \'\')) LIKE ?{_ESC} '
                f'OR TO_VARCHAR(T0."DocNum") LIKE ?)') if pattern else '',
        limit=_limit(limit),
    )
    params = ([code] if code else []) + ([pattern] * 3 if pattern else [])
    rows = [{
        'doc_entry': int(r['doc_entry']),
        'doc_num': int(r['doc_num']) if r.get('doc_num') is not None else None,
        'card_code': _s(r.get('card_code')),
        'card_name': _s(r.get('card_name')),
        'vendor_ref': _s(r.get('vendor_ref')),
        'doc_date': _date(r.get('doc_date')),
        'due_date': _date(r.get('due_date')),
        'currency': _s(r.get('currency')),
        # All three tax-inclusive, as SAP prints the order, so they add up:
        # doc_total = received_amount + open_amount.
        'doc_total': _money(r.get('doc_total')),
        'tax_amount': _money(r.get('tax_amount')),
        'received_amount': _money(r.get('received_amount')),
        'open_amount': _money(r.get('open_amount')),
        'remarks': _s(r.get('remarks')),
    } for r in _run(company, sql, params, 'open purchase orders')]
    return _with_attachments(company, 'OPOR', rows)


# ---------------------------------------------------------------------------
# Open invoices
# ---------------------------------------------------------------------------
#: vendor -> A/P invoice, customer -> A/R invoice. Two tables of the same
#: shape, so one query serves both; the caller says which side it is on rather
#: than this inferring it from something else.
_INVOICE_TABLE = {'vendor': 'OPCH', 'customer': 'OINV'}

_OPEN_INVOICE_SQL = '''
    SELECT
        "DocEntry"   AS "doc_entry",
        "DocNum"     AS "doc_num",
        "CardCode"   AS "card_code",
        "CardName"   AS "card_name",
        "NumAtCard"  AS "party_ref",
        "DocDate"    AS "doc_date",
        "DocDueDate" AS "due_date",
        "DocCur"     AS "currency",
        "DocTotal"   AS "doc_total",
        "PaidToDate" AS "paid_to_date",
        "DocTotal" - IFNULL("PaidToDate", 0) AS "balance_due"
    FROM "{schema}"."{table}"
    WHERE "DocStatus" = 'O'
      AND IFNULL("CANCELED", 'N') = 'N'
      {card}
      {search}
    ORDER BY "DocDueDate", "DocEntry" DESC
    LIMIT {limit}
'''


def open_invoices(company, party_type='vendor', card_code=None, search=None,
                  limit=None):
    """Unpaid invoices, oldest due first.

    `party_type` is 'vendor' (A/P — the advance we pay) or 'customer' (A/R —
    the advance we receive). Anything else raises `ValueError` rather than
    defaulting: silently reading the wrong ledger would put a customer's
    invoice in front of someone about to pay a supplier.
    """
    table = _INVOICE_TABLE.get(str(party_type or '').strip().lower())
    if not table:
        raise ValueError(
            f'party_type must be one of: {", ".join(sorted(_INVOICE_TABLE))}.')

    schema = _schema(company)
    code = _s(card_code)
    pattern = _like(search)
    sql = _OPEN_INVOICE_SQL.format(
        schema=schema,
        table=table,
        card='AND "CardCode" = ?' if code else '',
        search=(f'AND (UPPER("CardName") LIKE ?{_ESC} '
                f'OR UPPER(IFNULL("NumAtCard", \'\')) LIKE ?{_ESC} '
                f'OR TO_VARCHAR("DocNum") LIKE ?)') if pattern else '',
        limit=_limit(limit),
    )
    params = ([code] if code else []) + ([pattern] * 3 if pattern else [])
    rows = [{
        'doc_entry': int(r['doc_entry']),
        'doc_num': int(r['doc_num']) if r.get('doc_num') is not None else None,
        'card_code': _s(r.get('card_code')),
        'card_name': _s(r.get('card_name')),
        'party_ref': _s(r.get('party_ref')),
        'doc_date': _date(r.get('doc_date')),
        'due_date': _date(r.get('due_date')),
        'currency': _s(r.get('currency')),
        'doc_total': _money(r.get('doc_total')),
        'paid_to_date': _money(r.get('paid_to_date')),
        'balance_due': _money(r.get('balance_due')),
    } for r in _run(company, sql, params, f'open {party_type} invoices')]
    return _with_attachments(company, table, rows)


# ---------------------------------------------------------------------------
# Document attachments (ATC1)
# ---------------------------------------------------------------------------
#: A document's attachments are ATC1 rows under its `AtcEntry`, one per file,
#: numbered by `Line` in the order they were added. The LAST line is the one
#: shown: it is the latest, and in practice the one the others were rolled up
#: into (a scan of the signed invoice with its approvals, say).
#:
#: The tables a document kind may be read from. The view takes a KIND, never
#: a table name, and this is the whole list.
ATTACHMENT_TABLES = {'po': 'OPOR', 'bill': 'OPCH'}

_LATEST_ATTACHMENTS_SQL = '''
    SELECT
        D."DocEntry"                                        AS "doc_entry",
        A."FileName"                                        AS "file_name",
        A."FileExt"                                         AS "file_ext",
        A."Date"                                            AS "date",
        N."count"                                           AS "count"
    FROM "{schema}"."{table}" D
    JOIN (
        SELECT "AbsEntry", MAX("Line") AS "line", COUNT(*) AS "count"
        FROM "{schema}"."ATC1"
        GROUP BY "AbsEntry"
    ) N ON N."AbsEntry" = D."AtcEntry"
    JOIN "{schema}"."ATC1" A ON A."AbsEntry" = N."AbsEntry" AND A."Line" = N."line"
    WHERE D."DocEntry" IN ({entries})
'''


def _file_name(name, ext):
    name, ext = _s(name), _s(ext)
    return f'{name}.{ext}' if ext else name


def latest_attachments(company, table, doc_entries):
    """`{doc_entry: {file_name, count, date}}` for those that have any."""
    entries = sorted({int(e) for e in doc_entries})
    if not entries:
        return {}
    sql = _LATEST_ATTACHMENTS_SQL.format(
        schema=_schema(company), table=table, entries=', '.join('?' for _ in entries))
    return {
        int(r['doc_entry']): {
            'file_name': _file_name(r.get('file_name'), r.get('file_ext')),
            'count': int(r.get('count') or 0),
            'date': _date(r.get('date')),
        }
        for r in _run(company, sql, entries, 'document attachments')
    }


def _with_attachments(company, table, rows):
    """Each row gains `attachment`: its latest SAP attachment, or None.

    One extra query for the whole page, not one per row. An attachment that
    cannot be read is not a reason to hide the documents, so a failure here
    leaves `attachment` None and is logged.
    """
    try:
        found = latest_attachments(company, table, [r['doc_entry'] for r in rows])
    except SapUnavailable:
        logger.warning('advance_payment: attachments unreadable for %s %s', company, table)
        found = {}
    for r in rows:
        r['attachment'] = found.get(r['doc_entry'])
    return rows


_DOCUMENT_FACTS_SQL = '''
    SELECT
        "DocEntry"   AS "doc_entry",
        "DocNum"     AS "doc_num",
        "NumAtCard"  AS "vendor_ref",
        "TaxDate"    AS "document_date",
        "DocTotal"   AS "doc_total",
        IFNULL("WTSum", 0) AS "tds",
        "CardCode"   AS "card_code",
        "CardName"   AS "card_name"
    FROM "{schema}"."{table}"
    WHERE "DocEntry" = ?
'''


def document_facts(company, kind, doc_entry):
    """What SAP says about one PO / bill, to check its attachment against.

    `document_date` is SAP's TaxDate, the vendor's own document date (DocDate
    is when it was posted here). `gross_total` is DocTotal plus the TDS SAP
    deducted: the figure the vendor's invoice itself shows.
    """
    table = ATTACHMENT_TABLES.get(str(kind or '').strip().lower())
    if not table:
        raise ValueError(f'kind must be one of: {", ".join(sorted(ATTACHMENT_TABLES))}.')
    try:
        entry = int(doc_entry)
    except (TypeError, ValueError):
        raise ValueError('doc_entry must be a number.') from None
    rows = _run(company, _DOCUMENT_FACTS_SQL.format(schema=_schema(company), table=table),
                [entry], 'document')
    if not rows:
        return None
    r = rows[0]
    total, tds = _money(r.get('doc_total')), _money(r.get('tds'))
    return {
        'doc_num': int(r['doc_num']) if r.get('doc_num') is not None else None,
        'vendor_ref': _s(r.get('vendor_ref')),
        'document_date': _date(r.get('document_date')),
        'doc_total': total,
        'tds': tds,
        'gross_total': str(Decimal(total) + Decimal(tds)),
        'card_code': _s(r.get('card_code')),
        'card_name': _s(r.get('card_name')),
    }


def document_attachment(company, kind, doc_entry):
    """The latest attachment of one document, or None. `kind`: po | bill."""
    table = ATTACHMENT_TABLES.get(str(kind or '').strip().lower())
    if not table:
        raise ValueError(f'kind must be one of: {", ".join(sorted(ATTACHMENT_TABLES))}.')
    try:
        entry = int(doc_entry)
    except (TypeError, ValueError):
        raise ValueError('doc_entry must be a number.') from None
    return latest_attachments(company, table, [entry]).get(entry)


# ---------------------------------------------------------------------------
# Every open document against one business partner
# ---------------------------------------------------------------------------
#: NOT `OIPF`.
#:
#: `OIPF` is the LANDED COSTS document — its columns are `BillOfLad`,
#: `TrnspCode`, `ExCustomFC`, `CostFactor`, `incCustom`. It holds 558 rows in
#: Oil, 6 in Beverages and 0 in Mart, which is import files, not open items;
#: Oil alone has thousands of unpaid invoices. Reading it for this would
#: return nothing in Mart and the wrong thing everywhere else.
#:
#: The open items are in JDT1, the journal-entry lines. That is not a detour —
#: it is where SAP's own Business Partner Ageing reads from, and it is the only
#: single place that has ALL of them:
#:
#:     13  A/R invoice        18  A/P invoice        24  incoming payment
#:     14  A/R credit memo    19  A/P credit memo    46  outgoing payment
#:     30  journal entry
#:
#: (Those seven are what actually occur against a partner in Oil today.) A
#: query over OINV/OPCH alone would miss on-account payments and journals, so
#: the balance it showed would not agree with SAP.
#:
#: `BalDueDeb` / `BalDueCred` are the amounts still OPEN after internal
#: reconciliation — the whole point. `Debit` / `Credit` are what the document
#: was raised for. A partly-settled invoice has both, and the difference is
#: what has been paid.
#: THE JOINS ARE WHAT MAKE A ROW PAYABLE.
#:
#: `JDT1` alone says how much is outstanding, which is the ledger's question.
#: Paying an advance against a bill needs two more facts, and neither is in the
#: journal:
#:
#:   `doc_entry`  — `JDT1.SourceID` IS the source document's DocEntry (verified:
#:                  SourceID 495 -> OPCH DocEntry 495, DocNum 724104114). That
#:                  is the key an A/P Down Payment or a part payment has to be
#:                  linked to. `BaseRef` is only the DocNum, which is not unique
#:                  across companies and is not what the Service Layer wants.
#:
#:   `party_ref`  — the number printed on the VENDOR'S bill (`NumAtCard`), which
#:                  is what the person paying it is holding and searching for.
#:                  Our own DocNum means nothing to them.
#:
#: One LEFT JOIN per document type, each on a primary key and each guarded by
#: `TransType`, so a row only ever matches its own table. Payments carry
#: `CounterRef` (cheque/UTR) instead of `NumAtCard`; a manual journal (type 30)
#: has neither and correctly comes back blank.
_OPEN_DOC_SQL = '''
    SELECT
        T0."TransId"                     AS "trans_id",
        T0."Line_ID"                     AS "line_id",
        T0."TransType"                   AS "doc_type_code",
        T0."SourceID"                    AS "doc_entry",
        T0."BaseRef"                     AS "doc_num",
        T0."Ref1"                        AS "ref1",
        T0."Ref2"                        AS "ref2",
        T0."RefDate"                     AS "posting_date",
        T0."DueDate"                     AS "due_date",
        T0."TaxDate"                     AS "document_date",
        T0."Debit"                       AS "debit",
        T0."Credit"                      AS "credit",
        T0."BalDueDeb"                   AS "open_debit",
        T0."BalDueCred"                  AS "open_credit",
        T0."LineMemo"                    AS "remarks",
        T0."FCCurrency"                  AS "fc_currency",
        DAYS_BETWEEN(T0."DueDate", CURRENT_DATE) AS "days_overdue",
        COALESCE(P18."NumAtCard", P13."NumAtCard",
                 P19."NumAtCard", P14."NumAtCard",
                 P46."CounterRef", P24."CounterRef") AS "party_ref",
        COALESCE(P18."DocStatus", P13."DocStatus",
                 P19."DocStatus", P14."DocStatus")   AS "doc_status"
    FROM "{schema}"."JDT1" T0
    LEFT JOIN "{schema}"."OPCH" P18
           ON T0."TransType" = 18 AND P18."DocEntry" = T0."SourceID"
    LEFT JOIN "{schema}"."OINV" P13
           ON T0."TransType" = 13 AND P13."DocEntry" = T0."SourceID"
    LEFT JOIN "{schema}"."ORPC" P19
           ON T0."TransType" = 19 AND P19."DocEntry" = T0."SourceID"
    LEFT JOIN "{schema}"."ORIN" P14
           ON T0."TransType" = 14 AND P14."DocEntry" = T0."SourceID"
    LEFT JOIN "{schema}"."OVPM" P46
           ON T0."TransType" = 46 AND P46."DocEntry" = T0."SourceID"
    LEFT JOIN "{schema}"."ORCT" P24
           ON T0."TransType" = 24 AND P24."DocEntry" = T0."SourceID"
    WHERE T0."ShortName" = ?
      AND (T0."BalDueDeb" <> 0 OR T0."BalDueCred" <> 0)
      {bills_only}
    ORDER BY T0."DueDate", T0."TransId"
    LIMIT {limit}
'''

#: The document types that are a BILL you can pay against — invoices and the
#: credit memos that offset them. `?bills_only=1` drops payments on account and
#: journals, which are open items but are not things anyone raises an advance
#: against.
BILL_TYPES = (13, 14, 18, 19)

#: The invoices alone — what "except bills" takes out of the ledger.
INVOICE_TYPES = (13, 18)

#: `JDT1.TransType` -> what a human calls it. Only the seven that occur against
#: a partner are named; anything else falls back to `Type <n>` rather than
#: being labelled wrongly.
DOC_TYPES = {
    13: 'A/R Invoice',
    14: 'A/R Credit Memo',
    18: 'A/P Invoice',
    19: 'A/P Credit Memo',
    24: 'Incoming Payment',
    30: 'Journal Entry',
    46: 'Outgoing Payment',
    203: 'A/R Down Payment',
    204: 'A/P Down Payment',
    -2: 'Opening Balance',
}

#: `OCRD.CardType` -> the word the API uses.
_CARD_TYPE = {'S': 'vendor', 'C': 'customer', 'L': 'lead'}

_PARTNER_ONE_SQL = '''
    SELECT
        "CardCode" AS "card_code",
        "CardName" AS "card_name",
        "CardType" AS "card_type",
        "Currency" AS "currency",
        "Balance"  AS "balance",
        IFNULL("frozenFor", 'N') AS "frozen",
        IFNULL("validFor",  'Y') AS "valid"
    FROM "{schema}"."OCRD"
    WHERE "CardCode" = ?
'''


def partner(company, card_code):
    """One business partner, or None.

    Its own lookup so the caller can tell an unknown card code from a partner
    with nothing open — both would otherwise be an empty list, and only one of
    them is a typo.
    """
    code = _s(card_code)
    if not code:
        return None
    schema = _schema(company)
    sql = _PARTNER_ONE_SQL.format(schema=schema)
    rows = _run(company, sql, [code], 'business partner')
    if not rows:
        return None
    r = rows[0]
    return {
        'card_code': _s(r.get('card_code')),
        'card_name': _s(r.get('card_name')),
        'party_type': _CARD_TYPE.get(_s(r.get('card_type')), 'unknown'),
        'currency': _s(r.get('currency')),
        'balance': _money(r.get('balance')),
        'frozen': _s(r.get('frozen')) == 'Y',
        'active': _s(r.get('valid')) == 'Y',
    }


def open_documents(company, card_code, limit=None, bills_only=False,
                   exclude_bills=False):
    """Every open document against one partner, oldest due first.

    Returns `(rows, summary)`.

    `bills_only` narrows to invoices and credit memos — the things an advance
    is actually raised against. Payments on account and journals are genuine
    open items and stay in the default view, because leaving them out would
    make the summary disagree with the partner's balance in SAP.

    SIGNS. Each row carries positive `total_amount` and `open_amount` plus a
    `direction` of DEBIT or CREDIT, rather than one signed number. Mixing the
    two directions into a single signed column is how a payment screen ends up
    proposing to pay a credit note.

    The summary's `net_open` IS signed, and from the partner's normal side:
    positive means the usual thing — we owe a vendor, a customer owes us.
    Negative means the partner is in advance, which is exactly the case this
    module exists for and must not be hidden.
    """
    code = _s(card_code)
    if not code:
        return [], {}
    schema = _schema(company)
    sql = _OPEN_DOC_SQL.format(
        schema=schema,
        limit=_limit(limit),
        # Built from the int tuple above, never from input — nothing the caller
        # sends reaches this string.
        bills_only=(
            f'AND T0."TransType" IN ({",".join(str(t) for t in BILL_TYPES)})'
            if bills_only else
            # "All": everything but the invoices themselves. Credit memos stay —
            # they are not bills, and hiding one hides money owed back.
            f'AND T0."TransType" NOT IN ({",".join(str(t) for t in INVOICE_TYPES)})'
            if exclude_bills else ''),
    )

    rows = []
    open_dr = Decimal('0')
    open_cr = Decimal('0')
    for r in _run(company, sql, [code], 'open documents'):
        dr = Decimal(str(r.get('open_debit') or 0))
        cr = Decimal(str(r.get('open_credit') or 0))
        open_dr += dr
        open_cr += cr

        debit = Decimal(str(r.get('debit') or 0))
        credit = Decimal(str(r.get('credit') or 0))
        is_debit = dr != 0
        code_num = int(r['doc_type_code']) if r.get('doc_type_code') is not None else None
        # `BaseRef` is the document number for a document; a manual journal has
        # none, so fall back to Ref1 and then to the JE id — a row with no
        # identifier at all is not actionable.
        doc_num = _s(r.get('doc_num')) or _s(r.get('ref1')) or str(r.get('trans_id'))

        rows.append({
            'trans_id': int(r['trans_id']),
            'line_id': int(r['line_id']) if r.get('line_id') is not None else None,
            'doc_type_code': code_num,
            'doc_type': DOC_TYPES.get(code_num, f'Type {code_num}'),
            'is_bill': code_num in BILL_TYPES,
            # The key to post a payment against. Null on a journal, which has
            # no source document.
            'doc_entry': int(r['doc_entry']) if r.get('doc_entry') else None,
            'doc_num': doc_num,
            # The number on the VENDOR'S bill (NumAtCard), or the cheque/UTR on
            # a payment. What the person paying it is actually holding.
            'party_ref': _s(r.get('party_ref')),
            'doc_status': _s(r.get('doc_status')),
            'ref2': _s(r.get('ref2')),
            'document_date': _date(r.get('document_date')),
            'posting_date': _date(r.get('posting_date')),
            'due_date': _date(r.get('due_date')),
            'direction': 'DEBIT' if is_debit else 'CREDIT',
            'total_amount': _money(debit if is_debit else credit),
            'open_amount': _money(dr if is_debit else cr),
            'settled_amount': _money(
                (debit - dr) if is_debit else (credit - cr)),
            # Negative simply means not yet due; the caller decides whether to
            # show it as "due in 12 days" or to hide it.
            'days_overdue': int(r['days_overdue']) if r.get('days_overdue') is not None else None,
            'currency': _s(r.get('fc_currency')),
            'remarks': _s(r.get('remarks')),
        })

    info = partner(company, code) or {}
    party_type = info.get('party_type')
    net = (open_cr - open_dr) if party_type == 'vendor' else (open_dr - open_cr)
    summary = {
        'open_count': len(rows),
        'open_debit': _money(open_dr),
        'open_credit': _money(open_cr),
        'net_open': _money(net),
        'net_open_means': (
            'positive = we owe this vendor' if party_type == 'vendor'
            else 'positive = this customer owes us'),
        'overdue_count': sum(
            1 for r in rows if (r['days_overdue'] or 0) > 0),
    }
    return rows, summary


# ---------------------------------------------------------------------------
# "All": every open document against a vendor EXCEPT its bills and POs
# ---------------------------------------------------------------------------
#: Three sources, because no one table holds them:
#:
#:   JDT1  the partner's open ledger items that are not bills — credit memos,
#:         payments on account, journal entries. `open_documents` already reads
#:         these (with the joins that make a row payable); here it is asked to
#:         leave the invoices out.
#:   OPDN  goods receipts not yet invoiced (373 open in Oil). They never touch
#:         the partner's account — SAP posts a GRPO to the allocation account —
#:         so they are NOT in JDT1, and a ledger-only "All" would miss what is
#:         usually the biggest thing a vendor is waiting to be paid for.
#:   ORPD  goods returns not yet credited. The other direction: they reduce
#:         what is owed.
#:
#: POs are not in any of the three, which is the exclusion asked for. Bills
#: (A/R and A/P invoices, types 13 and 18) are filtered out of the JDT1 part.
#: Credit memos stay: a credit memo is not a bill, and leaving it out would
#: hide money the vendor already owes back.
#:
#: Open amount follows the same rule as a PO: `DocTotal - PaidToDate`, both
#: tax-inclusive. For the open GRPOs sampled it is the whole document — none is
#: part-invoiced — and the rule still holds when one is.
_OTHER_DOC_TYPES = {20: 'Goods Receipt PO', 21: 'Goods Return'}

_OPEN_RECEIPT_SQL = '''
    SELECT
        {code}                 AS "doc_type_code",
        T0."DocEntry"          AS "doc_entry",
        T0."DocNum"            AS "doc_num",
        T0."NumAtCard"         AS "party_ref",
        T0."DocStatus"         AS "doc_status",
        T0."TaxDate"           AS "document_date",
        T0."DocDate"           AS "posting_date",
        T0."DocDueDate"        AS "due_date",
        T0."DocCur"            AS "currency",
        T0."DocTotal"          AS "doc_total",
        IFNULL(T0."PaidToDate", 0) AS "settled",
        T0."DocTotal" - IFNULL(T0."PaidToDate", 0) AS "open_amount",
        T0."Comments"          AS "remarks",
        DAYS_BETWEEN(T0."DocDueDate", CURRENT_DATE) AS "days_overdue"
    FROM "{schema}"."{table}" T0
    WHERE T0."CardCode" = ?
      AND T0."DocStatus" = 'O'
      AND IFNULL(T0."CANCELED", 'N') = 'N'
      AND T0."DocTotal" - IFNULL(T0."PaidToDate", 0) > 0
    ORDER BY T0."DocDueDate", T0."DocEntry"
    LIMIT {limit}
'''

#: (object type, table, direction). A receipt is owed TO the vendor (credit,
#: like the bill it will become); a return is owed BY them (debit).
_RECEIPT_SOURCES = ((20, 'OPDN', 'CREDIT'), (21, 'ORPD', 'DEBIT'))


def other_documents(company, card_code, limit=None):
    """Every open document against one vendor except its bills and POs.

    Returns `(rows, summary)` in the same row shape as `open_documents`, so a
    caller maps one shape for Bill and for All. Oldest due first.
    """
    code = _s(card_code)
    if not code:
        return [], {}
    schema = _schema(company)
    cap = _limit(limit)

    ledger, _ = open_documents(company, code, limit=cap, exclude_bills=True)
    rows = list(ledger)

    for type_code, table, direction in _RECEIPT_SOURCES:
        sql = _OPEN_RECEIPT_SQL.format(
            schema=schema, table=table, code=type_code, limit=cap)
        for r in _run(company, sql, [code], f'open {table}'):
            total = Decimal(str(r.get('doc_total') or 0))
            settled = Decimal(str(r.get('settled') or 0))
            rows.append({
                # Not a journal line, so there is no TransId to give.
                'trans_id': None,
                'line_id': None,
                'doc_type_code': type_code,
                'doc_type': _OTHER_DOC_TYPES[type_code],
                'is_bill': False,
                'doc_entry': int(r['doc_entry']),
                'doc_num': str(r.get('doc_num') or r.get('doc_entry')),
                'party_ref': _s(r.get('party_ref')),
                'doc_status': _s(r.get('doc_status')),
                'ref2': '',
                'document_date': _date(r.get('document_date')),
                'posting_date': _date(r.get('posting_date')),
                'due_date': _date(r.get('due_date')),
                'direction': direction,
                'total_amount': _money(total),
                'open_amount': _money(r.get('open_amount')),
                'settled_amount': _money(settled),
                'days_overdue': (int(r['days_overdue'])
                                 if r.get('days_overdue') is not None else None),
                'currency': _s(r.get('currency')),
                'remarks': _s(r.get('remarks')),
            })

    rows.sort(key=lambda r: (r['due_date'] or '9999-12-31', r['doc_num']))
    rows = rows[:cap]

    owed_to = sum(Decimal(r['open_amount']) for r in rows if r['direction'] == 'CREDIT')
    owed_by = sum(Decimal(r['open_amount']) for r in rows if r['direction'] == 'DEBIT')
    summary = {
        'open_count': len(rows),
        'open_credit': _money(owed_to),
        'open_debit': _money(owed_by),
        # From the vendor's side: positive means we owe them.
        'net_open': _money(owed_to - owed_by),
        'net_open_means': 'positive = we owe this vendor',
        'by_type': {
            label: sum(1 for r in rows if r['doc_type'] == label)
            for label in sorted({r['doc_type'] for r in rows})
        },
    }
    return rows, summary


# ---------------------------------------------------------------------------
# The payee's bank accounts, as SAP holds them
# ---------------------------------------------------------------------------
#: A business partner's bank accounts are in OCRB, one row per account, and
#: the DEFAULT is the one OCRD names in `DflAccount`. In Oil 1,804 of 2,257
#: vendors have a default and only three have more than one account, so the
#: default is nearly always the whole answer, but it is not always the first
#: row: ORGV000136's default is its second account (PNB), not its first.
#:
#: IFSC is stored in `SwiftNum`. There is no IFSC column in SAP B1; this
#: installation keeps it in the SWIFT field, as `HDFC0000271`, and OCRD's
#: `DflSwift` holds the default account's.
#:
#: Values are CLEANED before they leave. Rows imported from a spreadsheet carry
#: a leading apostrophe, Excel's "keep this as text" marker, into SAP:
#: `'466705000041`, `'KKBK0004121`. An account number with a quote in front of
#: it fails the payout form's digit check and would fail the bank's too.
_BANK_ACCOUNTS_SQL = '''
    SELECT
        B."AbsEntry"     AS "id",
        B."BankCode"     AS "bank_code",
        D."BankName"     AS "bank_name",
        B."Account"      AS "account_number",
        B."SwiftNum"     AS "ifsc",
        B."Branch"       AS "branch",
        B."AcctName"     AS "account_name"
    FROM "{schema}"."OCRB" B
    LEFT JOIN "{schema}"."ODSC" D ON D."AbsEntry" = B."BankKey"
    WHERE B."CardCode" = ?
    ORDER BY B."AbsEntry"
'''

_BANK_DEFAULT_SQL = '''
    SELECT
        C."DflAccount" AS "account_number",
        C."DflSwift"   AS "ifsc",
        C."BankCode"   AS "bank_code",
        D."BankName"   AS "bank_name",
        C."DflBranch"  AS "branch",
        C."CardName"   AS "card_name"
    FROM "{schema}"."OCRD" C
    LEFT JOIN "{schema}"."ODSC" D
           ON D."BankCode" = C."BankCode" AND D."CountryCod" = 'IN'
    WHERE C."CardCode" = ?
'''

#: The shape every Indian IFSC has: 4 letters, a zero, 6 letters or digits.
_IFSC = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')


def _clean_bank_value(value):
    """Strip whitespace and the spreadsheet apostrophe from an account or IFSC."""
    return _s(value).strip().lstrip("'").strip()


def partner_bank_accounts(company, card_code):
    """Every bank account SAP holds for a partner, the default first.

    Returns `(rows, default_account_number)`. Each row:
    `{id, bank_code, bank_name, account_number, ifsc, ifsc_valid, branch,
      account_name, is_default}`.

    A default that has no matching OCRB row, which happens when the default was
    typed on the partner and never added as an account, is still returned, as
    its own row, because it is the account SAP will actually use.
    """
    code = _s(card_code)
    if not code:
        return [], ''
    schema = _schema(company)
    raw = _run(company, _BANK_ACCOUNTS_SQL.format(schema=schema), [code],
               'partner bank accounts')
    dflt_rows = _run(company, _BANK_DEFAULT_SQL.format(schema=schema), [code],
                     'partner default bank account')
    dflt = dflt_rows[0] if dflt_rows else {}
    default_number = _clean_bank_value(dflt.get('account_number'))

    rows = []
    for r in raw:
        number = _clean_bank_value(r.get('account_number'))
        if not number:
            continue
        ifsc = _clean_bank_value(r.get('ifsc')).upper()
        rows.append({
            'id': int(r['id']) if r.get('id') is not None else None,
            'bank_code': _s(r.get('bank_code')),
            'bank_name': _s(r.get('bank_name')),
            'account_number': number,
            'ifsc': ifsc,
            'ifsc_valid': bool(_IFSC.match(ifsc)),
            'branch': _s(r.get('branch')),
            'account_name': _s(r.get('account_name')),
            'is_default': number == default_number,
        })

    if default_number and not any(r['is_default'] for r in rows):
        ifsc = _clean_bank_value(dflt.get('ifsc')).upper()
        rows.append({
            'id': None,
            'bank_code': _s(dflt.get('bank_code')),
            'bank_name': _s(dflt.get('bank_name')),
            'account_number': default_number,
            'ifsc': ifsc,
            'ifsc_valid': bool(_IFSC.match(ifsc)),
            'branch': _s(dflt.get('branch')),
            'account_name': _s(dflt.get('card_name')),
            'is_default': True,
        })

    rows.sort(key=lambda r: (not r['is_default'], r['id'] or 0))
    return rows, default_number


# ---------------------------------------------------------------------------
# The company's own accounts: bank G/Ls and cash, for "pay FROM"
# ---------------------------------------------------------------------------
#: The parents of the accounts a payment can go out of: `1104100 BANK
#: ACCOUNTS` (current accounts) and `2201100 BANK CASH CREDIT LOAN` (the CC /
#: OD accounts, which is where Oil's main Indian Bank and ICICI accounts sit).
#: The same two in all three companies. Settings-overridable, comma-separated.
#:
#: Deliberately left out: 1104200 PAYMENT BANK ONLINE (Paytm, Razorpay:
#: collection gateways, nothing is paid out of them), FDRs, and term loans.
BANK_PARENTS = [
    code.strip() for code in str(getattr(
        settings, 'ADVANCE_PAYMENT_BANK_GL_PARENTS', '1104100,2201100')).split(',')
    if code.strip()
]

#: Why from the chart of accounts, not the house-bank table (DSC1): DSC1 holds
#: only the accounts someone set up as house banks. That is 5 of Oil's 13 bank
#: G/Ls, 1 of Mart's 17 and 3 of Beverages', so most of the company's real
#: bank accounts never showed. Every account a payment goes out of IS a G/L
#: under one of the parents above, and SAP posts an outgoing transfer to that
#: G/L, so the G/L is the thing to choose. DSC1 is joined only to add what it
#: knows (IFSC, branch, the account number as the bank writes it).
_COMPANY_BANKS_SQL = '''
    SELECT
        A."AcctCode"                  AS "gl_account",
        A."AcctName"                  AS "gl_name",
        A."FatherNum"                 AS "parent",
        IFNULL(B."BankCode", '')      AS "bank_code",
        IFNULL(T."BankName", '')      AS "bank_name",
        IFNULL(B."Account", '')       AS "account_number",
        IFNULL(B."Branch", '')        AS "branch",
        IFNULL(B."SwiftNum", '')      AS "ifsc"
    FROM "{schema}"."OACT" A
    LEFT JOIN "{schema}"."DSC1" B ON B."GLAccount" = A."AcctCode"
    LEFT JOIN "{schema}"."ODSC" T ON T."BankCode" = B."BankCode"
    WHERE A."FatherNum" IN ({parents})
      AND A."Postable" = 'Y'
      AND IFNULL(A."FrozenFor", 'N') = 'N'
    ORDER BY A."FatherNum", A."AcctCode"
'''

#: House banks with no G/L set. Mart's ICICI 629305042079 is one: its G/L
#: (1104108 "ICICI BANK 629305042079") exists, the DSC1 row just is not linked
#: to it. Matched to its G/L by the account number in the G/L's name.
_UNLINKED_HOUSE_BANKS_SQL = '''
    SELECT
        B."BankCode"                  AS "bank_code",
        IFNULL(T."BankName", '')      AS "bank_name",
        IFNULL(B."Account", '')       AS "account_number",
        IFNULL(B."Branch", '')        AS "branch",
        IFNULL(B."SwiftNum", '')      AS "ifsc"
    FROM "{schema}"."DSC1" B
    LEFT JOIN "{schema}"."ODSC" T ON T."BankCode" = B."BankCode"
    WHERE IFNULL(B."GLAccount", '') = ''
'''

#: The account number inside a G/L name: "HDFC BANK LTD. 11272320000316",
#: "HSBC BANK A/C- 166-794941-511 (USD)", "INDIAN OVERSEAS BANK A/c - 0060".
_NUMBER_IN_NAME = re.compile(r'\d[\d-]{2,}\d')


def _number_in_name(name):
    found = _NUMBER_IN_NAME.findall(name or '')
    return max(found, key=len) if found else ''


def _digits(value):
    return re.sub(r'\D', '', value or '')


def house_banks(company):
    """Every bank account `company` can pay out of, one per G/L.

    `key` is the G/L account code: unique in the company, and what SAP posts
    the payment to. `house_bank` says whether SAP also has it set up as a
    house bank (and so knows its IFSC); it does not stop the account being
    used.
    """
    schema = _schema(company)
    if not BANK_PARENTS:
        return []
    sql = _COMPANY_BANKS_SQL.format(
        schema=schema, parents=', '.join('?' for _ in BANK_PARENTS))
    rows = _run(company, sql, list(BANK_PARENTS), 'bank accounts')
    unlinked = _run(company, _UNLINKED_HOUSE_BANKS_SQL.format(schema=schema), [],
                    'house banks')

    out, seen = [], set()
    for r in rows:
        gl = _s(r.get('gl_account'))
        if gl in seen:  # two DSC1 rows on one G/L: the first describes it
            continue
        seen.add(gl)
        name = _s(r.get('gl_name'))
        bank = r
        if not _s(r.get('bank_code')):
            # Not linked from DSC1 by G/L; try an unlinked house bank whose
            # account number is the one in this G/L's name.
            in_name = _digits(_number_in_name(name))
            bank = next((u for u in unlinked
                         if in_name and _digits(_clean_bank_value(u.get('account_number'))) == in_name),
                        r)
        number = _clean_bank_value(bank.get('account_number')) or _number_in_name(name)
        out.append({
            'key': gl,
            'gl_account': gl,
            'gl_name': name,
            'bank_code': _s(bank.get('bank_code')),
            'bank_name': _s(bank.get('bank_name')),
            'account_number': number,
            'branch': _s(bank.get('branch')),
            'ifsc': _clean_bank_value(bank.get('ifsc')).upper(),
            'house_bank': bool(_s(bank.get('bank_code'))),
        })
    return out


#: Parent of the company's cash accounts: `1105000 CASH IN HAND` in all three
#: companies, each with the same three postable children (1105001 CASH SALE,
#: 1105002 CASH IN HAND, 1105003 CASH SALE MAYAPURI). Settings-overridable for
#: the same reason the employee-advance parent is.
#:
#: By PARENT, not by name. Matching "CASH" in the account name would also pick
#: up 1111002 GST CASH BALANCE ON PORTAL and 5640005 PROMOTIONAL EX CASHBACK,
#: neither of which anyone can pay out of.
CASH_PARENT = str(getattr(settings, 'ADVANCE_PAYMENT_CASH_GL_PARENT', '1105000')).strip()

_CASH_ACCOUNTS_SQL = '''
    SELECT
        "AcctCode"  AS "acct_code",
        "AcctName"  AS "acct_name",
        "CurrTotal" AS "balance"
    FROM "{schema}"."OACT"
    WHERE "FatherNum" = ?
      AND "Postable" = 'Y'
      AND IFNULL("FrozenFor", 'N') = 'N'
    ORDER BY "AcctCode"
'''


def cash_accounts(company):
    """The company's cash G/L accounts, from the chart of accounts."""
    schema = _schema(company)
    return [{
        'acct_code': _s(r.get('acct_code')),
        'acct_name': _s(r.get('acct_name')),
        'balance': _money(r.get('balance')),
    } for r in _run(company, _CASH_ACCOUNTS_SQL.format(schema=schema), [CASH_PARENT],
                    'cash accounts')]


# ---------------------------------------------------------------------------
# Employees, from the chart of accounts
# ---------------------------------------------------------------------------
#: Employees come from the GL, not from OHEM.
#:
#: OHEM — SAP's HR master — holds 17 rows in Oil. The advances actually paid
#: live as 335 postable accounts under `1113000 EMPLOYEES ADVANCES`, one per
#: person, named `<NAME> ADVANCE <CODE>`. The GL is where the money is, so the
#: GL is the list; and the account is what an advance has to be posted to
#: anyway, so returning it saves a second lookup later.
_EMPLOYEE_SQL = '''
    SELECT
        "AcctCode"  AS "acct_code",
        "AcctName"  AS "acct_name",
        "CurrTotal" AS "balance"
    FROM "{schema}"."OACT"
    WHERE "FatherNum" = ?
      AND "Postable" = 'Y'
      AND IFNULL("FrozenFor", 'N') = 'N'
      {search}
    ORDER BY "AcctName"
    LIMIT {limit}
'''


def _split_employee(acct_name):
    """`('RAVINDER SINGH SHUNTY', 'JWPL0035')` from the account name.

    Either half may come back empty. The raw account name is returned alongside
    by the caller, so a naming convention this does not recognise degrades to
    "no parsed code" rather than to a wrong one.
    """
    name = _s(acct_name)
    if _ADVANCE_MARKER in name:
        head, _, tail = name.rpartition(_ADVANCE_MARKER)
        return head.strip(), tail.strip()
    # No marker: treat the last token as a code only if it actually looks like
    # one. Guessing here would attach a real person's payroll code to the wrong
    # account.
    head, _, tail = name.rpartition(' ')
    if tail and _EMP_CODE.match(tail.upper()):
        return head.strip(), tail.strip()
    return name, ''


_EMPLOYEE_NAMES_SQL = '''
    SELECT "AcctName" AS "acct_name"
    FROM "{schema}"."OACT"
    WHERE "FatherNum" = ?
      AND "Postable" = 'Y'
      AND IFNULL("FrozenFor", 'N') = 'N'
'''


def employee_advance_codes(company):
    """Every employee code that HAS an advance account in `company`, upper-case.

    All of them, not a page: it answers "is this person in SAP?", which a
    capped list would answer wrongly for whoever fell off the end. (Sep 2026:
    Oil 328 accounts, Mart 146, Beverages 111.) An account whose name carries
    no recognisable code contributes nothing.
    """
    schema = _schema(company)
    codes = set()
    for r in _run(company, _EMPLOYEE_NAMES_SQL.format(schema=schema),
                  [EMPLOYEE_ADVANCE_PARENT], 'employee advance accounts'):
        _name, code = _split_employee(r.get('acct_name'))
        if code:
            codes.add(code.upper())
    return codes


def employees(company, search=None, limit=None):
    """Employee advance accounts, as `{employee_code, employee_name, acct_*}`."""
    schema = _schema(company)
    pattern = _like(search)
    sql = _EMPLOYEE_SQL.format(
        schema=schema,
        search=(f'AND (UPPER("AcctName") LIKE ?{_ESC} '
                f'OR "AcctCode" LIKE ?{_ESC})') if pattern else '',
        limit=_limit(limit),
    )
    params = [EMPLOYEE_ADVANCE_PARENT] + ([pattern, pattern] if pattern else [])

    out = []
    for r in _run(company, sql, params, 'employee advance accounts'):
        acct_name = _s(r.get('acct_name'))
        name, code = _split_employee(acct_name)
        out.append({
            'employee_code': code,
            'employee_name': name,
            'acct_code': _s(r.get('acct_code')),
            'acct_name': acct_name,
            'balance': _money(r.get('balance')),
        })
    return out


# ---------------------------------------------------------------------------
# What posting the outgoing payment needs to know
# ---------------------------------------------------------------------------

#: A document table per request-document kind, for its branch.
BRANCH_TABLES = {'BILL': 'OPCH', 'PO': 'OPOR'}

_DOCUMENT_BRANCHES_SQL = '''
    SELECT "DocEntry" AS "doc_entry", "DocNum" AS "doc_num", "BPLId" AS "bpl_id",
           "DocStatus" AS "doc_status", "CANCELED" AS "cancelled"
    FROM "{schema}"."{table}"
    WHERE "DocEntry" IN ({marks})
'''


def document_branches(company, kind, doc_entries):
    """`{doc_entry: {doc_num, bpl_id, open}}` for bills or POs.

    SAP refuses a payment whose branch differs from the documents it pays, so
    the payment inherits theirs. `open` is False for a closed or cancelled
    document, which can no longer be paid.
    """
    table = BRANCH_TABLES[kind]
    entries = sorted({int(e) for e in doc_entries})
    if not entries:
        return {}
    sql = _DOCUMENT_BRANCHES_SQL.format(
        schema=_schema(company), table=table, marks=', '.join('?' * len(entries)))
    out = {}
    for r in _run(company, sql, entries, f'{table} branches'):
        out[int(r['doc_entry'])] = {
            'doc_num': r.get('doc_num'),
            'bpl_id': r.get('bpl_id'),
            'open': _s(r.get('doc_status')) == 'O' and _s(r.get('cancelled')) != 'Y',
        }
    return out


#: SAP's object code for an outgoing payment (OVPM).
OUTGOING_PAYMENT_OBJECT = '46'

_SERIES_SQL = '''
    SELECT TOP 1 "Series" AS "series"
    FROM "{schema}"."NNM1"
    WHERE "ObjectCode" = ? AND "SeriesName" = ?
      AND IFNULL("Locked", 'N') = 'N' AND IFNULL("IsManual", 'N') = 'N'
'''


def outgoing_payment_series(company, posting_date):
    """The numbering series for an outgoing payment posted on `posting_date`.

    One series per calendar month, named `OP<MM><YY>` (Oil 2026-27: OP0426 is
    2596 … OP0327 is 2607). None when SAP has none open for that month; the
    caller refuses to post rather than let SAP pick the wrong one.
    """
    name = f'OP{posting_date:%m}{posting_date:%y}'
    rows = _run(company, _SERIES_SQL.format(schema=_schema(company)),
                [OUTGOING_PAYMENT_OBJECT, name], 'outgoing payment series')
    return int(rows[0]['series']) if rows else None


_PAYMENT_BY_MEMO_SQL = '''
    SELECT "DocEntry" AS "doc_entry", "DocNum" AS "doc_num"
    FROM "{schema}"."OVPM"
    WHERE "JrnlMemo" = ? AND "Canceled" = 'N'
    ORDER BY "DocEntry" DESC
'''


def outgoing_payment_by_memo(company, memo):
    """The live outgoing payment whose journal memo is `memo`, or None.

    Every voucher OMS posts carries a memo unique to that request and version
    (`OMS AP-2026-0001/2`). Checked before each post, so a post whose answer
    was lost (a timeout after SAP committed) is found rather than made twice.
    """
    rows = _run(company, _PAYMENT_BY_MEMO_SQL.format(schema=_schema(company)),
                [memo], 'outgoing payments')
    if not rows:
        return None
    return {'doc_entry': int(rows[0]['doc_entry']), 'doc_num': rows[0].get('doc_num')}


def employee_advance_account(company, employee_code):
    """The advance G/L of one employee, by code: `{acct_code, acct_name}` or None."""
    code = (employee_code or '').strip().upper()
    if not code:
        return None
    for row in employees(company, search=code, limit=MAX_LIMIT):
        if (row['employee_code'] or '').upper() == code:
            return {'acct_code': row['acct_code'], 'acct_name': row['acct_name']}
    return None
