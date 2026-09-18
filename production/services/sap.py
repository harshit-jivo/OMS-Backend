"""Reading production orders from SAP, and writing the decision back.

TWO DIRECTIONS, ONE FILE
------------------------
    read   OWOR (+ OITM, OUSR)          -> what the planner raised
    write  OMS_PRDO_APPROVAL            -> whether SAP may release it

Both are direct HANA, never the Service Layer. OMS creates no document here,
so there is nothing for SAP's document validation to do; and `tracker/sap.py`
already reads OPCH/ORPC the same way.

THE SCHEMA IS THE ONE INTERPOLATED THING
-----------------------------------------
HANA cannot bind an identifier, so the schema name is formatted into the SQL —
and it must therefore come from `Queries._schema_for_branch()`, which resolves
against settings and refuses anything else. It is NEVER taken from a request.
Every value binds with `?`.

WHY A TABLE AND NOT A PROCEDURE
--------------------------------
BKDT calls `OPEN_BKDT` because SAP ships that procedure. There is no SAP
procedure for "this production order is approved" — JSAP invented a table and
hand-wrote a lookup into `SBO_SP_TRANSACTIONNOTIFICATION`. OMS does the same
thing, but into a table it owns, with a real INTEGER DocEntry instead of
JSAP's NVARCHAR one. See `docs/Approvals/PRDO_DESIGN.md` §7 and
`docs/Approvals/prdo_test_wiring.sql`.
"""
import json
import logging

from django.conf import settings
from django.utils import timezone

from hana.services.connection import HANAConnection, HanaSchemaError, Queries

from production.models import SapWriteStatus

logger = logging.getLogger(__name__)


class SapUnavailable(Exception):
    """HANA could not be reached, or the query failed.

    Exists because the alternative — returning an empty list — makes "SAP has
    no planned orders" and "we never managed to ask SAP" the same answer. That
    is precisely how the JSAP job came to report success for 33 days while
    writing nothing. The sync treats this as a hard failure.
    """


class SapWriteError(Exception):
    """The approval could not be written back. The approval itself stands."""


#: The OMS-owned table SAP's release gate reads. Created per company schema;
#: see `docs/Approvals/prdo_test_wiring.sql`.
APPROVAL_TABLE = 'OMS_PRDO_APPROVAL'

#: Only Standard orders are approved. JSAP never touched Special or
#: Disassembly (702 of the 812 it missed), and SAP's own gate checks
#: `A."Type" = 'S'` — approving what the gate ignores would be theatre.
#:
#: Columns every company has.
BASE_COLUMNS = [
    ('A', 'DocEntry', 'doc_entry'),
    ('A', 'DocNum', 'doc_num'),
    ('A', 'ItemCode', 'item_code'),
    ('A', 'Status', 'status'),
    ('A', 'Type', 'type'),
    ('A', 'PlannedQty', 'planned_qty'),
    ('A', 'Warehouse', 'warehouse'),
    ('A', 'PostDate', 'post_date'),
    ('A', 'DueDate', 'due_date'),
    ('A', 'StartDate', 'start_date'),
    ('A', 'Comments', 'comments'),
    ('A', 'UserSign', 'user_sign'),
    ('O', 'ItemName', 'item_name'),
    ('O', 'U_Sub_Group', 'item_group'),
    ('O', 'Series', 'item_series'),
    ('O', 'SalFactor2', 'sal_factor2'),
    ('O', 'SalPackUn', 'sal_pack_un'),
    ('U', 'U_NAME', 'created_by'),
]

#: Columns that exist in SOME company databases only.
#:
#: `U_BATCH_NO` / `U_MFG` / `U_EXP_DATE` are user-defined fields on OIL's OWOR
#: and are simply ABSENT from Beverages and Mart — selecting them there is a
#: hard `invalid column name`, not a null. The whole point of per-company
#: databases is that they can differ, so the SELECT is built from what each
#: schema actually has rather than from what OIL happens to have.
OPTIONAL_COLUMNS = [
    ('A', 'U_BATCH_NO', 'batch_no'),
    ('A', 'U_MFG', 'mfg_date'),
    ('A', 'U_EXP_DATE', 'expiry_date'),
]

#: schema -> set of OWOR column names. Read once per process; a company
#: database does not gain user-defined fields while the job is running.
_OWOR_COLUMNS = {}


def _owor_columns(conn, schema):
    if schema not in _OWOR_COLUMNS:
        rows = conn.execute(
            'SELECT COLUMN_NAME AS "name" FROM SYS.TABLE_COLUMNS '
            'WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?', [schema, 'OWOR'])
        _OWOR_COLUMNS[schema] = {r['name'] for r in rows}
    return _OWOR_COLUMNS[schema]


