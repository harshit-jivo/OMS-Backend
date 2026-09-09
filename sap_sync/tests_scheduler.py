"""Scheduling — Phase 5.1.

`sap_sync/scheduler.py` had no tests, and that is how it stayed broken in
production for its whole life without anyone noticing: the auto-start was
gated on an environment variable no production server sets, so it silently
never ran. Nothing failed. Nothing logged. The only visible trace was an
absence — zero `triggered_by='scheduled'` rows out of 372 sync logs.

Tests here therefore cover two different things:

* the reconcile logic, which is ordinary pure-ish code over a real jobstore;
* the WIRING — that the scheduler is not started from app startup, and that
  the worker command exists and refuses to run twice. Those are the assertions
  that would have caught the original defect.

`MemoryJobStore` stands in for `DjangoJobStore` throughout. The Django store's
value is persistence across restarts, which is not what any of this is about,
and using it would drag `django_apscheduler`'s tables into every assertion.

Run with::

    python manage.py test sap_sync.tests_scheduler --settings=OMS.test_settings
"""
import ast
import inspect
from pathlib import Path
from unittest.mock import patch

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from sap_sync import scheduler as sched
from sap_sync.management.commands import run_scheduler as cmd_module
from sap_sync.models import SyncSchedule


def _scheduler():
    """A live scheduler with real job stores, started PAUSED.

    Not an un-started one. `add_job` on a scheduler that has never been
    started queues into `_pending_jobs`, a plain list where `replace_existing`
    is ignored and `get_job` returns the first match — so a second
    `reconcile_schedules` appends duplicates and every assertion about them
    still passes, because `{job.id for job in ...}` dedupes them away. The
    first draft of these tests did exactly that and was green.

    `start(paused=True)` gives the real store with the real replace semantics
    and no job ever fires.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_jobstore(MemoryJobStore(), 'default')
    scheduler.start(paused=True)
    return scheduler


class TriggerMappingTests(TestCase):

    def _schedule(self, **kwargs):
        return SyncSchedule(**{'name': 'n', 'hour': 6, **kwargs})

    def test_each_frequency_maps_to_its_trigger(self):
        cases = [
            ('HOURLY', IntervalTrigger),
            ('DAILY', CronTrigger),
            ('WEEKLY', CronTrigger),
            ('CUSTOM', IntervalTrigger),
        ]
        for frequency, expected in cases:
            with self.subTest(frequency=frequency):
                trigger = sched.trigger_for(self._schedule(frequency=frequency))
                self.assertIsInstance(trigger, expected)

    def test_daily_fires_at_the_configured_hour(self):
        trigger = sched.trigger_for(self._schedule(frequency='DAILY', hour=23))
        self.assertEqual(str(trigger.fields[trigger.FIELD_NAMES.index('hour')]), '23')

    def test_an_unknown_frequency_falls_back_to_daily_rather_than_crashing(self):
        """`frequency` has choices but no database constraint, so a bad value
        is reachable. Falling back beats raising inside the reconcile loop,
        which would take every OTHER schedule down with it."""
        trigger = sched.trigger_for(self._schedule(frequency='FORTNIGHTLY'))
        self.assertIsInstance(trigger, IntervalTrigger)

    def test_a_zero_custom_interval_does_not_raise(self):
        """`custom_interval_minutes` is a plain IntegerField with no validator,
        so 0 can be saved through the API. `IntervalTrigger(minutes=0)` raises,
        and it would do so inside reconciliation."""
        for minutes in (0, -5):
            with self.subTest(minutes=minutes):
                trigger = sched.trigger_for(
                    self._schedule(frequency='CUSTOM',
                                   custom_interval_minutes=minutes))
                self.assertIsInstance(trigger, IntervalTrigger)
                self.assertGreater(trigger.interval.total_seconds(), 0)


class ReconcileTests(TestCase):

    def setUp(self):
        self.scheduler = _scheduler()
        self.addCleanup(self.scheduler.shutdown, wait=False)

    def _job_ids(self):
        """A LIST-length check alongside the set, because a set of IDs cannot
        tell one job from two copies of it — see `_scheduler`."""
        jobs = self.scheduler.get_jobs()
        ids = {j.id for j in jobs}
        self.assertEqual(len(jobs), len(ids), 'duplicate jobs on the scheduler')
        return ids

    def test_an_active_schedule_becomes_a_job(self):
        s = SyncSchedule.objects.create(name='nightly', frequency='DAILY',
                                        is_active=True)
        added, removed = sched.reconcile_schedules(self.scheduler)
        self.assertEqual((added, removed), (1, 0))
        self.assertEqual(self._job_ids(), {sched.job_id_for(s.id)})

    def test_an_inactive_schedule_does_not(self):
        SyncSchedule.objects.create(name='off', is_active=False)
        added, _ = sched.reconcile_schedules(self.scheduler)
        self.assertEqual(added, 0)
        self.assertEqual(self._job_ids(), set())

    def test_deactivating_a_schedule_removes_its_job(self):
        """The reason reconciliation exists. Under the old design the API
        wrote `is_active=False` and the job kept running, because no view ever
        called into the scheduler."""
        s = SyncSchedule.objects.create(name='nightly', is_active=True)
        sched.reconcile_schedules(self.scheduler)
        self.assertIn(sched.job_id_for(s.id), self._job_ids())

        SyncSchedule.objects.filter(pk=s.pk).update(is_active=False)
        added, removed = sched.reconcile_schedules(self.scheduler)
        self.assertEqual((added, removed), (0, 1))
        self.assertEqual(self._job_ids(), set())

    def test_deleting_a_schedule_removes_its_job(self):
        s = SyncSchedule.objects.create(name='gone', is_active=True)
        sched.reconcile_schedules(self.scheduler)
        SyncSchedule.objects.filter(pk=s.pk).delete()

        _, removed = sched.reconcile_schedules(self.scheduler)
        self.assertEqual(removed, 1)
        self.assertEqual(self._job_ids(), set())

    def test_editing_the_frequency_reaches_the_trigger(self):
        """Not just presence — the job has to be REBUILT, not left alone
        because its ID already exists."""
        s = SyncSchedule.objects.create(name='n', frequency='DAILY', hour=6,
                                        is_active=True)
        sched.reconcile_schedules(self.scheduler)
        first = self.scheduler.get_job(sched.job_id_for(s.id)).trigger
        self.assertIsInstance(first, CronTrigger)

        SyncSchedule.objects.filter(pk=s.pk).update(frequency='HOURLY')
        sched.reconcile_schedules(self.scheduler)
        second = self.scheduler.get_job(sched.job_id_for(s.id)).trigger
        self.assertIsInstance(second, IntervalTrigger)

    def test_reconciling_twice_changes_nothing(self):
        """It runs every 60 seconds for the life of the worker, so drift or
        duplication here compounds rather than staying put."""
        SyncSchedule.objects.create(name='a', is_active=True)
        SyncSchedule.objects.create(name='b', is_active=True)
        sched.reconcile_schedules(self.scheduler)
        before = self._job_ids()
        sched.reconcile_schedules(self.scheduler)
        self.assertEqual(self._job_ids(), before)
        self.assertEqual(len(before), 2)

    def test_reconciling_leaves_foreign_jobs_alone(self):
        """The worker's own reconcile job lives on the same scheduler. If the
        prefix filter were dropped, the first reconcile would delete the job
        that schedules reconciliation and the worker would go quiet without
        ever exiting."""
        self.scheduler.add_job(lambda: None, 'interval', hours=1,
                               id='sap_sync_reconcile')
        SyncSchedule.objects.create(name='a', is_active=True)
        sched.reconcile_schedules(self.scheduler)
        self.assertIn('sap_sync_reconcile', self._job_ids())

    def test_next_run_is_written_back(self):
        """The admin and `/api/sap-sync/schedules/` both display `next_run`.
        It is only meaningful if something maintains it."""
        s = SyncSchedule.objects.create(name='n', frequency='HOURLY',
                                        is_active=True)
        self.assertIsNone(s.next_run)
        sched.reconcile_schedules(self.scheduler)
        s.refresh_from_db()
        self.assertIsNotNone(s.next_run)


class RunScheduledSyncTests(TestCase):

    def test_the_sync_type_selects_the_matching_service_call(self):
        cases = [
            ('PRODUCT', 'sync_products'),
            ('PARTY', 'sync_parties'),
            ('PARTY_ADDRESS', 'sync_party_addresses'),
            ('ALL', 'sync_all'),
        ]
        for sync_type, method in cases:
            with self.subTest(sync_type=sync_type):
                s = SyncSchedule.objects.create(name=sync_type, is_active=True,
                                                sync_type=sync_type)
                with patch.object(sched, 'SyncService') as service:
                    sched.run_scheduled_sync(s.id)
                getattr(service.return_value, method).assert_called_once_with()

    def test_an_unknown_sync_type_falls_back_to_sync_all(self):
        s = SyncSchedule.objects.create(name='x', is_active=True,
                                        sync_type='NONSENSE')
        with patch.object(sched, 'SyncService') as service:
            sched.run_scheduled_sync(s.id)
        service.return_value.sync_all.assert_called_once_with()

    def test_the_run_is_marked_as_scheduled(self):
        """`triggered_by` is how a scheduled run is told apart from a human
        pressing Sync — and the field that proved the old scheduler had never
        fired."""
        s = SyncSchedule.objects.create(name='x', is_active=True)
        with patch.object(sched, 'SyncService') as service:
            sched.run_scheduled_sync(s.id)
        service.assert_called_once_with(triggered_by='scheduled')

    def test_an_inactive_schedule_is_skipped_even_if_its_job_survives(self):
        """Belt and braces against the reconcile window: a schedule switched
        off 10 seconds ago still has a live job for up to a minute."""
        s = SyncSchedule.objects.create(name='x', is_active=False)
        with patch.object(sched, 'SyncService') as service:
            sched.run_scheduled_sync(s.id)
        service.assert_not_called()

    def test_a_missing_schedule_does_not_raise(self):
        with patch.object(sched, 'SyncService') as service:
            sched.run_scheduled_sync(999999)
        service.assert_not_called()

    def test_a_failing_sync_does_not_stamp_last_run(self):
        """`last_run` drives 'when did this last work'. Stamping it after a
        failure makes a dead sync look healthy."""
        s = SyncSchedule.objects.create(name='x', is_active=True)
        with patch.object(sched, 'SyncService') as service:
            service.return_value.sync_all.side_effect = RuntimeError('HANA down')
            with self.assertLogs('sap_sync.scheduler', level='ERROR'):
                sched.run_scheduled_sync(s.id)   # must not propagate
        s.refresh_from_db()
        self.assertIsNone(s.last_run)

    def test_a_successful_sync_stamps_last_run(self):
        s = SyncSchedule.objects.create(name='x', is_active=True)
        with patch.object(sched, 'SyncService'):
            sched.run_scheduled_sync(s.id)
        s.refresh_from_db()
        self.assertIsNotNone(s.last_run)


class WorkerCommandTests(TestCase):

    def test_once_reconciles_and_returns(self):
        SyncSchedule.objects.create(name='a', is_active=True)
        with patch('sap_sync.management.commands.run_scheduler'
                   '.acquire_singleton_lock', return_value=True), \
             patch('sap_sync.management.commands.run_scheduler'
                   '.release_singleton_lock'):
            call_command('run_scheduler', '--once')

    def test_a_second_worker_refuses_to_start(self):
        """The whole point of the lock. If this ever passes silently, two
        workers run every sync twice — which is the failure mode the plan
        attributed to the old design."""
        with patch('sap_sync.management.commands.run_scheduler'
                   '.acquire_singleton_lock', return_value=False):
            with self.assertRaises(CommandError):
                call_command('run_scheduler', '--once')

    def test_a_non_postgres_backend_is_refused_rather_than_failing_open(self):
        """The guard cannot be expressed without advisory locks, so the worker
        stops instead of running unguarded. Returning True here would be the
        worst outcome: two schedulers, no error."""
        with patch('sap_sync.management.commands.run_scheduler.connection') as conn:
            conn.vendor = 'sqlite'
            with self.assertRaises(CommandError):
                cmd_module.acquire_singleton_lock()

    def test_the_lock_is_session_scoped_not_transaction_scoped(self):
        """`pg_try_advisory_xact_lock` would release at the end of the
        acquiring statement's transaction — i.e. immediately — and the guard
        would silently protect nothing."""
        source = inspect.getsource(cmd_module)
        self.assertIn('pg_try_advisory_lock', source)
        self.assertNotIn('pg_try_advisory_xact_lock', source)

    def test_the_lock_is_actually_taken_and_released(self):
        """Against the real database, not a mock — the lock is only worth
        anything if Postgres agrees it exists.

        Skipped under the SQLite test database, which is most runs. That is a
        genuine gap and the reason `acquire_singleton_lock` refuses to pretend
        on a non-Postgres backend rather than failing open.
        """
        from django.db import connection

        if connection.vendor != 'postgresql':
            self.skipTest('advisory locks are a Postgres feature')

        from sap_sync.management.commands.run_scheduler import (
            ADVISORY_LOCK_KEY, acquire_singleton_lock, release_singleton_lock,
        )

        def held():
            with connection.cursor() as c:
                c.execute(
                    'SELECT count(*) FROM pg_locks WHERE locktype = %s '
                    'AND objid = %s AND pid = pg_backend_pid()',
                    ['advisory', ADVISORY_LOCK_KEY])
                return c.fetchone()[0]

        self.assertEqual(held(), 0)
        self.assertTrue(acquire_singleton_lock())
        try:
            self.assertEqual(held(), 1)
        finally:
            release_singleton_lock()
        self.assertEqual(held(), 0)


class NoSchedulerInTheWebProcessTests(TestCase):
    """The regression guard for 5.1.

    Every one of these would have passed before the refactor except the first,
    which is the one that matters: a scheduler started from `ready()` runs in
    whatever process happens to import Django, and no amount of care inside
    `scheduler.py` can fix that from where it sits.
    """

    def test_the_app_config_starts_nothing(self):
        from sap_sync.apps import SapSyncConfig

        # `__dict__`, not `hasattr`: AppConfig itself defines a no-op
        # `ready()`, so `hasattr` is True for every app config ever written.
        # The first draft used `hasattr` and failed against correct code.
        self.assertNotIn(
            'ready', SapSyncConfig.__dict__,
            'sap_sync.apps.SapSyncConfig defines ready() again — a scheduler '
            'started from there runs once per web worker, and in production '
            'runs zero times or N times depending on the WSGI server. Start '
            'it from `manage.py run_scheduler` instead.')

    def test_no_app_config_in_this_project_starts_a_scheduler(self):
        """Broader than the class above: the same mistake in any other app's
        `ready()` has the same consequence."""
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob('*/apps.py'):
            if '.venv' in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == 'ready':
                    body = ast.dump(node)
                    if 'scheduler' in body.lower() or 'Thread' in body:
                        offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f'background work started from ready(): {offenders}')

    def test_the_scheduler_module_has_no_module_level_scheduler(self):
        """A module-level `BackgroundScheduler()` is constructed on import,
        which means merely importing this module from a web worker — as
        `views.py` or a serializer might — recreates the old coupling."""
        source = Path(sched.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                self.assertNotIn(
                    'Scheduler',
                    ast.dump(node.value),
                    'sap_sync/scheduler.py constructs a scheduler at import time')

    def test_every_scheduler_entry_point_takes_the_scheduler_as_an_argument(self):
        """The structural reason the module cannot regrow a singleton: with no
        module-level scheduler to reach for, each function must be handed one,
        and the only caller that can hand one over is the worker."""
        for name in ('add_schedule_job', 'remove_schedule_job',
                     'reconcile_schedules'):
            with self.subTest(function=name):
                params = list(inspect.signature(getattr(sched, name)).parameters)
                self.assertEqual(params[0], 'scheduler')

    def test_the_worker_command_is_registered(self):
        """`call_command` on a missing command raises, so this also proves the
        `management/commands` package is importable — a missing `__init__.py`
        makes the command silently invisible."""
        from django.core.management import get_commands

        self.assertEqual(get_commands().get('run_scheduler'), 'sap_sync')
