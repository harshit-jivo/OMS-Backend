"""Fast SAP business-partner lookup for the invoice entry form.

Reads the business-partner master (OCRD) directly from HANA — NOT via the
Service Layer, which is slower — reusing the shared HANAConnection. Includes
BOTH vendors/suppliers (CardType 'S') AND customers (CardType 'C'), so the
entry form's searchable dropdown can pick either. Cached for a few minutes so
the dropdown stays snappy.

GSTIN is taken from OCRD."LicTradNo" (the Federal Tax ID field, which holds the
GSTIN in the India localization); if that is blank we fall back to any non-empty
CRD1."GSTRegnNo" for the party.
"""
from django.conf import settings
from django.core.cache import cache

from hana.services.connection import HANAConnection

# v2: cache key bumped so the supplier-only cache is discarded and customers appear.
VENDOR_CACHE_KEY = 'tracker_sap_vendors_v2'
VENDOR_CACHE_TTL = 600  # seconds (10 min)

# A tracker invoice is a *purchase* document, so it lands in the A/P tables:
#   OPCH — A/P Invoice        (SAP object type 18)
#   ORPC — A/P Credit Memo    (SAP object type 19)
# It is identified by the vendor's own invoice number, which SAP stores in
# NumAtCard ("Vendor Ref. No."), scoped to the vendor (CardCode). NumAtCard is
# NOT unique on its own — the same "21" appears against many vendors — so the
# vendor code is part of the key, and even then a vendor may reuse a number
# across years, hence a list of candidates rather than a single row.
AP_TABLES = (('OPCH', 18, 'AP_INVOICE'), ('ORPC', 19, 'AP_CREDIT_MEMO'))


def _vendor_query():
    schema = settings.DATABASES['hana']['OIL_SCHEMA']
    return f'''
        SELECT
            T0."CardCode"   AS "card_code",
            T0."CardName"   AS "card_name",
            T0."CardType"   AS "card_type",
            T0."LicTradNum" AS "lic_trad_no",
            T0."State1"     AS "state",
            G."gst_regn_no"
        FROM "{schema}"."OCRD" AS T0
        LEFT JOIN (
            SELECT "CardCode", MAX("GSTRegnNo") AS "gst_regn_no"
            FROM "{schema}"."CRD1"
            WHERE "GSTRegnNo" IS NOT NULL AND "GSTRegnNo" <> ''
            GROUP BY "CardCode"
        ) AS G ON G."CardCode" = T0."CardCode"
        WHERE T0."CardType" IN ('S', 'C')
        ORDER BY T0."CardName"
    '''


def fetch_vendors(force_refresh=False):
    """List of vendors: [{card_code, card_name, gstin, state}]. Cached."""
    if not force_refresh:
        cached = cache.get(VENDOR_CACHE_KEY)
        if cached is not None:
            return cached

    with HANAConnection() as conn:
        rows = conn.execute(_vendor_query())

    vendors = [{
        'card_code': (r.get('card_code') or '').strip(),
        'card_name': (r.get('card_name') or '').strip(),
        # 'S' = vendor/supplier, 'C' = customer.
        'card_type': (r.get('card_type') or '').strip(),
        'gstin': ((r.get('lic_trad_no') or '') or (r.get('gst_regn_no') or '')).strip(),
        'state': (r.get('state') or '').strip(),
    } for r in rows]

    cache.set(VENDOR_CACHE_KEY, vendors, VENDOR_CACHE_TTL)
    return vendors


# ---------------------------------------------------------------------------
# Tracker invoice  ->  SAP A/P document (OPCH / ORPC)
# ---------------------------------------------------------------------------
def schema_for_invoice(invoice):
    """Company DB schema a tracker invoice belongs to.

    Branch wins over unit: 'Mart' is its own company DB, while Wellness
    invoices are split between the Oil and Beverage companies by unit.
    """
    branch = (getattr(invoice.branch, 'name', '') or '').upper()
    unit = (getattr(invoice.unit, 'name', '') or '').upper()
    if 'MART' in branch or 'MART' in unit:
        return getattr(settings, 'HANA_MART_COMPANY_DB', '')
    if unit.startswith('BEVERAGE'):
        return settings.HANA_BEVERAGE_COMPANY_DB
    return settings.HANA_OIL_COMPANY_DB


