"""
Mirror an OMS-generated IRN into the SAP add-on's UDO table `@UTL_MDEXTH`.

WHY BOTH TABLES
---------------
They feed different printers, and neither is redundant:

    OMS_IRN_LOG    -> the OMS bill print (OMS_SP_GST_INVOICE, which UNIONs both)
    @UTL_MDEXTH    -> every SAP-side Crystal layout (CRYSTAL_AR_INVOICE_ITEMS,
                      CRYSTAL_AR_INVOICE_SERVICES, ...), which read ONLY this one

So an OMS IRN must ALWAYS land in `OMS_IRN_LOG` first — that write is what makes
the invoice printable from OMS at all. This module is the *additional* write, and
it is strictly best-effort: if it fails, OMS printing is unaffected and
`reconcile_udo_mirror` picks the row up later.

Gated by settings.EINV_MIRROR_UDO.

THE TWO THINGS THAT MAKE THIS HARDER THAN OMS_IRN_LOG
-----------------------------------------------------
1. `OMS_IRN_LOG."DocEntry"` is `BY DEFAULT AS IDENTITY` — HANA assigns it, which
   is why `oms_irn_log.py` never supplies one. `@UTL_MDEXTH."DocEntry"` is a
   plain integer primary key with no identity and no default: the SAP add-on
   allocates it as MAX+1 (its sequence is dense, while `ONNM.AutoKey` sits
   hundreds behind — see docs/OMS_IRN_TO_SAP_UDO_TRANSFER.md §10). OMS therefore
   races the add-on for the next key on every concurrent generation. The key is
   allocated INSIDE the INSERT statement and a unique-constraint violation is
   retried, because a MAX read in Python followed by an INSERT widens that race
   to a network round trip.

2. Two 'S' rows for one invoice BREAK PRINTING. The Crystal procs read the QR
   with scalar subqueries keyed on the invoice, so a second row makes them fail
   with SQL error 305 ("single-row query returns more than one row") and the
   bill does not render at all. The `NOT EXISTS` guard below is not an
   optimisation — omitting it takes invoices offline. It matches on
   `U_UTL_BaseEntry` + `U_UTL_DocType`, never `DocNum` (the two tables mean
   different things by `DocNum`; see §4.2 of the doc).
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)

TABLE = "@UTL_MDEXTH"
OBJECT = "UTL_MDEXT_UOM"      # the UDO's registered code in OUDO
CREATOR = "OMS"               # deliberately not a SAP USER_CODE: with no DELETE
                              # privilege this is the only handle on these rows
USER_SIGN = 1                 # SAP user 'manager' in every company

#: NIC document type -> SAP ObjType, and the source table its posting period
#: lives in. A credit note is ObjType 14 and lives in ORIN, not OINV.
_DOCTYPE = {"INV": "13", "CRN": "14", "DBN": "14"}
_PERIOD_SOURCE = {"13": "OINV", "14": "ORIN"}

#: How many times to re-attempt after losing the DocEntry race.
_KEY_RETRIES = 5


def enabled() -> bool:
    return bool(getattr(settings, "EINV_MIRROR_UDO", False))


def _schema(schema):
    return schema or getattr(settings, "HANA_OIL_COMPANY_DB", "")


def _is_duplicate_key(exc) -> bool:
    """True when the failure is a primary-key collision on DocEntry.

    HANAConnection.execute re-raises everything as RuntimeError with the driver
    message inlined, so the error code has to be read out of the text. HANA
    reports a unique-constraint violation as error 301.
    """
    msg = str(exc).lower()
    return "unique constraint violated" in msg or "(301," in msg or "error: (301)" in msg


def write_success(record, *, docentry, qr_path=None, schema=None) -> bool:
    """Insert the 'S' row into `@UTL_MDEXTH`. Returns True only if a row landed.

    Returns False (without raising) when the invoice already has an 'S' row from
    the add-on or from an earlier mirror — that is the normal, correct outcome
    for an invoice SAP has already e-invoiced, not an error.
    """
    from hana.services.connection import HANAConnection

    schema = _schema(schema)
    if not schema:
        logger.warning("UDO mirror skipped: no schema/company DB configured")
        return False
    if docentry is None:
        logger.warning("UDO mirror skipped for doc %s: no SAP DocEntry",
                       getattr(record, "doc_no", None))
        return False

    doctype = _DOCTYPE.get((record.doc_type or "").upper())
    if not doctype:
        logger.warning("UDO mirror skipped for doc %s: unmapped doc type %r",
                       record.doc_no, record.doc_type)
        return False
    src = _PERIOD_SOURCE[doctype]
    base_entry = str(docentry)
    now = datetime.now()

    # One statement: allocate the key, derive Period from the real document, and
    # refuse to create a duplicate. Anything split across statements re-opens the
    # race described in the module docstring.
    sql = f'''
INSERT INTO "{schema}"."{TABLE}"
  ("DocEntry","DocNum","Period","Instance","Series","Handwrtten","Canceled","Object",
   "LogInst","UserSign","Transfered","Status","CreateDate","CreateTime",
   "DataSource","RequestStatus","Creator","NaturalPer","DPPStatus",
   "U_UTL_QRPT","U_UTL_QRST","U_UTL_IRN","U_UTL_IST","U_UTL_IRNGENDT",
   "U_UTL_GenrTime","U_UTL_DocType","U_UTL_BaseEntry","U_UTL_AckNo")
SELECT
  (SELECT IFNULL(MAX("DocEntry"), 0) + 1 FROM "{schema}"."{TABLE}"),
  (SELECT IFNULL(MAX("DocEntry"), 0) + 1 FROM "{schema}"."{TABLE}"),
  D."FinncPriod", 0, -1, 'N', 'N', '{OBJECT}',
  NULL, {USER_SIGN}, 'N', 'O', ?, ?,
  'O', 'W', '{CREATOR}', 'N', 'N',
  ?, ?, ?, 'S', ?,
  ?, ?, ?, ?
FROM "{schema}"."{src}" D
WHERE D."DocEntry" = ?
  AND NOT EXISTS (SELECT 1 FROM "{schema}"."{TABLE}" U
                   WHERE U."U_UTL_BaseEntry" = ?
                     AND U."U_UTL_DocType"  = ?
                     AND U."U_UTL_IST" = 'S')
'''
    params = [
        now, now.hour * 100 + now.minute,                      # CreateDate, CreateTime
        qr_path, record.signed_qr_code, record.irn, now,       # QRPT, QRST, IRN, IRNGENDT
        now.strftime("%H:%M:%S"), doctype, base_entry,         # GenrTime, DocType, BaseEntry
        record.ack_no or None,                                 # AckNo
        int(docentry),                                         # the document to read Period from
        base_entry, doctype,                                   # the duplicate guard
    ]

    for attempt in range(1, _KEY_RETRIES + 1):
        try:
            with HANAConnection() as conn:
                conn.execute(sql, params)
                inserted = getattr(conn.cursor, "rowcount", -1)
            if inserted == 0:
                logger.info("UDO mirror: %s DocEntry %s already has an 'S' row "
                            "(or the document was not found) - nothing written",
                            schema, base_entry)
                return False
            logger.info("UDO mirror: wrote IRN %s for %s DocEntry %s",
                        (record.irn or "")[:12], schema, base_entry)
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            if _is_duplicate_key(exc) and attempt < _KEY_RETRIES:
                logger.info("UDO mirror: DocEntry race on %s, retry %s/%s",
                            schema, attempt, _KEY_RETRIES)
                continue
            logger.exception("UDO mirror failed for %s DocEntry %s", schema, base_entry)
            return False
    return False


def mark_cancelled(irn, *, schema=None) -> bool:
    """Stamp the mirrored row as cancelled so it stops printing.

    Needs UPDATE on the schema. Oil and Mart have it; **Beverages does not**, so
    there this logs and returns False and the SAP-side bill keeps showing the
    cancelled IRN until the grant is in place (docs §2).
    """
    from hana.services.connection import HANAConnection

    schema = _schema(schema)
    if not schema or not irn:
        return False
    now = datetime.now()
    sql = (f'UPDATE "{schema}"."{TABLE}" SET "Canceled" = ?, "U_UTL_CANDT" = ?, '
           f'"UpdateDate" = ?, "UpdateTime" = ?, "U_UTL_CancelTime" = ? '
           f'WHERE "U_UTL_IRN" = ? AND "Creator" = ?')
    try:
        with HANAConnection() as conn:
            conn.execute(sql, ['Y', now, now, now.hour * 100 + now.minute,
                               now.strftime("%H:%M:%S"), irn, CREATOR])
            changed = getattr(conn.cursor, "rowcount", -1)
        logger.info("UDO mirror: cancel stamped %s row(s) for IRN %s in %s",
                    changed, (irn or "")[:12], schema)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("UDO mirror: cancel stamp failed for IRN %s in %s", irn, schema)
        return False


def missing_rows(schema=None, *, limit=500):
    """OMS_IRN_LOG 'S' rows with no `@UTL_MDEXTH` counterpart — the divergence
    left behind when the mirror write failed. Backs `reconcile_udo_mirror`.
    """
    from hana.services.connection import HANAConnection

    schema = _schema(schema)
    if not schema:
        return []
    sql = f'''
SELECT O."DocEntry", O."DocNum", O."U_UTL_BaseEntry", O."U_UTL_DocType",
       O."U_UTL_IRN", O."U_UTL_QRST", O."U_UTL_QRPT", O."U_UTL_AckNo"
  FROM "{schema}"."OMS_IRN_LOG" O
 WHERE O."U_UTL_IST" = 'S'
   AND IFNULL(O."Canceled", 'N') <> 'Y'
   AND IFNULL(O."U_UTL_BaseEntry", '') <> ''
   AND NOT EXISTS (SELECT 1 FROM "{schema}"."{TABLE}" U
                    WHERE U."U_UTL_BaseEntry" = O."U_UTL_BaseEntry"
                      AND U."U_UTL_DocType"  = O."U_UTL_DocType"
                      AND U."U_UTL_IST" = 'S')
 ORDER BY O."DocEntry"
 LIMIT {int(limit)}
'''
    with HANAConnection() as conn:
        return conn.execute(sql)
