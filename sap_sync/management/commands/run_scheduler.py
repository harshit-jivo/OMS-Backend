"""The scheduler worker — Phase 5.1.

Run this as its own long-lived process, NOT inside the web server::

    python manage.py run_scheduler

One process, one scheduler. A second copy refuses to start rather than
double-running the jobs, so restarting under a supervisor that overlaps the old
and new process is safe.

The guard is a Postgres session advisory lock. It is the right tool here
because it needs no table (this refactor makes no schema changes), it is held
by the connection rather than by a row, and the database releases it the moment
the process dies — so a killed worker never leaves a stale lock behind that a
human has to clear. A lock file could not promise any of those things on a
Windows host that occasionally reboots mid-job.
"""
import logging
import signal

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django_apscheduler.jobstores import DjangoJobStore

from payments import scheduler_jobs as payments_jobs
from sap_sync.scheduler import (
    RECONCILE_INTERVAL_SECONDS,
    reconcile_schedules,
)

logger = logging.getLogger(__name__)

#: Arbitrary but fixed. Postgres advisory locks share one namespace across the
#: whole database, so this number must not collide with another one; it is
#: recorded here because it exists nowhere else.
ADVISORY_LOCK_KEY = 8231150

RECONCILE_JOB_ID = 'sap_sync_reconcile'


def acquire_singleton_lock():
    """Try to become the one scheduler. Returns True if we got it.

    Session-scoped (`pg_try_advisory_lock`, not `_xact_`), so it is held for as
    long as this connection lives rather than to the end of a transaction. The
    transaction-scoped variant would release at the end of this very statement
    and the guard would protect nothing while appearing to work.
    """
    if connection.vendor != 'postgresql':
        # Refuse rather than fail open. Returning True here would let two
        # workers run on a backend that cannot express the constraint, which is
        # the exact failure the lock exists to prevent — and it would do so
        # silently. OMS runs on Postgres; anything else is a misconfiguration.
        raise CommandError(
            f'The scheduler needs a Postgres advisory lock to guarantee a '
            f'single instance, and the default database is '
            f'{connection.vendor!r}. Refusing to run without the guard.')
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [ADVISORY_LOCK_KEY])
        return bool(cursor.fetchone()[0])


def release_singleton_lock():
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s)', [ADVISORY_LOCK_KEY])


class Command(BaseCommand):
    help = 'Run the SAP sync scheduler in a dedicated worker process.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval', type=int, default=RECONCILE_INTERVAL_SECONDS,
            help='Seconds between re-reads of the schedule table '
                 f'(default {RECONCILE_INTERVAL_SECONDS}).')
        parser.add_argument(
            '--once', action='store_true',
            help='Reconcile once and exit without starting the scheduler. '
                 'For checking configuration without holding the process.')

    def handle(self, *args, **options):
        if not acquire_singleton_lock():
            # Not an error the operator needs to fix — this is exactly what
            # should happen during an overlapping restart. But it IS a non-zero
            # exit, so a supervisor does not treat the loser as a healthy start.
            raise CommandError(
                'Another scheduler process already holds the lock. '
                'Only one may run at a time; this one is exiting.')

        scheduler = BlockingScheduler()
        # Sync jobs persist, so a restart does not lose them and `next_run` in
        # the admin reflects something real.
        scheduler.add_jobstore(DjangoJobStore(), 'default')
        # The reconcile job does not: it closes over the live scheduler, which
        # cannot be pickled into the Django store.
        scheduler.add_jobstore(MemoryJobStore(), 'memory')

        added, removed = reconcile_schedules(scheduler)
        self.stdout.write(f'Reconciled: {added} active schedule(s), {removed} removed.')

        if options['once']:
            # Still sweep. `--once` is used to check configuration, and a
            # stranded payment is worth recovering whether or not the worker
            # then stays up.
            payments_jobs.sweep_stranded_posts()
            release_singleton_lock()
            return

        scheduler.add_job(
            reconcile_schedules,
            trigger='interval',
            seconds=options['interval'],
            id=RECONCILE_JOB_ID,
            args=[scheduler],
            replace_existing=True,
            # If a reconcile overruns, skip rather than stack — the next one a
            # minute later reaches the same end state anyway.
            max_instances=1,
            coalesce=True,
            jobstore='memory',
        )

        # Payments: re-post anything whose approval landed but whose SAP call
        # was lost to a restart. Registered here because this is the one
        # process in the system that runs scheduled work; the job itself and
        # its interval belong to payments. It sweeps once immediately, since
        # the restart that just happened is itself a cause of stranding.
        sweep_job = payments_jobs.register(scheduler)
        self.stdout.write(
            f'Stranded SAP post sweep every {sweep_job.trigger.interval.total_seconds():.0f}s.')

        def shutdown(signum, _frame):
            logger.info('Scheduler received signal %s, shutting down', signum)
            scheduler.shutdown(wait=True)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, shutdown)
            except (ValueError, AttributeError, OSError):
                # Not on the main thread, or the signal does not exist on this
                # platform. APScheduler's own KeyboardInterrupt handling still
                # covers the interactive case.
                pass

        self.stdout.write(self.style.SUCCESS(
            f'SAP sync scheduler running (reconcile every {options["interval"]}s). '
            'Ctrl-C to stop.'))
        try:
            scheduler.start()
        finally:
            release_singleton_lock()
