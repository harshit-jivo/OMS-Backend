"""Scheduled SAP master-data syncs.

Phase 5.1 moved scheduling OUT of the web process. What follows is the job
body plus the pieces a dedicated worker needs; the worker itself is
``manage.py run_scheduler``.

The plan recorded 5.1 as "jobs duplicate under multiple workers". That is not
what was happening. ``SapSyncConfig.ready`` gated the start on
``os.environ.get('RUN_MAIN') == 'true'``, and ``RUN_MAIN`` is set by the Django
dev-server AUTORELOADER and nothing else — not by waitress, gunicorn, uvicorn
or IIS. So under any production server the scheduler never started at all.

The database agrees: 0 rows in ``SyncSchedule``, 0 in ``django_apscheduler``'s
job and execution tables, and of 372 rows in ``sap_sync_logs`` not one carries
``triggered_by='scheduled'``. In the lifetime of this system APScheduler has
never run a single job.

Both readings — "runs twice" and "never runs" — argue for the same fix, which
is why 5.1 still stands: scheduling belongs in its own process, where it runs
exactly once and can be started, stopped and watched on its own terms.

Two further defects in the old design are fixed here rather than carried over:

* ``add_schedule_job`` / ``refresh_schedules`` were called only from
  ``start_scheduler``. No view called them, so creating or editing a schedule
  through ``/api/sap-sync/schedules/`` wrote a row and changed nothing about
  what was scheduled. With the scheduler out of process a view CANNOT reach it,
  so the worker reconciles from the table on a timer instead — see
  ``reconcile_schedules``. The database is the only channel, which is also the
  only channel that survives a worker restart.
* ``except:`` swallowed everything including ``KeyboardInterrupt``. Narrowed.
"""
import logging

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from django.utils import timezone

from .models import SyncSchedule
from .services import SyncService

logger = logging.getLogger(__name__)

#: Prefix for every job this module owns. ``reconcile_schedules`` uses it to
#: tell its own jobs apart from the reconcile job itself, so it never removes
#: the job that is doing the removing.
JOB_PREFIX = 'sync_schedule_'

#: How long after a missed fire time a job is still allowed to run. Beyond
#: this the slot is dropped rather than run late, because a master-data sync
#: hours after its window is worth less than a predictable next one.
MISFIRE_GRACE_SECONDS = 15 * 60

#: How often the worker re-reads the schedule table.
RECONCILE_INTERVAL_SECONDS = 60


def job_id_for(schedule_id):
    return f'{JOB_PREFIX}{schedule_id}'


def run_scheduled_sync(schedule_id):
    """Execute one scheduled sync. Referenced by ID from the job store, so the
    import path of this function is part of the persisted job — moving or
    renaming it orphans every stored job."""
    try:
        schedule = SyncSchedule.objects.get(pk=schedule_id)
    except SyncSchedule.DoesNotExist:
        logger.error('Schedule %s not found', schedule_id)
        return

    if not schedule.is_active:
        logger.info('Schedule %s is inactive, skipping', schedule.name)
        return

    logger.info('Starting scheduled sync: %s', schedule.name)
    try:
        sync_service = SyncService(triggered_by='scheduled')
        runner = {
            'PRODUCT': sync_service.sync_products,
            'PARTY': sync_service.sync_parties,
            'PARTY_ADDRESS': sync_service.sync_party_addresses,
        }.get(schedule.sync_type, sync_service.sync_all)
        result = runner()
    except Exception:
        # exc_info, not str(e): a sync reaches HANA, the Service Layer and
        # Postgres, and the message alone has never been enough to tell which.
        logger.exception('Scheduled sync failed: %s', schedule.name)
        return

    schedule.last_run = timezone.now()
    schedule.save(update_fields=['last_run'])
    logger.info('Scheduled sync completed: %s - %s', schedule.name, result)


