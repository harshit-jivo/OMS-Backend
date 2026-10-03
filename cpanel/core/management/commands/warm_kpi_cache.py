"""Fill the KPI caches ahead of time so no real visitor pays the SAP wait.

Run it on a schedule (every 5 minutes is a good start):

    python manage.py warm_kpi_cache

It builds the top-strip ticker for the current month, and by default the
previous month too, because every "vs last month" figure needs it.

It used to warm the home-page KPI cards as well. That page is gone - Sales is
the main page now - so nothing reads that cache any more and warming it would
only cost SAP round-trips for figures no one would see.
"""

import time
from datetime import date

from django.core.management.base import BaseCommand

from cpanel.core.context_processors import get_ticker_items


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


class Command(BaseCommand):
    help = 'Pre-build the top-strip ticker cache so pages load instantly.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--months', type=int, default=2,
            help='How many months back to warm, counting this one (default 2).',
        )

    def handle(self, *args, **options):
        today = date.today()
        year, month = today.year, today.month

        for _ in range(max(1, options['months'])):
            started = time.time()

            get_ticker_items(year, month, blocking=True)

            self.stdout.write(self.style.SUCCESS(
                f'warmed {year}-{month:02d} in {time.time() - started:.1f}s'
            ))
            year, month = _prev_month(year, month)
