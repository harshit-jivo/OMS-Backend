"""
Find OMS IRNs that never reached the SAP UDO table, and optionally write them.

The mirror in `einvoice.sap_udo` is best-effort by design — it must never put the
`OMS_IRN_LOG` write (the one OMS's own bill print depends on) at risk. That means
a HANA hiccup, a lost DocEntry race, or the flag being off for a while leaves
invoices that print from OMS but show no IRN on any SAP-side Crystal layout.
This command is the safety net for exactly that gap.

    python manage.py reconcile_udo_mirror                 # report, all companies
    python manage.py reconcile_udo_mirror --apply         # write the missing rows
    python manage.py reconcile_udo_mirror --company MART

Exit codes: 0 nothing missing, 1 rows missing (report mode), 2 a company failed.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import sap_udo


class _Rec:
    """The few IrnRecord attributes sap_udo.write_success reads, rebuilt from a
    stored OMS_IRN_LOG row — there is no Django record to hand for a backfill."""

    def __init__(self, row):
        self.doc_no = row.get("DocNum")
        self.irn = row.get("U_UTL_IRN")
        self.ack_no = row.get("U_UTL_AckNo")
        self.signed_qr_code = row.get("U_UTL_QRST")
        self.doc_type = "CRN" if str(row.get("U_UTL_DocType") or "") == "14" else "INV"


class Command(BaseCommand):
    help = "Mirror OMS_IRN_LOG 'S' rows that are missing from @UTL_MDEXTH."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="OIL, BEVERAGE or MART (default: all configured)")
        parser.add_argument("--apply", action="store_true",
                            help="write the missing rows (default: report only)")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **opts):
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

        total_missing = failures = written = 0
        for label, schema in companies.items():
            try:
                rows = sap_udo.missing_rows(schema, limit=opts["limit"])
            except Exception as exc:  # noqa: BLE001 — one company must not stop the rest
                failures += 1
                self.stderr.write(f"{label}: lookup failed: {exc}")
                continue

            total_missing += len(rows)
            if not rows:
                self.stdout.write(f"{label}: up to date")
                continue
            self.stdout.write(f"{label} ({schema}): {len(rows)} IRN(s) missing from @UTL_MDEXTH")

            if not opts["apply"]:
                for r in rows[:20]:
                    self.stdout.write(
                        f"    invoice {r.get('DocNum')}  BaseEntry {r.get('U_UTL_BaseEntry')}"
                        f"  IRN {str(r.get('U_UTL_IRN'))[:16]}...")
                if len(rows) > 20:
                    self.stdout.write(f"    ... and {len(rows) - 20} more")
                continue

            for r in rows:
                base = r.get("U_UTL_BaseEntry")
                try:
                    ok = sap_udo.write_success(_Rec(r), docentry=int(base),
                                               qr_path=r.get("U_UTL_QRPT"), schema=schema)
                except (TypeError, ValueError):
                    ok = False
                    self.stderr.write(f"    BaseEntry {base!r} is not a DocEntry - skipped")
                if ok:
                    written += 1
                else:
                    # Not necessarily an error: write_success returns False when
                    # the add-on already holds an 'S' row for the invoice.
                    self.stdout.write(f"    BaseEntry {base}: not written (duplicate or failed)")

        if opts["apply"]:
            self.stdout.write(self.style.SUCCESS(f"wrote {written} row(s)"))
        if failures:
            raise SystemExit(2)
        if total_missing and not opts["apply"]:
            raise SystemExit(1)
