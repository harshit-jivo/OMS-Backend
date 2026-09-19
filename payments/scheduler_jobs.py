"""Payments jobs for the scheduler worker (`manage.py run_scheduler`).

WHY THIS EXISTS. The final approval defers its SAP call to
`transaction.on_commit`, which is not durable: if the process is restarted
between the commit and the callback, the call is simply lost and the document
sits in POSTING_TO_SAP owing a post that nothing will ever make. See
`sap_recovery` for the full account.

`recover_stranded_sap_posts` fixes such a document, but only when somebody runs
it. That is the gap this closes — the failure is caused by a restart, and a
restart is exactly when nobody is watching. Observed on
RCP-OIL-20260919-000003, approved at 09:38 with zero SAP call logs, which would
have sat unposted indefinitely.

A dev server autoreloads on every file save, so during active development this
is not a rare event — it is the normal consequence of editing code while a
document is mid-approval.

WHY IT LIVES HERE AND NOT IN `sap_sync.scheduler`. The worker process is
shared, but the job is a payments concern: its schedule, its logging and its
failure handling belong beside the code it recovers. `run_scheduler` calls
`register()` and knows nothing else about it.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Fixed id, so `replace_existing` updates the one job rather than accumulating
#: copies across worker restarts.
SWEEP_JOB_ID = 'payments_recover_stranded_sap_posts'

#: Default seconds between sweeps. Five minutes: the sweep is cheap (two
#: indexed queries that return nothing in the normal case) and the cost of a
#: delay is a customer payment sitting unposted.
DEFAULT_SWEEP_INTERVAL_SECONDS = 300


def sweep_interval_seconds():
    """Seconds between sweeps, from `PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS`.

    Floored at 30. A zero or negative interval makes APScheduler's
    IntervalTrigger raise, which would take the whole worker down at startup
    over a typo in the environment.
    """
    configured = getattr(settings, 'PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS',
                         DEFAULT_SWEEP_INTERVAL_SECONDS)
    try:
        return max(30, int(configured))
    except (TypeError, ValueError):
        logger.warning(
            'PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS=%r is not a number; '
            'using %s', configured, DEFAULT_SWEEP_INTERVAL_SECONDS)
        return DEFAULT_SWEEP_INTERVAL_SECONDS


def sweep_stranded_posts():
    """Post anything whose approval landed but whose SAP call never ran.

    NEVER RAISES. An exception escaping a job is logged by APScheduler and the
    job keeps its schedule, but a traceback every five minutes buries anything
    else in the worker's log. The sweep's own per-document error handling
    already isolates one bad document from the rest; this catches only the
    outer failures — the database being unreachable, say — and says so once.

    Silent when there is nothing to do, which is the normal case. A sweep that
    announced "0 stranded" every five minutes would train everyone to ignore
    the one line that matters.
    """
    from .sap_recovery import recover_stranded_posts

    try:
        summary = recover_stranded_posts()
    except Exception:                                      # noqa: BLE001
        logger.exception('stranded SAP post sweep failed')
        return None

    found = summary['receipts_found'] + summary['deposits_found']
    if not found:
        return summary

    logger.warning(
        'stranded SAP posts recovered: %s/%s receipts, %s/%s deposits, '
        '%s error(s)',
        summary['receipts_posted'], summary['receipts_found'],
        summary['deposits_posted'], summary['deposits_found'],
        summary['errors'])
    for detail in summary['details']:
        logger.warning('  %s', detail)
    return summary


def register(scheduler, *, run_now=True):
    """Add the sweep to `scheduler`, and by default run one immediately.

    THE IMMEDIATE RUN IS THE POINT. A document is stranded BY a restart, so the
    first moment worth sweeping is the moment the worker comes back up. Waiting
    a full interval would leave the very document this restart stranded sitting
    unposted for no reason.

    Returns the APScheduler job.
    """
    interval = sweep_interval_seconds()

    if run_now:
        sweep_stranded_posts()

    return scheduler.add_job(
        sweep_stranded_posts,
        trigger='interval',
        seconds=interval,
        id=SWEEP_JOB_ID,
        replace_existing=True,
        # Skip rather than stack. A sweep that overruns its interval is talking
        # to a slow SAP; starting a second one alongside it would put two
        # posts in flight for the same document, which is the one outcome
        # every guard in `sap_recovery` exists to prevent.
        max_instances=1,
        coalesce=True,
        # Memory, not the Django job store: this job is a fixed part of the
        # worker rather than user configuration, so it is re-added on every
        # start and must not outlive the process that owns it.
        jobstore='memory',
    )