def find_sap_documents(invoice_number, party_code='', schema=None, *, require_party=True):
    """Candidate SAP A/P documents for a vendor invoice number.

    The key is TRIM(NumAtCard) + CardCode. NumAtCard alone is not a key —
    '21' matches five unrelated vendors' documents in Oil — so with
    `require_party` (the default) a blank party_code yields [] rather than a
    confident wrong answer. Pass require_party=False only for a diagnostic
    "what else carries this number?" search. Returns newest-first:
        [{schema, table, object_type, doc_type, docentry, docnum,
          num_at_card, card_code, card_name, doc_date, doc_total, canceled}]
    Best-effort — a HANA error yields [] rather than breaking the caller.
    """
    number = (invoice_number or '').strip()
    code = (party_code or '').strip()
    if not number or (require_party and not code):
        return []
    schema = schema or settings.HANA_OIL_COMPANY_DB
    if not schema:
        return []

    num = number.replace("'", "''")
    card_filter = ''
    if code:
        card_filter = ' AND "CardCode" = \'{}\''.format(code.replace("'", "''"))

    rows = []
    try:
        with HANAConnection() as conn:
            for table, object_type, doc_type in AP_TABLES:
                sql = (
                    f'SELECT "DocEntry", "DocNum", "NumAtCard", "CardCode", "CardName",'
                    f'       "DocDate", "DocTotal", "CANCELED", "DocStatus"'
                    f' FROM "{schema}"."{table}"'
                    f' WHERE TRIM("NumAtCard") = \'{num}\'{card_filter}'
                    f' ORDER BY "DocEntry" DESC'
                )
                for r in conn.execute(sql):
                    rows.append({
                        'schema': schema,
                        'table': table,
                        'object_type': object_type,
                        'doc_type': doc_type,
                        'docentry': r.get('DocEntry'),
                        'docnum': r.get('DocNum'),
                        'num_at_card': (r.get('NumAtCard') or '').strip(),
                        'card_code': (r.get('CardCode') or '').strip(),
                        'card_name': (r.get('CardName') or '').strip(),
                        'doc_date': r.get('DocDate'),
                        'doc_total': r.get('DocTotal'),
                        'canceled': (r.get('CANCELED') or 'N').strip(),
                    })
    except Exception:  # noqa: BLE001
        return []
    return rows


def find_draft_documents(invoice_number, party_code='', schema=None):
    """Candidate SAP *draft* documents (ODRF) for a vendor invoice number.

    Drafts matter because JSAP's budget approval happens on the draft, before
    it posts: bud.jsDocEntry.docEntry is an ODRF.DocEntry, not an OPCH one.
    ODRF holds every document type in one table, so it is filtered to the A/P
    object types. Same key as the posted lookup: TRIM(NumAtCard) + CardCode.
    """
    number = (invoice_number or '').strip()
    code = (party_code or '').strip()
    if not number or not code:
        return []
    schema = schema or settings.HANA_OIL_COMPANY_DB
    if not schema:
        return []

    obj_types = ", ".join("'{}'".format(o) for _, o, _ in AP_TABLES)
    sql = (
        f'SELECT "DocEntry", "DocNum", "ObjType", "NumAtCard", "CardCode", "CardName",'
        f'       "DocDate", "DocTotal", "CANCELED"'
        f' FROM "{schema}"."ODRF"'
        f' WHERE TRIM("NumAtCard") = \'{number.replace(chr(39), chr(39) * 2)}\''
        f'   AND "CardCode" = \'{code.replace(chr(39), chr(39) * 2)}\''
        f'   AND "ObjType" IN ({obj_types})'
        f' ORDER BY "DocEntry" DESC'
    )
    try:
        with HANAConnection() as conn:
            rows = conn.execute(sql)
    except Exception:  # noqa: BLE001
        return []
    return [{
        'schema': schema,
        'table': 'ODRF',
        'object_type': int(r.get('ObjType') or 0),
        'docentry': r.get('DocEntry'),
        'docnum': r.get('DocNum'),
        'num_at_card': (r.get('NumAtCard') or '').strip(),
        'card_code': (r.get('CardCode') or '').strip(),
        'card_name': (r.get('CardName') or '').strip(),
        'doc_date': r.get('DocDate'),
        'doc_total': r.get('DocTotal'),
        'canceled': (r.get('CANCELED') or 'N').strip(),
    } for r in rows]


