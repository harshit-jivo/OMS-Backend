"""Mirror JSAP budget decisions onto invoices parked at the JSAP desk.

Approved invoices advance, rejected ones go back to SAP Approval carrying the
approver's own reason, and anything still pending stays put. Nothing is
written to JSAP — it is the system of record.

Intended to run on a schedule (every few minutes) alongside the stuck-alert
sweep; also exposed as a manual "refresh" button on the JSAP desk.

    python manage.py sync_jsap [--limit N] [--dry-run]
"""
from django.core.management.base import BaseCommand

from tracker import jsap, services
from tracker.models import Invoice


class Command(BaseCommand):
    help = 'Advance/return invoices at the JSAP desk to match JSAP.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N invoices.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would happen without moving anything.')

    def handle(self, *args, **options):
        if not jsap.is_configured():
            self.stderr.write(self.style.WARNING(
                'JSAP database is not configured (JSAP_DB_HOST/JSAP_DB_NAME) — nothing to do.'))
            return

        if options['dry_run']:
            qs = (Invoice.objects
                  .filter(current_stage__code=services.JSAP_STAGE_CODE,
                          status=Invoice.Status.IN_PROGRESS)
                  .select_related('current_stage', 'category', 'unit', 'branch'))
            if options['limit']:
                qs = qs[:options['limit']]
            for inv in qs:
                st = jsap.status_for_invoice(inv)
                verdict = st.get('label') if st.get('available') else st.get('reason')
                self.stdout.write(f'  {inv.invoice_number} ({inv.party_name}) -> {verdict}')
            self.stdout.write(self.style.SUCCESS('Dry run complete — nothing moved.'))
            return

        res = services.sync_jsap_all(limit=options['limit'])
        self.stdout.write(self.style.SUCCESS(
            f"advanced={len(res['advanced'])} returned={len(res['returned'])} "
            f"waiting={len(res['waiting'])} errors={len(res['errors'])}"))
        for err in res['errors']:
            self.stderr.write(self.style.ERROR(f"  invoice {err['id']}: {err['error']}"))
