"""The sweep runs on a schedule, so nobody has to notice a lost SAP call.

`recover_stranded_sap_posts` has always been able to fix a document whose
approval landed but whose `on_commit` SAP call was lost to a restart. Nothing
ran it. The failure is CAUSED by a restart, which is precisely when no one is
watching, so a manual command was never going to be enough — observed on
RCP-OIL-20260919-000003, approved with zero SAP call logs and left unposted.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from . import scheduler_jobs


class SweepIntervalTests(SimpleTestCase):
    def test_the_default_is_used_when_nothing_is_configured(self):
        self.assertEqual(scheduler_jobs.sweep_interval_seconds(),
                         scheduler_jobs.DEFAULT_SWEEP_INTERVAL_SECONDS)

    @override_settings(PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS=60)
    def test_it_is_configurable(self):
        self.assertEqual(scheduler_jobs.sweep_interval_seconds(), 60)

    @override_settings(PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS=0)
    def test_zero_is_floored_rather_than_crashing_the_worker(self):
        """IntervalTrigger raises on a non-positive interval, and that
        exception would take down the whole scheduler at startup over a typo
        in the environment."""
        self.assertEqual(scheduler_jobs.sweep_interval_seconds(), 30)

    @override_settings(PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS=-99)
    def test_a_negative_is_floored_too(self):
        self.assertEqual(scheduler_jobs.sweep_interval_seconds(), 30)

    @override_settings(PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS='not a number')
    def test_nonsense_falls_back_to_the_default(self):
        self.assertEqual(scheduler_jobs.sweep_interval_seconds(),
                         scheduler_jobs.DEFAULT_SWEEP_INTERVAL_SECONDS)


class SweepTests(SimpleTestCase):
    """The job body. It must never raise — see its docstring."""

    def test_a_failure_is_logged_and_swallowed(self):
        """An exception escaping the job keeps its schedule but prints a
        traceback every interval, burying everything else in the log."""
        with patch('payments.sap_recovery.recover_stranded_posts',
                   side_effect=RuntimeError('database is on fire')):
            self.assertIsNone(scheduler_jobs.sweep_stranded_posts())

    def test_a_quiet_sweep_returns_its_summary(self):
        empty = {'receipts_found': 0, 'receipts_posted': 0,
                 'deposits_found': 0, 'deposits_posted': 0,
                 'errors': 0, 'details': []}
        with patch('payments.sap_recovery.recover_stranded_posts',
                   return_value=empty) as sweep:
            self.assertIs(scheduler_jobs.sweep_stranded_posts(), empty)
        sweep.assert_called_once_with()

    def test_a_recovery_is_reported(self):
        found = {'receipts_found': 1, 'receipts_posted': 1,
                 'deposits_found': 0, 'deposits_posted': 0,
                 'errors': 0, 'details': ['RCP-OIL-1: posted']}
        with patch('payments.sap_recovery.recover_stranded_posts',
                   return_value=found):
            with self.assertLogs('payments.scheduler_jobs', 'WARNING') as logs:
                scheduler_jobs.sweep_stranded_posts()
        self.assertIn('RCP-OIL-1: posted', str(logs.output))


class RegisterTests(SimpleTestCase):
    def setUp(self):
        self.scheduler = MagicMock()

    def test_it_sweeps_IMMEDIATELY_not_after_one_interval(self):
        """The restart that just happened is itself a cause of stranding, so
        the first moment worth sweeping is the moment the worker comes back."""
        with patch.object(scheduler_jobs, 'sweep_stranded_posts') as sweep:
            scheduler_jobs.register(self.scheduler)
        sweep.assert_called_once_with()

    def test_the_immediate_run_can_be_suppressed(self):
        with patch.object(scheduler_jobs, 'sweep_stranded_posts') as sweep:
            scheduler_jobs.register(self.scheduler, run_now=False)
        sweep.assert_not_called()

    def test_the_job_cannot_stack_on_itself(self):
        """Two sweeps in flight could put two posts in flight for the SAME
        document, which is the one outcome every guard in sap_recovery
        exists to prevent."""
        with patch.object(scheduler_jobs, 'sweep_stranded_posts'):
            scheduler_jobs.register(self.scheduler)
        kwargs = self.scheduler.add_job.call_args.kwargs
        self.assertEqual(kwargs['max_instances'], 1)
        self.assertTrue(kwargs['coalesce'])

    def test_it_has_a_fixed_id_so_restarts_do_not_accumulate_copies(self):
        with patch.object(scheduler_jobs, 'sweep_stranded_posts'):
            scheduler_jobs.register(self.scheduler)
        kwargs = self.scheduler.add_job.call_args.kwargs
        self.assertEqual(kwargs['id'], scheduler_jobs.SWEEP_JOB_ID)
        self.assertTrue(kwargs['replace_existing'])

    @override_settings(PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS=90)
    def test_the_configured_interval_reaches_the_job(self):
        with patch.object(scheduler_jobs, 'sweep_stranded_posts'):
            scheduler_jobs.register(self.scheduler)
        self.assertEqual(self.scheduler.add_job.call_args.kwargs['seconds'], 90)