def _planned_order_sql(schema, available):
    columns = list(BASE_COLUMNS) + [
        c for c in OPTIONAL_COLUMNS if c[1] in available
    ]
    selected = ',\n        '.join(
        f'{alias}."{col}" AS "{out}"' for alias, col, out in columns)
    return f'''
    SELECT
        {selected}
    FROM "{schema}"."OWOR" A
    INNER JOIN "{schema}"."OITM" O ON O."ItemCode" = A."ItemCode"
    LEFT  JOIN "{schema}"."OUSR" U ON U."USERID"   = A."UserSign"
    WHERE A."Status" = 'P' AND A."Type" = 'S'
    ORDER BY A."DocEntry"
'''


#: Used by the reconcile pass: what is SAP's status for orders OMS still has
#: open? Chunked by the caller — HANA has a parameter limit and the pending
#: set is small anyway.
STATUS_SQL = '''
    SELECT "DocEntry" AS "doc_entry", "Status" AS "status"
    FROM "{schema}"."OWOR"
    WHERE "DocEntry" IN ({placeholders})
'''


def _schema(company):
    try:
        return Queries._schema_for_branch(company)
    except HanaSchemaError as exc:
        raise SapUnavailable(
            f'SAP is not configured for {company}. Ask an administrator to '
            f'check the HANA settings.') from exc


def _as_date(value):
    return value.date() if hasattr(value, 'date') else value


def planned_orders(company):
    """Every Planned, Standard production order in `company`, as dicts.

    Raises `SapUnavailable` rather than returning `[]` when the read fails —
    the whole point. An empty list from here means SAP genuinely has nothing
    planned.
    """
    schema = _schema(company)
    try:
        with HANAConnection() as conn:
            available = _owor_columns(conn, schema)
            rows = conn.execute(_planned_order_sql(schema, available))
    except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
        logger.warning('PRDO: reading planned orders for %s failed: %s',
                       company, exc, exc_info=True)
        raise SapUnavailable(f'Could not read production orders for {company}: '
                             f'{type(exc).__name__}: {exc}') from exc

    out = []
    for r in rows:
        out.append({
            'sap_doc_entry': int(r['doc_entry']),
            'sap_doc_num': int(r['doc_num']) if r.get('doc_num') is not None else None,
            'item_code': (r.get('item_code') or '').strip(),
            'sap_status': (r.get('status') or 'P').strip(),
            'order_type': (r.get('type') or 'S').strip(),
            'planned_qty': r.get('planned_qty'),
            'warehouse': (r.get('warehouse') or '').strip(),
            'post_date': _as_date(r.get('post_date')),
            'due_date': _as_date(r.get('due_date')),
            'start_date': _as_date(r.get('start_date')),
            'remarks': (r.get('comments') or '').strip(),
            'sap_user_sign': int(r['user_sign']) if r.get('user_sign') is not None else None,
            'batch_no': (r.get('batch_no') or '').strip(),
            'mfg_date': _as_date(r.get('mfg_date')),
            'expiry_date': _as_date(r.get('expiry_date')),
            'item_name': (r.get('item_name') or '').strip(),
            'item_group': (r.get('item_group') or '').strip(),
            'item_series': int(r['item_series']) if r.get('item_series') is not None else None,
            'sal_factor2': r.get('sal_factor2'),
            'sal_pack_un': r.get('sal_pack_un'),
            'sap_created_by': (r.get('created_by') or '').strip(),
        })
    return out


def statuses_for(company, doc_entries):
    """`{DocEntry: Status}` for the given orders. Used by the reconcile pass."""
    doc_entries = [int(d) for d in doc_entries]
    if not doc_entries:
        return {}

    schema = _schema(company)
    found = {}
    CHUNK = 500
    try:
        with HANAConnection() as conn:
            for i in range(0, len(doc_entries), CHUNK):
                chunk = doc_entries[i:i + CHUNK]
                sql = STATUS_SQL.format(
                    schema=schema,
                    placeholders=','.join('?' for _ in chunk))
                for row in conn.execute(sql, chunk):
                    found[int(row['doc_entry'])] = (row.get('status') or '').strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning('PRDO: status refresh for %s failed: %s',
                       company, exc, exc_info=True)
        raise SapUnavailable(f'Could not read order statuses for {company}: '
                             f'{type(exc).__name__}: {exc}') from exc
    return found


#: OMS decision -> the `Status` SAP's gate reads.
#:
#: The gate only tests `<> 'A'`, so a rejection could have been left as no row
#: at all and SAP would behave identically. It is written anyway, because
#: "no row" was otherwise doing two jobs — *rejected* and *nobody has looked
#: at it* — and anyone reading this table from inside SAP could not tell them
#: apart. That ambiguity is cheap to remove now and expensive to diagnose in
#: two years.
DECISION_APPROVED = 'A'
DECISION_REJECTED = 'R'


