"""
Put a QR PNG in every folder that is supposed to have one.

THE FAILURE THIS CATCHES
------------------------
`U_UTL_QRPT` holds a full path and the report opens that path at render time. If
the row is right but the FILE is not there, nothing errors anywhere — the bill
simply prints with a blank QR box. Nobody finds out until a customer or an
officer looks at the invoice.

A different fault from the one `backfill_qr_png` fixes:

    backfill_qr_png     row has NO path        -> render PNG, stamp U_UTL_QRPT
    repair_qr_files     row HAS a path, but    -> render the PNG into the folder
                        the file is missing       that row's reader opens

TWO FOLDERS, TWO READERS
------------------------
Each table records the folder its own reader looks in, so each is checked
against its own table:

    OMS_Attachments\\<CO>_ATTACHMENTS\\Bitmap   OMS_IRN_LOG   the OMS bill print
    SAP Attachments\\Jivo <Co>\\Bitmaps         @UTL_MDEXTH   the SAP Crystal layouts

The OMS folder is the one that must never be short a file — without it OMS
cannot print at all — so it is checked first and reported separately.

Nothing is lost when a PNG goes missing: the QR is only a render of the signed
string in `U_UTL_QRST`, so it rebuilds from the row.

    python manage.py repair_qr_files                      # report, all companies
    python manage.py repair_qr_files --apply
    python manage.py repair_qr_files --company MART --apply
    python manage.py repair_qr_files --folder oms --apply  # just the OMS folders

MUST run on a host that can reach \\\\10.10.101.52 — i.e. .118, not a dev
workstation. Check with `python manage.py test_qr_share` first.

Idempotent: a file already present is never rewritten. The duplicate check is one
directory listing per folder, not a stat per row.
"""
import ntpath

from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import services


class Command(BaseCommand):
    help = "Write missing QR PNGs into the OMS and SAP Bitmaps folders."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="OIL, BEVERAGE or MART (default: all configured)")
        parser.add_argument("--folder", default="both", choices=["both", "oms", "sap"],
                            help="which folder to repair (default: both)")
        parser.add_argument("--apply", action="store_true",
                            help="write the missing PNGs (default: report only)")
        parser.add_argument("--limit", type=int, default=0, help="cap rows per folder")

    def handle(self, *args, **opts):
        from hana.services.connection import HANAConnection

        companies = {
            "OIL": getattr(settings, "HANA_OIL_COMPANY_DB", ""),
            "BEVERAGE": getattr(settings, "HANA_BEVERAGE_COMPANY_DB", ""),
            "MART": getattr(settings, "HANA_MART_COMPANY_DB", ""),
        }
        wanted = (opts.get("company") or "").upper()
        if wanted:
            if wanted not in companies:
                self.stderr.write(f"unknown company {wanted!r}; expected one of {sorted(companies)}")
                return
            companies = {wanted: companies[wanted]}
        companies = {k: v for k, v in companies.items() if v}

        missing_total = written_total = failed_total = 0

        for label, schema in companies.items():
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {label} ({schema}) ==="))

            # (folder, the table whose rows that folder must satisfy)
            targets = []
            if opts["folder"] in ("both", "oms"):
                targets.append(("OMS", services.oms_qr_dir_for_company(schema), "OMS_IRN_LOG"))
            if opts["folder"] in ("both", "sap"):
                targets.append(("SAP", services.qr_dir_for_company(schema), "@UTL_MDEXTH"))

            for kind, folder, table in targets:
                if not folder:
                    self.stderr.write(f"  {kind}: no folder configured - skipped")
                    continue
                self.stdout.write(f"  {kind} folder: {folder}")
                self.stdout.write(f"       checked against: {table}")

                present = services.qr_dir_listing(folder)
                if not present:
                    self.stderr.write(self.style.WARNING(
                        "       folder is empty or unreadable from this host - refusing "
                        "to treat every row as missing. Run on .118; see test_qr_share."))
                    failed_total += 1
                    continue
                self.stdout.write(f"       {len(present)} file(s) already there")

                try:
                    rows = self._rows(HANAConnection, schema, table, opts["limit"])
                except Exception as exc:  # noqa: BLE001
                    failed_total += 1
                    self.stderr.write(f"       query failed: {exc}")
                    continue

                missing = [(r, n) for r, n in ((r, self._file_name(r)) for r in rows)
                           if n and n.lower() not in present]
                self.stdout.write(f"       {len(rows)} row(s) with a signed QR, "
                                  f"{len(missing)} missing a PNG")
                missing_total += len(missing)
                if not missing:
                    continue

                if not opts["apply"]:
                    for r, name in missing[:10]:
                        self.stdout.write(f"          invoice {r.get('DocNum')}  "
                                          f"BaseEntry {r.get('U_UTL_BaseEntry')}  -> {name}")
                    if len(missing) > 10:
                        self.stdout.write(f"          ... and {len(missing) - 10} more")
                    continue

                for r, name in missing:
                    try:
                        path = services.save_signed_qr(
                            r["U_UTL_QRST"], folder,
                            doc_no=r.get("DocNum"), irn=r.get("U_UTL_IRN"),
                            ack_no=r.get("U_UTL_AckNo"))
                        present.add(ntpath.basename(path).lower())
                        written_total += 1
                    except Exception as exc:  # noqa: BLE001 — one row must not stop the run
                        failed_total += 1
                        self.stderr.write(f"          {name}: {exc}")

        self.stdout.write("")
        if opts["apply"]:
            self.stdout.write(self.style.SUCCESS(f"wrote {written_total} PNG(s)"))
        else:
            self.stdout.write(f"{missing_total} PNG(s) missing - re-run with --apply")
        if failed_total:
            raise SystemExit(2)
        if missing_total and not opts["apply"]:
            raise SystemExit(1)

    # -- helpers ----------------------------------------------------------
    def _rows(self, HANAConnection, schema, table, limit):
        """Rows that SHOULD have a PNG: successful, not cancelled, and carrying
        the signed QR string the PNG is rendered from.

        U_UTL_QRST and U_UTL_QRPT are LOB columns and HANA will not compare a LOB
        with a literal, so emptiness is tested with LENGTH().
        """
        lim = f" LIMIT {int(limit)}" if limit else ""
        sql = (f'SELECT "DocNum", "U_UTL_IRN", "U_UTL_QRST", "U_UTL_QRPT", '
               f'       "U_UTL_AckNo", "U_UTL_BaseEntry" '
               f'FROM "{schema}"."{table}" '
               f'WHERE "U_UTL_IST" = \'S\' '
               f'  AND IFNULL("Canceled", \'N\') <> \'Y\' '
               f'  AND "U_UTL_QRST" IS NOT NULL AND LENGTH("U_UTL_QRST") > 0'
               f' ORDER BY "DocEntry" DESC{lim}')
        with HANAConnection() as conn:
            return conn.execute(sql)

    def _file_name(self, row):
        """The filename this row's QR should have.

        The name recorded in U_UTL_QRPT wins when there is one — that is the
        exact name the report will try to open, so matching on anything else
        would leave the bill blank even after a 'successful' repair. The folder
        part of it is ignored on purpose: we are checking one specific folder,
        and old rows carry paths from shares that have since moved.
        """
        stored = (row.get("U_UTL_QRPT") or "").strip()
        if stored:
            return ntpath.basename(stored.replace("/", "\\"))
        if not row.get("U_UTL_IRN"):
            return None
        return services.qr_file_name(doc_no=row.get("DocNum"), irn=row.get("U_UTL_IRN"),
                                     ack_no=row.get("U_UTL_AckNo"))
