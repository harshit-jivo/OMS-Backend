"""Read-only budget-approval status from JSAP (SQL Server, `jsaplive3`).

JSAP approves a SAP *draft* document against a budget before it is posted, so
everything here is keyed on an ODRF DocEntry — never an OPCH one:

    tracker Invoice
      -> ODRF   (TRIM(NumAtCard) + CardCode, in the invoice's company DB)
           |  ODRF.DocEntry
           v
    bud.jsDocEntry         one row per (draft, approval template); status A/P/R
           |  id
           v
    bud.jsBudgetStatusWorkflow   per-action log; `description` holds the
                                 approver's reason on a rejection

`bud.jsDocEntry` carries no company column, and draft DocEntry sequences run
per company, so a lookup is always cross-checked against `bud.jsBudgetTable`
(which does carry Branch) before its status is trusted.

Note the live tables live in the `bud` schema — the `dbo` copies are a frozen
archive that stops in March 2025.

Nothing here writes: JSAP owns its own approvals. Every call is best-effort;
an unreachable database yields "unknown" rather than an exception, so the
tracker keeps working when JSAP is down.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# jsDocEntry.status / jsBudgetStatusWorkflow.status
STATUS_APPROVED = 'A'
STATUS_PENDING = 'P'
STATUS_REJECTED = 'R'
STATUS_LABELS = {
    STATUS_APPROVED: 'Approved',
    STATUS_PENDING: 'Pending',
    STATUS_REJECTED: 'Rejected',
}

# jsBudgetTable.Branch -> company DB. Mart is deliberately absent: it has no
# Branch value in JSAP, so Mart invoices are never budget-approved there.
_BRANCH_ALIASES = {'OIL': 'OIL', 'BEVERAGE': 'BEVERAGE', 'BEVERAGES': 'BEVERAGE'}


def is_configured():
    return bool(settings.JSAP_DB_HOST and settings.JSAP_DB_NAME)


def branch_for_schema(schema):
    """JSAP Branch for a company DB, or None when JSAP does not cover it."""
    if not schema:
        return None
    if schema == settings.HANA_OIL_COMPANY_DB:
        return 'OIL'
    if schema == settings.HANA_BEVERAGE_COMPANY_DB:
        return 'BEVERAGE'
    return None      # Mart — not present in jsBudgetTable


def _connect():
    import pymssql

    return pymssql.connect(
        server=settings.JSAP_DB_HOST,
        port=settings.JSAP_DB_PORT,
        user=settings.JSAP_DB_USER,
        password=settings.JSAP_DB_PASSWORD,
        database=settings.JSAP_DB_NAME,
        login_timeout=10,
        timeout=30,
    )


def _query(sql, params=()):
    with _connect() as conn:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql, params)
        return cur.fetchall()


def status_for_draft(docentry, branch=None):
    """Budget-approval status of one SAP draft.

    Returns None when the draft is unknown to JSAP (never submitted, or JSAP
    is unreachable). Otherwise:
        {status, label, doc_id, doc_entry, branch, updated_on,
         description, decided_on, decided_by}
    `description` / `decided_*` come from the newest workflow action, which is
    where a rejecting approver's reason is recorded.

    When `branch` is given, a row is only accepted if jsBudgetTable agrees the
    draft belongs to that branch — draft DocEntry values repeat across
    companies, so without this an OIL draft could pick up a BEVERAGE approval.
    """
    if not is_configured() or not docentry:
        return None
    branch = _BRANCH_ALIASES.get((branch or '').upper()) if branch else None

    sql = """
        SELECT TOP 1 d.id, d.docEntry, d.status, d.updatedOn, d.createdOn
        FROM bud.jsDocEntry d
        WHERE d.docEntry = %s
    """
    params = [int(docentry)]
    if branch:
        sql += """
          AND EXISTS (SELECT 1 FROM bud.jsBudgetTable b
                      WHERE b.DocEntry = d.docEntry AND b.Branch = %s)
        """
        params.append(branch)
    sql += " ORDER BY d.id DESC"

    try:
        rows = _query(sql, tuple(params))
    except Exception:  # noqa: BLE001
        logger.exception('JSAP: status lookup failed for draft %s', docentry)
        return None
    if not rows:
        return None

    row = rows[0]
    status = (row.get('status') or '').strip().upper()
    out = {
        'doc_id': row.get('id'),
        'doc_entry': row.get('docEntry'),
        'branch': branch,
        'status': status,
        'label': STATUS_LABELS.get(status, status or 'Unknown'),
        'updated_on': row.get('updatedOn'),
        'created_on': row.get('createdOn'),
        'description': '',
        'decided_on': None,
        'decided_by': None,
    }

    try:
        acts = _query("""
            SELECT TOP 1 status, description, createdOn, userId
            FROM bud.jsBudgetStatusWorkflow
            WHERE docId = %s ORDER BY createdOn DESC
        """, (row['id'],))
    except Exception:  # noqa: BLE001
        logger.exception('JSAP: workflow lookup failed for docId %s', row['id'])
        acts = []
    if acts:
        out['description'] = (acts[0].get('description') or '').strip()
        out['decided_on'] = acts[0].get('createdOn')
        out['decided_by'] = acts[0].get('userId')
    return out


def status_for_invoice(invoice):
    """Budget-approval status for a tracker invoice, resolved end to end.

    Adds `draft` (the matched ODRF row) so callers can show *which* SAP
    document the status came from, and `reason` when nothing was found —
    the UI needs to tell "JSAP rejected it" apart from "we could not find it
    in SAP at all".
    """
    from . import sap

    schema = sap.schema_for_invoice(invoice)
    branch = branch_for_schema(schema)
    if branch is None:
        return {'available': False, 'reason': 'not_in_jsap',
                'detail': 'Mart is not budget-approved in JSAP.'}
    if not is_configured():
        return {'available': False, 'reason': 'not_configured',
                'detail': 'JSAP database is not configured.'}
    if not (invoice.party_code or '').strip():
        return {'available': False, 'reason': 'no_party_code',
                'detail': 'Pick the SAP vendor on this invoice to link it to SAP.'}

    try:
        drafts = sap.resolve_draft_documents(invoice, strict=True)
    except sap.SapUnavailable as exc:
        # NOT 'no_draft'. The desk is told we could not ask, so it stops
        # reading a failed lookup as "still pending".
        return {'available': False, 'reason': 'sap_unreachable',
                'detail': f'Could not reach SAP: {exc}'}

    if not drafts:
        return {'available': False, 'reason': 'no_draft',
                'detail': 'No matching SAP draft for this invoice number and vendor.'}

    # Try every draft, not just the newest: a re-created draft leaves JSAP's
    # approval attached to the earlier one. Newest-first, so the common case
    # still answers on the first probe.
    for draft in drafts:
        status = status_for_draft(draft['docentry'], branch)
        if status:
            return {'available': True, 'draft': draft, **status}

    return {'available': False, 'reason': 'not_submitted', 'draft': drafts[0],
            'detail': 'The SAP draft exists but has not reached JSAP.'}
