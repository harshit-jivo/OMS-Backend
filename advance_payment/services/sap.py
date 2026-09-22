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
#: `open_amount` is the part still to be delivered, summed from the LINES: a PO
#: half received is not one to advance the full value against. It comes from
#: POR1 rather than a header column because SAP keeps no header total of the
#: open portion.
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
        T0."PaidToDate" AS "paid_to_date",
        T0."Comments"   AS "remarks",
        IFNULL((SELECT SUM(L."OpenSum")
                  FROM "{schema}"."POR1" L
                 WHERE L."DocEntry" = T0."DocEntry"
                   AND L."LineStatus" = 'O'), 0) AS "open_amount"
    FROM "{schema}"."OPOR" T0
    WHERE T0."DocStatus" = 'O'
      AND IFNULL(T0."CANCELED", 'N') = 'N'
      {card}
      {search}
    ORDER BY T0."DocDate" DESC, T0."DocEntry" DESC
    LIMIT {limit}
'''


def open_purchase_orders(company, card_code=None, search=None, limit=None):
    """Purchase orders still open, newest first.

    `card_code` narrows to one vendor, which is the normal call: an advance is
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
    return [{
        'doc_entry': int(r['doc_entry']),
        'doc_num': int(r['doc_num']) if r.get('doc_num') is not None else None,
        'card_code': _s(r.get('card_code')),
        'card_name': _s(r.get('card_name')),
        'vendor_ref': _s(r.get('vendor_ref')),
        'doc_date': _date(r.get('doc_date')),
        'due_date': _date(r.get('due_date')),
        'currency': _s(r.get('currency')),
        'doc_total': _money(r.get('doc_total')),
        'paid_to_date': _money(r.get('paid_to_date')),
        'open_amount': _money(r.get('open_amount')),
        'remarks': _s(r.get('remarks')),
    } for r in _run(company, sql, params, 'open purchase orders')]


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
    return [{
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