# ---------------------------------------------------------------------------
# Batched "is it already posted in SAP?" lookup
# ---------------------------------------------------------------------------
# `find_sap_documents` opens its own HANA connection per call, which is right
# for the one-invoice question the detail screen asks and unusable for the
# whole-queue question the SAP-saved sweep asks: 800 invoices would be 1,600
# round trips. This groups the work by company schema and issues one statement
# per (schema, table) instead — five or six in total.
POSTED_BATCH_SIZE = 400


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _quoted(values):
    return ', '.join("'" + v.replace("'", "''") + "'" for v in sorted(set(values)))


def posted_documents_for(invoices):
    """{invoice.id: document dict} for invoices already POSTED in SAP.

    "Posted" deliberately means OPCH / ORPC only — a draft (ODRF) is not saved,
    it is pending, and treating one as saved would march an invoice to Payment
    on the strength of a document nobody has committed yet. Cancelled documents
    are ignored for the same reason: a cancelled A/P invoice is not a saved one.

    Matching is case-insensitive on the invoice number. That is not a
    loosening — `Invoice`'s own uniqueness constraint is `Lower(...)`, so the
    tracker already considers 'ats:394' and 'ATS:394' the same number, and a
    case-sensitive probe here would disagree with the application's own idea of
    identity. The vendor code is still part of the key: `NumAtCard` alone is
    not unique across vendors.

    Each invoice is searched ONLY in the company schema its branch/unit selects
    (`schema_for_invoice`). Documents posted in a different company are
    reported by `cross_company_documents_for` rather than acted on, because a
    mismatch there means the tracker row or the posting is wrong — which is a
    thing for a person to look at, not for a sweep to resolve by advancing it.

    Best-effort: a HANA failure yields {} so a scheduled sweep degrades to
    doing nothing rather than raising.
    """
    return _posted_lookup(invoices, cross_company=False)


def cross_company_documents_for(invoices):
    """{invoice.id: document dict} for invoices posted in the WRONG company.

    Diagnostic only — never used to advance an invoice. See
    `posted_documents_for` for why.
    """
    return _posted_lookup(invoices, cross_company=True)


def _all_schemas():
    names = [settings.HANA_OIL_COMPANY_DB,
             settings.HANA_BEVERAGE_COMPANY_DB,
             getattr(settings, 'HANA_MART_COMPANY_DB', '')]
    return [n for n in names if n]


