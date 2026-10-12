"""A payment's attachment written straight into SAP's tables, when the Service Layer cannot.

WHY THIS EXISTS
---------------
The Service Layer (on .222, Linux) reads the attachments share through its own
mount, and when that mount drops it refuses every attachment: "-5002 Source
file does not exist" or "Attachments folder ... changed or removed", even for
a file the file upload service (.118:8013) has just put on the share. The
file itself is fine and where SAP keeps attachments; only the record is
missing. factory_app's GRPO attachments do the same
(`SAP_ATTACHMENT_HANA_FALLBACK`); decided for OMS 2026-10-12.

WHAT IS WRITTEN — what SAP's own client writes for a file already in the
attachments folder (ATC1 rows made by the B1 client, e.g. 182521):

    OATC  AbsEntry                 next of SAP's own sequence OATC_S, so it can
                                   never take a number SAP gives out
    ATC1  AbsEntry, Line 1..n
          srcPath = trgtPath       the company's attachments folder, as SAP has
                                   it (OADP.AttachPath, the UNC path) — not a
                                   path only one machine can see
          FileName, FileExt        the name the file service stored it under
          Date                     today, India time, no time of day
          UsrID                    the Service Layer user (OUSR), as if it had
                                   made the row
          Copied, CopyToTrgt  'Y'  the file IS in the target folder
          Override 'N', CopyToProd, EDocSign, SendInMail, CopyFile, FileSuffix 'N'
          FileSize, U_CHK          size in KB; U_CHK2 'OK' (UDFs, where present)

All rows of one call commit together or not at all. Only schemas OMS is
configured for are written, so a schema name never comes from a request.
"""
import logging
import math

from django.conf import settings

from advance_payment.services import clock

logger = logging.getLogger(__name__)


class RowsError(Exception):
    """The rows could not be written; nothing was."""


def _schemas():
    names = ('HANA_OIL_COMPANY_DB', 'HANA_BEVERAGE_COMPANY_DB', 'HANA_MART_COMPANY_DB', 'HANA_TEST_COMPANY_DB')
    return {getattr(settings, n, '') for n in names} - {''}


def enabled():
    return bool(getattr(settings, 'SAP_ATTACHMENT_HANA_FALLBACK', False))


def _split(name):
    stem, _, ext = (name or '').rpartition('.')
    return (stem, ext) if stem else (name or '', '')


def _kb(size):
    return max(1, math.ceil(size / 1024)) if size else None


class _Writer:
    """One HANA connection, one transaction."""

    def __init__(self, schema):
        if schema not in _schemas():
            raise RowsError(f'{schema!r} is not a company database OMS writes to.')
        self.schema = schema

    def __enter__(self):
        from hana.services.connection import HANAConnection

        self.hana = HANAConnection()
        self.hana.connect()
        self.hana.connection.setautocommit(False)
        self.cursor = self.hana.connection.cursor()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.hana.connection.commit()
            else:
                self.hana.connection.rollback()
        finally:
            self.hana.disconnect()

    def one(self, sql, params=()):
        self.cursor.execute(sql, list(params))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def run(self, sql, params=()):
        self.cursor.execute(sql, list(params))

    def folder(self):
        path = self.one(f'SELECT TO_NVARCHAR("AttachPath") FROM "{self.schema}"."OADP"')
        if not path:
            raise RowsError('SAP has no attachments folder set (Administration > General Settings > Path).')
        return path.rstrip('\\/')

    def user_id(self):
        return self.one(f'SELECT "USERID" FROM "{self.schema}"."OUSR" WHERE "USER_CODE" = ?',
                        [getattr(settings, 'HANA_USERNAME', '')])

    def has_column(self, column):
        return bool(self.one('SELECT COUNT(*) FROM SYS.TABLE_COLUMNS WHERE SCHEMA_NAME = ? '
                             'AND TABLE_NAME = \'ATC1\' AND COLUMN_NAME = ?', [self.schema, column]))

    def lines(self, entry, first_line, files):
        folder, user, today = self.folder(), self.user_id(), clock.today()
        udfs = [c for c in ('U_CHK', 'U_CHK2') if self.has_column(c)]
        columns = ['AbsEntry', 'Line', 'srcPath', 'trgtPath', 'FileName', 'FileExt', 'Date', 'UsrID',
                   'Copied', 'Override', 'CopyToTrgt', 'CopyToProd', 'EDocSign', 'SendInMail', 'FileSize',
                   'CopyFile', 'FileSuffix', *udfs]
        sql = (f'INSERT INTO "{self.schema}"."ATC1" ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in columns)}) '
               f'VALUES ({", ".join("?" for _ in columns)})')
        for n, (name, size) in enumerate(files):
            stem, ext = _split(name)
            values = [entry, first_line + n, folder, folder, stem, ext, today, user,
                      'Y', 'N', 'Y', 'N', 'N', 'N', _kb(size), 'N', 'N']
            values += [{'U_CHK': _kb(size) or 1, 'U_CHK2': 'OK'}[c] for c in udfs]
            self.run(sql, values)


def create(schema, files):
    """A new attachment of `files` (`[(stored name, bytes)]`). Its AbsEntry."""
    try:
        with _Writer(schema) as w:
            entry = int(w.one(f'SELECT "{schema}"."OATC_S".NEXTVAL FROM DUMMY'))
            w.run(f'INSERT INTO "{schema}"."OATC" ("AbsEntry") VALUES (?)', [entry])
            w.lines(entry, 1, files)
    except RowsError:
        raise
    except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
        logger.warning('ADVANCE: attachment rows not written in %s: %s', schema, exc, exc_info=True)
        raise RowsError(f'Could not write the attachment in SAP: {exc}') from exc
    logger.info('ADVANCE: attachment %s written in %s (%s files)', entry, schema, len(files))
    return entry


def add(schema, entry, files):
    """`files` added to attachment `entry` as its next lines."""
    try:
        with _Writer(schema) as w:
            if not w.one(f'SELECT COUNT(*) FROM "{schema}"."OATC" WHERE "AbsEntry" = ?', [entry]):
                raise RowsError(f'SAP has no attachment {entry}.')
            last = w.one(f'SELECT IFNULL(MAX("Line"), 0) FROM "{schema}"."ATC1" WHERE "AbsEntry" = ?', [entry])
            w.lines(entry, int(last) + 1, files)
    except RowsError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning('ADVANCE: attachment %s lines not added in %s: %s', entry, schema, exc, exc_info=True)
        raise RowsError(f'Could not add to the attachment in SAP: {exc}') from exc


def link_payment(schema, doc_entry, entry):
    """Set attachment `entry` on outgoing payment `doc_entry`, when it has none.

    Only after the Service Layer refused the same PATCH over the attachments
    folder; never replaces an attachment the payment already has.
    """
    try:
        with _Writer(schema) as w:
            w.run(f'UPDATE "{schema}"."OVPM" SET "AtcEntry" = ? WHERE "DocEntry" = ? AND "AtcEntry" IS NULL',
                  [entry, doc_entry])
            if w.cursor.rowcount != 1:
                raise RowsError(f'Payment {doc_entry} already has an attachment, or is not in SAP.')
    except RowsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RowsError(f'Could not set the attachment on the payment: {exc}') from exc