def write_approval(flow):
    """Record an approval in SAP. The order becomes releasable."""
    return _write_decision(flow, DECISION_APPROVED)


def write_rejection(flow):
    """Record a rejection in SAP. The order stays blocked, now explicitly.

    A failed write here is far less serious than a failed approval: SAP treats
    a missing row as "not approved" anyway, so the order remains blocked
    either way and only the DOCUMENTATION is lost. A failed approval, by
    contrast, leaves an order blocked that should be shipping.
    """
    return _write_decision(flow, DECISION_REJECTED)


def _write_decision(flow, status):
    """UPSERT one decision into the company's `OMS_PRDO_APPROVAL`.

    An UPSERT, not an INSERT: a retry after a failure, or a re-decision after
    an administrator cleared a row, must converge rather than collide on the
    primary key. JSAP's equivalent procedure inserted blindly, which is part
    of why its author never ran it.

    On failure the OMS decision is NOT undone — it is already committed. What
    SAP said is recorded and the caller reports it; `retry-sap/` exists for the
    second attempt.
    """
    order = flow.production_order
    schema = _schema(order.company)
    table = f'"{schema}"."{APPROVAL_TABLE}"'
    now = timezone.now()
    actor = _last_actor_name(order)
    word = 'approval' if status == DECISION_APPROVED else 'rejection'

    # `DecidedBy` / `DecidedAt`, not `ApprovedBy` / `ApprovedAt`: this table
    # records a decision either way, and a rejected row reading
    # "ApprovedBy: preshit" would say the opposite of what happened.
    params = [
        order.sap_doc_entry,      # DocEntry
        status,                   # Status  — 'A' or 'R'
        order.company,            # Company (traceability only)
        order.pk,                 # RequestId
        actor,                    # DecidedBy
        now,                      # DecidedAt
        now,                      # SyncedAt
    ]
    payload = {
        'schema': schema,
        'table': APPROVAL_TABLE,
        'values': {
            'DocEntry': order.sap_doc_entry, 'Status': status,
            'Company': order.company, 'RequestId': order.pk,
            'DecidedBy': actor, 'DecidedAt': now.isoformat(),
            'SyncedAt': now.isoformat(),
        },
    }

    sql = (f'UPSERT {table} '
           f'("DocEntry","Status","Company","RequestId","DecidedBy",'
           f'"DecidedAt","SyncedAt") VALUES (?,?,?,?,?,?,?) '
           f'WITH PRIMARY KEY')

    try:
        with HANAConnection() as conn:
            conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.warning('PRDO: %s write-back failed for flow=%s: %s',
                       word, flow.pk, exc, exc_info=True)
        flow.sap_payload = payload
        flow.sap_status = SapWriteStatus.FAILED
        flow.sap_status_text = json.dumps(
            {'status': 'FAILED', 'response': f'{type(exc).__name__}: {exc}'},
            indent=2)
        flow.save(update_fields=['sap_payload', 'sap_status',
                                 'sap_status_text', 'updated_at'])
        raise SapWriteError(
            f'SAP did not accept the {word} for production order '
            f'{order.sap_doc_num or order.sap_doc_entry}. The {word} stands; '
            f'an administrator can retry the SAP write. The exact SAP response '
            f'is recorded against the request.') from exc

    flow.sap_payload = payload
    flow.sap_status = SapWriteStatus.SUCCESS
    flow.sap_status_text = json.dumps({
        'status': 'SUCCESS',
        'response': (f'{APPROVAL_TABLE} upserted: DocEntry '
                     f'{order.sap_doc_entry} = {status} in {schema}.'),
    }, indent=2)
    flow.save(update_fields=['sap_payload', 'sap_status', 'sap_status_text',
                             'updated_at'])
    return flow.sap_status_text


def _last_actor_name(order):
    """Whoever made the final decision, for SAP's audit column.

    APPROVE or REJECT, whichever came last — the column has to name the person
    behind the `Status` beside it. Text and truncated: the SAP column is
    NVARCHAR(100) and holds a label, not a key. The authoritative record of
    who decided, when and why is `production_order_action_logs`.
    """
    from production.models import LogAction

    row = (order.action_logs
           .filter(action__in=[LogAction.APPROVE, LogAction.REJECT])
           .select_related('acted_by')
           .order_by('-acted_at', '-id')
           .first())
    if not row or not row.acted_by:
        return 'oms'
    return (getattr(row.acted_by, 'username', '') or 'oms')[:100]
