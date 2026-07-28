"""
Polling sweep: generate IRNs for recent SAP invoices that don't have one yet.

Covers invoices created directly in SAP B1 (not via OMS) and retries failures.
Each attempt is recorded in einvoice_irn_generation_log. Idempotent — invoices
that already succeeded (or were skipped) are not re-processed.

Schedule it (Windows Task Scheduler / cron / APScheduler), e.g. every 10 min:

    python manage.py auto_generate_irns --company-db JIVO_OIL_HANADB --limit 30
    python manage.py auto_generate_irns --since 76000 --dry-run
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import sap, services
from einvoice.models import IrnGenerationLog


class Command(BaseCommand):
    help = "Generate IRNs for recent SAP invoices that don't have one yet (polling sweep)."

    def add_arguments(self, parser):
        parser.add_argument("--company-db", default=None, help="SAP company DB (defaults to HANA_COMPANY_DB)")
        parser.add_argument("--limit", type=int, default=25, help="How many recent invoices to scan")
        parser.add_argument("--since", type=int, default=None, help="Only DocEntry greater than this")
        parser.add_argument("--dry-run", action="store_true", help="List what would be processed; don't call NIC")

    def handle(self, *args, **opts):
        # No --company-db -> sweep EVERY configured company, so beverage/oil
        # invoices each get their IRN generated against (and mirrored into) their
        # own company DB instead of everything defaulting to OIL.
        if not opts["company_db"]:
            for choice in sap.company_choices():
                self.stdout.write(self.style.MIGRATE_HEADING(
                    f"\n=== {choice['label']} ({choice['company_db']}) ==="))
                self._sweep(dict(opts, company_db=choice["company_db"]))
            return
        self._sweep(opts)

    def _sweep(self, opts):
        company_db = opts["company_db"]
        base = settings.HANA_SERVICE_LAYER_URL.rstrip("/")
        session = sap.get_session(company_db)

        flt = f"&$filter=DocEntry gt {opts['since']}" if opts["since"] is not None else ""
        url = (f"{base}/Invoices?$select=DocEntry,DocNum,CardName,DocTotal"
               f"&$orderby=DocEntry desc&$top={opts['limit']}{flt}")
        resp = session.get(url, verify=getattr(settings, "HANA_SSL_VERIFY", False), timeout=60)
        resp.raise_for_status()
        rows = resp.json().get("value", [])
        self.stdout.write(f"Scanned {len(rows)} recent invoice(s).")

        # DocEntries already handled (succeeded or skipped) — don't re-process.
        done = set(
            IrnGenerationLog.objects.filter(
                docentry__in=[r["DocEntry"] for r in rows], outcome__in=["SUCCESS", "SKIPPED"]
            ).values_list("docentry", flat=True)
        )

        processed = ok = failed = 0
        for r in rows:
            de = r["DocEntry"]
            if de in done:
                continue
            processed += 1
            if opts["dry_run"]:
                self.stdout.write(f"  would process DocEntry {de} ({r.get('CardName')})")
                continue
            log = services.auto_generate_irn(de, company_db=company_db, trigger="poll")
            outcome = getattr(log, "outcome", "FAILED")
            if outcome == "SUCCESS":
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"  DocEntry {de}: IRN {(log.irn or '')[:16]}…"))
            elif outcome == "SKIPPED":
                self.stdout.write(f"  DocEntry {de}: skipped ({log.error_message})")
            else:
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f"  DocEntry {de}: FAILED [{log.error_code}] {log.error_message}"))

        self.stdout.write(self.style.SUCCESS(
            f"Done. processed={processed} success={ok} failed={failed} (already-done skipped)."))
