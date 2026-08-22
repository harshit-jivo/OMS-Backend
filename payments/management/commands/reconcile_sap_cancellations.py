"""Detect payments/deposits cancelled in SAP after a successful post.

Run from cron, Task Scheduler, or any job runner:

    python manage.py reconcile_sap_cancellations
    python manage.py reconcile_sap_cancellations --company OIL --limit 100

Read-only against SAP. It never posts, reposts or cancels anything in SAP; it
only reads ORCT.Canceled and records what it finds in OMS.
"""
from django.core.management.base import BaseCommand

from payments.sap_reconciliation import reconcile_sap_cancellations


class Command(BaseCommand):
    help = 'Mark OMS payments/deposits that were cancelled in SAP after posting.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--company', default=None,
            help='Restrict to one company (OIL / MART / BEVERAGES).')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Check at most this many documents of each kind.')

    def handle(self, *args, **options):
        summary = reconcile_sap_cancellations(
            limit=options['limit'], company=options['company'])

        self.stdout.write(
            f"receipts: {summary['receipts_checked']} checked, "
            f"{summary['receipts_cancelled']} newly cancelled")
        self.stdout.write(
            f"deposits: {summary['deposits_checked']} checked, "
            f"{summary['deposits_cancelled']} newly cancelled")

        total = summary['receipts_cancelled'] + summary['deposits_cancelled']
        if summary['errors']:
            self.stdout.write(self.style.WARNING(
                f"{summary['errors']} document(s) could not be checked"))
        if total:
            self.stdout.write(self.style.WARNING(
                f'{total} document(s) were cancelled in SAP — review required'))
        else:
            self.stdout.write(self.style.SUCCESS('No new cancellations found'))