def _posted_lookup(invoices, cross_company):
    invoices = [i for i in invoices if (i.party_code or '').strip()
                and (i.invoice_number or '').strip()]
    if not invoices:
        return {}

    wanted = {}                       # schema -> {(NUMBER, CODE): [invoice, ...]}
    for inv in invoices:
        schema = schema_for_invoice(inv)
        if not schema:
            continue
        key = (inv.invoice_number.strip().upper(), inv.party_code.strip().upper())
        wanted.setdefault(schema, {}).setdefault(key, []).append(inv)

    if not wanted:
        return {}

    search_in = {}                    # schema -> {keys to look for there}
    if cross_company:
        every = _all_schemas()
        for own, keys in wanted.items():
            for other in every:
                if other == own:
                    continue
                bucket = search_in.setdefault(other, {})
                for key, invs in keys.items():
                    # extend, never overwrite: two companies can legitimately
                    # contribute the same (number, vendor) key, and assigning
                    # would drop one company's invoices from the search.
                    bucket.setdefault(key, []).extend(invs)
    else:
        search_in = wanted

    found = {}
    try:
        with HANAConnection() as conn:
            for schema, keys in search_in.items():
                for row in _rows_for_schema(conn, schema, list(keys)):
                    key = (row['num_at_card'].upper(), row['card_code'].upper())
                    for inv in keys.get(key, ()):
                        # Newest document wins if a vendor reused the number.
                        current = found.get(inv.id)
                        if current is None or (row['docentry'] or 0) > (current['docentry'] or 0):
                            found[inv.id] = row
    except Exception:  # noqa: BLE001
        return {}
    return found


def _rows_for_schema(conn, schema, keys):
    """Posted, non-cancelled A/P documents in `schema` matching any of `keys`."""
    out = []
    for batch in _chunks(keys, POSTED_BATCH_SIZE):
        numbers = _quoted(n for n, _c in batch)
        codes = _quoted(c for _n, c in batch)
        for table, object_type, doc_type in AP_TABLES:
            sql = (
                f'SELECT "DocEntry", "DocNum", TRIM("NumAtCard") AS "NumAtCard",'
                f'       "CardCode", "CardName", "DocDate", "DocTotal", "DocStatus"'
                f' FROM "{schema}"."{table}"'
                f' WHERE UPPER(TRIM("NumAtCard")) IN ({numbers})'
                f'   AND UPPER("CardCode") IN ({codes})'
                f"   AND IFNULL(\"CANCELED\", 'N') = 'N'"
            )
            for r in conn.execute(sql):
                out.append({
                    'schema': schema,
                    'table': table,
                    'object_type': object_type,
                    'doc_type': doc_type,
                    'docentry': r.get('DocEntry'),
                    'docnum': r.get('DocNum'),
                    'num_at_card': (r.get('NumAtCard') or '').strip(),
                    'card_code': (r.get('CardCode') or '').strip(),
                    'card_name': (r.get('CardName') or '').strip(),
                    'doc_date': r.get('DocDate'),
                    'doc_total': r.get('DocTotal'),
                })
    return out


def resolve_draft_document(invoice):
    """The SAP draft (ODRF) row for a tracker invoice, or None.

    Only the invoice's own company is searched — the draft DocEntry is fed to
    JSAP, whose docEntry values collide across companies, so a cross-company
    guess here would attach the wrong approval status.
    """
    hits = find_draft_documents(invoice.invoice_number, invoice.party_code,
                                schema_for_invoice(invoice))
    live = [h for h in hits if h['canceled'] != 'Y']
    return (live or hits or [None])[0]


def resolve_sap_document(invoice):
    """The single SAP A/P document for a tracker invoice, or None.

    Searches the invoice's own company DB first; if nothing matches there the
    other companies are tried, since the unit/branch on the tracker row is not
    always the company the document was booked in. A cancelled document is
    only returned when it is the sole candidate. When several live candidates
    remain (a vendor reusing a number) the newest DocEntry wins and the rest
    are available via find_sap_documents().
    """
    own = schema_for_invoice(invoice)
    others = [s for s in (settings.HANA_OIL_COMPANY_DB,
                          settings.HANA_BEVERAGE_COMPANY_DB,
                          getattr(settings, 'HANA_MART_COMPANY_DB', ''))
              if s and s != own]
    for schema in [own] + others:
        hits = find_sap_documents(invoice.invoice_number, invoice.party_code, schema)
        if not hits:
            continue
        live = [h for h in hits if h['canceled'] != 'Y']
        return (live or hits)[0]
    return None