def trigger_for(schedule):
    """Map a `SyncSchedule` row onto an APScheduler trigger."""
    if schedule.frequency == 'HOURLY':
        return IntervalTrigger(hours=1)
    if schedule.frequency == 'DAILY':
        return CronTrigger(hour=schedule.hour, minute=0)
    if schedule.frequency == 'WEEKLY':
        return CronTrigger(day_of_week='mon', hour=schedule.hour, minute=0)
    if schedule.frequency == 'CUSTOM':
        # A zero or negative interval makes IntervalTrigger raise, which would
        # take down reconciliation for every OTHER schedule too. The model
        # default is 60 and the field has no validator, so clamp.
        minutes = schedule.custom_interval_minutes or 60
        return IntervalTrigger(minutes=max(1, minutes))
    return IntervalTrigger(hours=24)


def add_schedule_job(scheduler, schedule):
    """Install (or replace) the job for one schedule."""
    job_id = job_id_for(schedule.id)

    if not schedule.is_active:
        remove_schedule_job(scheduler, schedule.id)
        return None

    trigger = trigger_for(schedule)
    job = scheduler.add_job(
        run_scheduled_sync,
        trigger=trigger,
        id=job_id,
        args=[schedule.id],
        name=schedule.name,
        replace_existing=True,
        # Without these, a worker that was down over several fire times comes
        # back up and runs the same sync once per missed slot.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )

    next_run = getattr(job, 'next_run_time', None) or trigger.get_next_fire_time(
        None, timezone.now())
    if schedule.next_run != next_run:
        schedule.next_run = next_run
        schedule.save(update_fields=['next_run'])

    logger.info('Scheduled %s (%s) next=%s', schedule.name, job_id, next_run)
    return job


def remove_schedule_job(scheduler, schedule_id):
    try:
        scheduler.remove_job(job_id_for(schedule_id))
    except JobLookupError:
        return False
    logger.info('Removed schedule job: %s', job_id_for(schedule_id))
    return True


def reconcile_schedules(scheduler):
    """Make the scheduler's jobs match the `SyncSchedule` table.

    This is the whole IPC story. The API server writes rows; the worker reads
    them on a timer. Nothing has to be signalled, nothing is lost across a
    restart, and there is no second source of truth to drift from.

    Returns ``(added, removed)`` for logging and for the tests.
    """
    wanted = {s.id: s for s in SyncSchedule.objects.filter(is_active=True)}
    current = {
        job.id for job in scheduler.get_jobs() if job.id.startswith(JOB_PREFIX)
    }

    removed = 0
    for job_id in current - {job_id_for(pk) for pk in wanted}:
        try:
            scheduler.remove_job(job_id)
            removed += 1
        except JobLookupError:
            pass

    added = 0
    for schedule in wanted.values():
        # Unconditional, not just for new rows: an edit to `frequency` or
        # `hour` has to reach the trigger, and `replace_existing` makes this
        # idempotent for the ones that did not change.
        if add_schedule_job(scheduler, schedule) is not None:
            added += 1

    if added or removed:
        logger.info('Reconciled schedules: %s active, %s removed', added, removed)
    return added, removed


# --------------------------------------------------------------------------
# Removed in 5.1 — kept as a record of what the in-web-process design was.
#
# A module-level `BackgroundScheduler` singleton, started from
# `SapSyncConfig.ready()`. Every web worker that imported this module got its
# own scheduler sharing one `DjangoJobStore`, which is the duplication the plan
# describes. It never actually fired (see the module docstring), but the shape
# was wrong regardless: a web worker is started, stopped and scaled for
# request handling, and scheduled work inherits all of that by accident.
#
#     scheduler = BackgroundScheduler()
#     scheduler.add_jobstore(DjangoJobStore(), "default")
#
#     def start_scheduler():
#         if scheduler.running:
#             return
#         for schedule in SyncSchedule.objects.filter(is_active=True):
#             add_schedule_job(schedule)
#         scheduler.start()
#
#     def stop_scheduler():
#         if scheduler.running:
#             scheduler.shutdown()
#
#     def refresh_schedules():
#         ...   # never called by anything
#
# The replacement is `manage.py run_scheduler`, which owns the lifecycle
# explicitly and holds a Postgres advisory lock so a second copy refuses to
# start rather than double-running the jobs.
# --------------------------------------------------------------------------
