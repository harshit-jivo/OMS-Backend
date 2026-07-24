"""
Write IRN-generation data into the HANA OMS_IRN_LOG table — same column shape as
the SAP add-on's @UTL_MDEXTH (Marketing Extension Header).

One row per IRN attempt: success ('S') or failure ('F'); a cancellation updates
the row in place. DocEntry is an auto-increment identity, so we never supply it.
Everything here is best-effort — a failure is logged and never breaks the IRN flow.

Gated by settings.EINV_MIRROR_HANA (the caller checks it). Targets the company DB
schema (settings.HANA_COMPANY_DB) unless a schema is passed.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)

TABLE = "OMS_IRN_LOG"
OBJECT = "OMS_IRN"        # marker so OMS rows are distinguishable from the add-on's
CREATOR = "OMS"
# NIC document type -> SAP object type (as stored in U_UTL_DocType).
_DOCTYPE = {"INV": "13", "CRN": "14", "DBN": "14"}


def _schema(schema):
    return schema or getattr(settings, "HANA_COMPANY_DB", "")


def _int_or_none(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _insert(schema, cols: dict):
    from hana.services.connection import HANAConnection

    keys = list(cols.keys())
    collist = ", ".join(f'"{k}"' for k in keys)
    placeholders = ", ".join(["?"] * len(keys))
    sql = f'INSERT INTO "{schema}"."{TABLE}" ({collist}) VALUES ({placeholders})'
    with HANAConnection() as conn:
        conn.execute(sql, [cols[k] for k in keys])


def write_success(record, *, docentry=None, qr_path=None, schema=None) -> bool:
    """Insert an 'S' (success) row from a generated IrnRecord."""
    schema = _schema(schema)
    if not schema:
        logger.warning("OMS_IRN_LOG skipped: no schema/company DB configured")
        return False
    now = datetime.now()
    cols = {
        "DocNum": _int_or_none(record.doc_no),
        "Object": OBJECT,
        "Canceled": "N",
        "Status": "O",
        "CreateDate": now,
        "CreateTime": now.hour * 100 + now.minute,
        "Creator": CREATOR,
        "U_UTL_QRPT": qr_path,
        "U_UTL_QRST": record.signed_qr_code,
        "U_UTL_IRN": record.irn,
        "U_UTL_IST": "S",
        "U_UTL_IRNGENDT": now,
        "U_UTL_GenrTime": now.strftime("%H:%M:%S"),
        "U_UTL_DocType": _DOCTYPE.get((record.doc_type or "").upper()),
        "U_UTL_BaseEntry": str(docentry) if docentry is not None else None,
        "U_UTL_AckNo": record.ack_no or None,
    }
    try:
        _insert(schema, cols)
        logger.info("OMS_IRN_LOG: S row for IRN %s (doc %s)", (record.irn or "")[:12], record.doc_no)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("OMS_IRN_LOG: failed to write S row for doc %s", record.doc_no)
        return False


def write_failure(*, docentry=None, doc_no=None, doc_type=None, error="", schema=None) -> bool:
    """Insert an 'F' (failed) row for an attempt that did not produce an IRN."""
    schema = _schema(schema)
    if not schema:
        return False
    now = datetime.now()
    cols = {
        "DocNum": _int_or_none(doc_no),
        "Object": OBJECT,
        "Canceled": "N",
        "Status": "O",
        "CreateDate": now,
        "CreateTime": now.hour * 100 + now.minute,
        "Creator": CREATOR,
        "U_UTL_IST": "F",
        "U_UTL_RMK": (error or "")[:100000],
        "U_UTL_IRNGENDT": now,
        "U_UTL_GenrTime": now.strftime("%H:%M:%S"),
        "U_UTL_DocType": _DOCTYPE.get((doc_type or "").upper()) if doc_type else None,
        "U_UTL_BaseEntry": str(docentry) if docentry is not None else None,
    }
    try:
        _insert(schema, cols)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("OMS_IRN_LOG: failed to write F row for doc %s", doc_no)
        return False


def irn_status_by_docentry(docentries, *, schema=None) -> dict:
    """Look up existing IRNs for a set of SAP DocEntries across BOTH HANA tables.

    Checks the SAP add-on's @UTL_MDEXTH first, then OMS's OMS_IRN_LOG (fallback),
    so an invoice whose IRN was generated in SAP is recognised even though it was
    never written to the OMS Django table. Returns:
        { docentry(int): {"irn": str, "ack_no": str|None, "source": "@UTL_MDEXTH"|"OMS_IRN_LOG"} }
    Only DocEntries that actually have a successful, non-cancelled IRN appear.
    Best-effort: any error returns {} (the caller falls back to the Django table).
    """
    from hana.services.connection import HANAConnection

    schema = _schema(schema)
    ids = [str(int(d)) for d in (docentries or []) if _int_or_none(d) is not None]
    if not schema or not ids:
        return {}
    in_list = ",".join(ids)

    def _latest(table, extra_where):
        sql = (
            f'SELECT "de", "irn", "ack" FROM ('
            f'  SELECT "U_UTL_BaseEntry" AS "de", "U_UTL_IRN" AS "irn", "U_UTL_AckNo" AS "ack",'
            f'         ROW_NUMBER() OVER (PARTITION BY "U_UTL_BaseEntry"'
            f'                            ORDER BY "U_UTL_IRNGENDT" DESC) AS "rn"'
            f'  FROM "{schema}"."{table}"'
            f'  WHERE "U_UTL_DocType" = 13 AND "U_UTL_IST" = \'S\''
            f'    AND IFNULL("U_UTL_IRN", \'\') <> \'\' {extra_where}'
            f'    AND "U_UTL_BaseEntry" IN ({in_list})'
            f') WHERE "rn" = 1'
        )
        with HANAConnection() as conn:
            return conn.execute(sql)

    result = {}
    try:
        # OMS_IRN_LOG first, then @UTL_MDEXTH overrides -> SAP add-on wins on ties.
        for row in _latest("OMS_IRN_LOG", "AND IFNULL(\"Canceled\", 'N') <> 'Y'"):
            de = _int_or_none(row.get("de"))
            if de is not None:
                result[de] = {"irn": row.get("irn"), "ack_no": row.get("ack"),
                              "source": "OMS_IRN_LOG"}
        for row in _latest("@UTL_MDEXTH", ""):
            de = _int_or_none(row.get("de"))
            if de is not None:
                result[de] = {"irn": row.get("irn"), "ack_no": row.get("ack"),
                              "source": "@UTL_MDEXTH"}
    except Exception:  # noqa: BLE001
        logger.exception("irn_status_by_docentry: HANA lookup failed")
        return {}
    return result


def mark_cancelled(irn, *, schema=None) -> bool:
    """Stamp the row for `irn` as cancelled (Canceled='Y' + cancel date/time)."""
    from hana.services.connection import HANAConnection

    schema = _schema(schema)
    if not schema or not irn:
        return False
    now = datetime.now()
    sql = (f'UPDATE "{schema}"."{TABLE}" SET "Canceled" = ?, "UpdateDate" = ?, '
           f'"U_UTL_CANDT" = ?, "U_UTL_CancelTime" = ? WHERE "U_UTL_IRN" = ?')
    try:
        with HANAConnection() as conn:
            conn.execute(sql, ["Y", now, now, now.strftime("%H:%M:%S"), irn])
        logger.info("OMS_IRN_LOG: marked cancelled for IRN %s", (irn or "")[:12])
        return True
    except Exception:  # noqa: BLE001
        logger.exception("OMS_IRN_LOG: failed to mark cancelled for IRN %s", irn)
        return False
