"""Walk invoices to Payment once SAP shows the A/P document already posted.

An invoice can be saved in SAP without anyone advancing the tracker row behind
it — the desk posts the document and moves on. The row then sits at whatever
stage it was on, ageing, showing up in the queue and in the stuck-alert mail,
describing a state of the world that ended when the document was saved. This
sweep closes that gap: for every in-progress invoice it asks SAP whether a
posted, non-cancelled A/P invoice or credit memo exists for that vendor and
number, and if so marks the intervening desks skipped and moves the invoice to
Payment. The reason written on every one of those visits names the SAP document
it was matched to.

Only POSTED documents count. A draft (ODRF) is pending, not saved, and is
ignored — see `tracker.sap.posted_documents_for`.

Idempotent: an invoice already at Payment is never a candidate, so running it
twice changes nothing. Safe to schedule.

    python manage.py sync_sap_saved [--dry-run] [--limit N] [--ids 1 2 3]
"""
from django.core.management.base import BaseCommand

from tracker import services


class Command(BaseCommand):
    help = "Advance invoices to Payment when SAP already holds the posted document."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="Report what would move; change nothing.")
        parser.add_argument('--limit', type=int, default=None,
                            help="Check at most N invoices (oldest id first).")
        parser.add_argument('--ids', nargs='*', type=int, default=None,
                            help="Restrict to these tracker invoice ids.")
        parser.add_argument('--verbose', action='store_true',
                            help="List every invoice moved, not just the totals.")

    def handle(self, *args, **opts):
        result = services.sync_sap_saved(
            invoice_ids=opts['ids'], user=None,
            dry_run=opts['dry_run'], limit=opts['limit'],
        )

        advanced = result['advanced']
        prefix = '[dry-run] would advance' if opts['dry_run'] else 'Advanced'

        if opts['verbose'] or opts['dry_run']:
            for row in advanced:
                inv, doc = row['invoice'], row['document']
                self.stdout.write(
                    f"  {inv.invoice_number:<20} {inv.party_name[:28]:<28} "
                    f"{row['from_stage']:<20} -> Payment   "
                    f"{doc['table']} {doc['docnum']}")

        for err in result['errors']:
            self.stderr.write(self.style.ERROR(
                f"  {err['invoice'].invoice_number}: {err['detail']}"))

        cross = result['cross_company']
        if cross:
            self.stdout.write(self.style.WARNING(
                f"{len(cross)} invoice(s) have a posted document in a DIFFERENT "
                f"company database than their branch/unit selects. Not advanced "
                f"— the tracker row or the posting is wrong and a person should "
                f"decide which."))

        self.stdout.write(self.style.SUCCESS(
            f"SAP-saved sweep: checked {result['checked']}, "
            f"{prefix} {len(advanced)}, {len(result['errors'])} failed, "
            f"{len(cross)} cross-company (not advanced)."))
