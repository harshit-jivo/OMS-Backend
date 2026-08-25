"""Rebuild missing QR PNGs for IRNs already in OMS_IRN_LOG.

When the QR file-save hook fails (typically the app host cannot reach
\\JIVO-APP), IRN generation still succeeds: the row is written with the signed
QR string in U_UTL_QRST but U_UTL_QRPT left NULL, and the report then prints no
QR. Nothing is lost — the PNG is just a render of U_UTL_QRST — so this command
regenerates the file and stamps the path back onto the row.

MUST run on a host that can reach the share (i.e. .75). Verify first with
`python manage.py test_qr_share`.

    python manage.py backfill_qr_png [--company <DB>] [--docentry N] [--limit N] [--dry-run]

Idempotent: only rows with a blank U_UTL_QRPT are touched.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import services


class Command(BaseCommand):
    help = 'Regenerate QR PNGs for OMS_IRN_LOG rows whose U_UTL_QRPT is blank.'

    def add_arguments(self, parser):
        parser.add_argument('--company', default=None,
                            help='Company DB schema (default: all configured).')
        parser.add_argument('--docentry', type=int, default=None,
                            help='Only this SAP DocEntry (U_UTL_BaseEntry).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N rows per company.')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be written; write nothing.')

    def handle(self, *args, **opts):
        from hana.services.connection import HANAConnection

        schemas = ([opts['company']] if opts['company'] else
                   [s for s in (settings.HANA_OIL_COMPANY_DB,
                                settings.HANA_BEVERAGE_COMPANY_DB,
                                getattr(settings, 'HANA_MART_COMPANY_DB', '')) if s])

        total_fixed = total_failed = 0
        for schema in schemas:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== {schema} ==='))
            qr_dir = services.qr_dir_for_company(schema)
            if not qr_dir:
                self.stderr.write(self.style.WARNING('  no QR folder configured — skipped'))
                continue
            self.stdout.write(f'  target folder: {qr_dir}')

            # U_UTL_QRST (and sometimes U_UTL_QRPT) are LOB columns — HANA refuses
            # to compare a LOB with a string literal, so test LENGTH() instead.
            where = ('WHERE "U_UTL_IST" = \'S\''
                     '  AND ("U_UTL_QRPT" IS NULL OR LENGTH("U_UTL_QRPT") = 0)'
                     '  AND "U_UTL_QRST" IS NOT NULL AND LENGTH("U_UTL_QRST") > 0')
            if opts['docentry']:
                where += f'  AND "U_UTL_BaseEntry" = \'{int(opts["docentry"])}\''
            limit = f' LIMIT {int(opts["limit"])}' if opts['limit'] else ''
            sql = (f'SELECT "DocEntry", "DocNum", "U_UTL_IRN", "U_UTL_QRST", '
                   f'       "U_UTL_AckNo", "U_UTL_BaseEntry" '
                   f'FROM "{schema}"."OMS_IRN_LOG" {where} '
                   f'ORDER BY "DocEntry" DESC{limit}')

            try:
                with HANAConnection() as conn:
                    rows = conn.execute(sql)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f'  query failed: {exc}'))
                continue

            self.stdout.write(f'  {len(rows)} row(s) missing a QR path')
            for r in rows:
                doc_no = r.get('DocNum')
                irn = r.get('U_UTL_IRN')
                label = f'DocNum {doc_no} (BaseEntry {r.get("U_UTL_BaseEntry")})'
                if opts['dry_run']:
                    name = services.qr_file_name(doc_no=doc_no, irn=irn,
                                                 ack_no=r.get('U_UTL_AckNo'))
                    self.stdout.write(f'    would write {label} -> {qr_dir}\\{name}')
                    continue
                try:
                    path = services.save_signed_qr(
                        r['U_UTL_QRST'], qr_dir,
                        doc_no=doc_no, irn=irn, ack_no=r.get('U_UTL_AckNo'))
                    # Stamp the path back so the report picks the file up.
                    with HANAConnection() as conn:
                        conn.execute(
                            f'UPDATE "{schema}"."OMS_IRN_LOG" SET "U_UTL_QRPT" = ? '
                            f'WHERE "DocEntry" = ?', [path, r['DocEntry']])
                    self.stdout.write(self.style.SUCCESS(f'    {label} -> {path}'))
                    total_fixed += 1
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(self.style.ERROR(f'    {label} FAILED: {exc}'))
                    total_failed += 1

        if opts['dry_run']:
            self.stdout.write(self.style.SUCCESS('\nDry run complete — nothing written.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone. {total_fixed} QR file(s) written, {total_failed} failed.'))
