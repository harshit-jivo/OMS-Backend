"""
Backfill: mirror existing GENERATED IrnRecord rows (from order_management) into
the HANA EINVOICE_IRN table, including the QR PNG.

    python manage.py mirror_irns_to_hana --schema JIVO_OIL_HANADB
    python manage.py mirror_irns_to_hana --schema JIVO_OIL_HANADB --limit 100

The DocEntry is parsed from IrnRecord.source ("…OINV:<docentry>") when present.
Run setup_hana_irn_table first. Best-effort per row; failures are reported.
"""
import re

from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import hana_store
from einvoice.models import IrnRecord

_DOCENTRY_RE = re.compile(r"OINV:(\d+)")


class Command(BaseCommand):
    help = "Mirror existing GENERATED IRN records into HANA EINVOICE_IRN."

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None, help="HANA schema (default: HANA_COMPANY_DB)")
        parser.add_argument("--limit", type=int, default=None, help="Max rows to mirror")

    def handle(self, *args, **opts):
        schema = opts["schema"] or settings.HANA_COMPANY_DB
        qs = IrnRecord.objects.filter(generation_status="GENERATED").order_by("id")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        ok = fail = 0
        for rec in qs:
            m = _DOCENTRY_RE.search(rec.source or "")
            docentry = int(m.group(1)) if m else None
            if hana_store.mirror_record(rec, schema=schema, docentry=docentry):
                ok += 1
                self.stdout.write(f"  mirrored doc {rec.doc_no} (IRN {(rec.irn or '')[:12]}…)")
            else:
                fail += 1
                self.stdout.write(self.style.WARNING(f"  FAILED doc {rec.doc_no}"))

        self.stdout.write(self.style.SUCCESS(f"Done. mirrored={ok} failed={fail} into {schema}.{hana_store.TABLE}"))
