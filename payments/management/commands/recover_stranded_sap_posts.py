"""Post approved payments/deposits whose SAP call never ran.

The approval hook defers its SAP post to `transaction.on_commit`, which is not
durable: if the worker restarts between the commit and the callback, the post
is lost and the document is stranded APPROVED-but-never-sent, with no other
code path that will ever pick it up.

Run from cron or Task Scheduler alongside reconcile_sap_cancellations:

    python manage.py recover_stranded_sap_posts --dry-run
    python manage.py recover_stranded_sap_posts
    python manage.py recover_stranded_sap_posts --company OIL --limit 5

ALWAYS run --dry-run first: this is the one command here that WRITES to SAP.
It only ever posts documents proven never to have been sent (no SAP call log,
no DocEntry, still PENDING_APPROVAL), so it cannot duplicate a document — but
it is a financial write, and seeing the list first costs nothing.
"""
from django.core.management.base import BaseCommand

from payments.sap_recovery import recover_stranded_posts


class Command(BaseCommand):
    help = 'Post approved payments/deposits whose SAP call was never made.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--company', default='',
            help='Restrict to one company (OIL / MART / BEVERAGES).')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Recover at most this many documents of each kind.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List what would be posted without calling SAP.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        summary = recover_stranded_posts(
            company=options['company'], limit=options['limit'],
            dry_run=dry_run)

        found = summary['receipts_found'] + summary['deposits_found']
        posted = summary['receipts_posted'] + summary['deposits_posted']

        for line in summary['details']:
            self.stdout.write(f'  {line}')

        self.stdout.write(
            f"receipts: {summary['receipts_found']} stranded, "
            f"{summary['receipts_posted']} posted")
        self.stdout.write(
            f"deposits: {summary['deposits_found']} stranded, "
            f"{summary['deposits_posted']} posted")

        if summary['errors']:
            self.stdout.write(self.style.WARNING(
                f"{summary['errors']} document(s) could not be posted"))

        if not found:
            self.stdout.write(self.style.SUCCESS('No stranded documents found'))
        elif dry_run:
            self.stdout.write(self.style.WARNING(
                f'{found} document(s) would be posted — rerun without '
                f'--dry-run to post them'))
        elif posted:
            self.stdout.write(self.style.SUCCESS(
                f'{posted} document(s) recovered and posted to SAP'))
