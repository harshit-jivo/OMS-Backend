"""Health and readiness — Phase 5.3.

The hard part of testing a health check is that the interesting cases are the
ones where a dependency is DOWN, and the test suite runs against a database
that is up and an SAP that is unreachable. So the probes are swapped for fakes
and what is asserted is the decision logic on top of them: which failures mean
503, which mean "degraded but still serving", and what an anonymous caller is
allowed to be told.

Two of these endpoints answer the public internet. Three tests exist purely to
pin that boundary, because the failure mode is silent — a leaked hostname in
an error string looks exactly like a working health check.

Run with::

    python manage.py test core.tests_health --settings=OMS.test_settings
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core import health

User = get_user_model()


def _check(name, criticality, probe):
    return health.Check(name, criticality, probe)


def _up():
    return True


def _down():
    raise RuntimeError('connection refused to 10.0.0.5:30015 as SYSTEM')


def _absent():
    return health.NOT_CONFIGURED


class CheckTests(TestCase):

    def test_a_healthy_probe_reports_up(self):
        result = _check('x', health.CRITICAL, _up).run()
        self.assertEqual(result['status'], health.UP)
        self.assertNotIn('error', result)

    def test_a_failing_probe_is_caught_not_raised(self):
        """A health endpoint that 500s during an outage tells the monitor
        nothing except that the health endpoint is broken."""
        result = _check('x', health.CRITICAL, _down).run()
        self.assertEqual(result['status'], health.DOWN)
        self.assertIn('connection refused', result['error'])

    def test_an_unconfigured_dependency_is_skipped_not_failed(self):
        """CI and the test database have no HANA. Reporting that as DOWN would
        make the check permanently red and therefore permanently ignored."""
        result = _check('x', health.DEGRADED, _absent).run()
        self.assertEqual(result['status'], health.SKIPPED)

    def test_latency_is_recorded_even_for_a_failure(self):
        """A dependency that is up but slow and one that is down look the same
        in a status field alone; the timeout is the distinguishing evidence."""
        self.assertIn('latency_ms', _check('x', health.CRITICAL, _down).run())


class OverallStatusTests(TestCase):

    def test_all_up_is_up(self):
        overall, _ = health.run_checks([_check('a', health.CRITICAL, _up)])
        self.assertEqual(overall, health.UP)

    def test_a_critical_failure_is_down(self):
        overall, _ = health.run_checks([_check('pg', health.CRITICAL, _down)])
        self.assertEqual(overall, health.DOWN)

    def test_a_degraded_failure_is_not_down(self):
        """The load-balancer decision. A HANA outage must not take every node
        out of rotation — orders, tracker, approvals and payments all live in
        Postgres and keep working."""
        overall, _ = health.run_checks([
            _check('pg', health.CRITICAL, _up),
            _check('hana', health.DEGRADED, _down),
        ])
        self.assertEqual(overall, health.DEGRADED)

    def test_a_skipped_dependency_does_not_degrade_anything(self):
        overall, _ = health.run_checks([
            _check('pg', health.CRITICAL, _up),
            _check('hana', health.DEGRADED, _absent),
        ])
        self.assertEqual(overall, health.UP)

    def test_every_check_runs_even_after_one_fails(self):
        """During an incident the second failure is often the informative one.
        Short-circuiting would report the first problem and hide the rest."""
        ran = []

        def record(name):
            def probe():
                ran.append(name)
                return True
            return probe

        health.run_checks([
            _check('a', health.CRITICAL, _down),
            _check('b', health.DEGRADED, record('b')),
            _check('c', health.DEGRADED, record('c')),
        ])
        self.assertEqual(ran, ['b', 'c'])

    def test_every_registered_check_declares_its_criticality(self):
        """The field that decides whether an outage empties the load balancer.
        A new dependency must not inherit an answer by accident."""
        for check in health.CHECKS:
            with self.subTest(check=check.name):
                self.assertIn(check.criticality, {health.CRITICAL, health.DEGRADED})

    def test_postgres_is_the_only_critical_dependency(self):
        """Pinned deliberately. Promoting an external system to CRITICAL means
        a SAP outage becomes an OMS outage, which is a decision worth making
        on purpose rather than by editing one word."""
        critical = [c.name for c in health.CHECKS if c.criticality == health.CRITICAL]
        self.assertEqual(critical, ['postgres'])


class LivenessEndpointTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_it_answers_anonymously(self):
        response = self.client.get(reverse('health-live'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': health.UP})

    def test_it_touches_no_dependency(self):
        """The distinction from readiness, and the reason both exist. A
        liveness probe wired to the database reports every node as dead during
        a database outage, and the orchestrator restarts all of them — which
        does not fix a database."""
        with patch.object(health, 'run_checks') as run_checks:
            self.client.get(reverse('health-live'))
        run_checks.assert_not_called()


class ReadinessEndpointTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_it_answers_anonymously(self):
        with patch.object(health, 'CHECKS', [_check('pg', health.CRITICAL, _up)]):
            response = self.client.get(reverse('health-ready'))
        self.assertEqual(response.status_code, 200)

    def test_a_critical_outage_returns_503(self):
        with patch.object(health, 'CHECKS', [_check('pg', health.CRITICAL, _down)]):
            response = self.client.get(reverse('health-ready'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], health.DOWN)

    def test_a_degraded_dependency_still_returns_200(self):
        with patch.object(health, 'CHECKS', [
            _check('pg', health.CRITICAL, _up),
            _check('hana', health.DEGRADED, _down),
        ]):
            response = self.client.get(reverse('health-ready'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], health.DEGRADED)
        self.assertEqual(response.json()['checks']['hana'], health.DOWN)

    def test_it_leaks_no_diagnostic_detail_to_an_anonymous_caller(self):
        """The reason this endpoint is terse. `_down` raises a message
        carrying a host, a port and a database username — exactly the shape of
        a real driver error — and none of it may appear in the body."""
        with patch.object(health, 'CHECKS', [_check('pg', health.CRITICAL, _down)]):
            response = self.client.get(reverse('health-ready'))
        body = response.content.decode()
        for secret in ('10.0.0.5', '30015', 'SYSTEM', 'RuntimeError',
                       'connection refused'):
            self.assertNotIn(secret, body, f'{secret!r} leaked to an anonymous caller')

    def test_the_body_carries_component_names_and_nothing_else(self):
        with patch.object(health, 'CHECKS', [_check('pg', health.CRITICAL, _up)]):
            body = self.client.get(reverse('health-ready')).json()
        self.assertEqual(set(body), {'status', 'checks'})
        self.assertEqual(body['checks'], {'pg': health.UP})


class HealthDetailEndpointTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_it_is_not_public(self):
        self.assertEqual(self.client.get(reverse('health-detail')).status_code, 401)

    def test_a_non_admin_is_refused(self):
        user = User.objects.create_user(username='clerk', password='x')
        self.client.force_authenticate(user=user)
        self.assertEqual(self.client.get(reverse('health-detail')).status_code, 403)

    def test_an_admin_gets_the_diagnosis(self):
        """The counterpart to the leak test above: the error text has to exist
        SOMEWHERE, or an outage is only ever observable as a number."""
        admin = User.objects.create_superuser(username='boss', password='x')
        self.client.force_authenticate(user=admin)
        with patch.object(health, 'CHECKS', [_check('pg', health.CRITICAL, _down)]):
            body = self.client.get(reverse('health-detail')).json()
        self.assertEqual(body['status'], health.DOWN)
        self.assertIn('connection refused', body['checks'][0]['error'])


class RealProbeTests(TestCase):
    """The probes themselves, against whatever this environment actually has.

    Deliberately tolerant about the RESULT — HANA and the Service Layer are
    unreachable from CI and that is not a test failure. What is asserted is
    that each probe returns a well-formed verdict instead of raising, because
    a probe that raises takes the whole health endpoint down with it.
    """

    def test_postgres_probe_succeeds_against_the_test_database(self):
        self.assertIs(health.probe_postgres(), True)

    def test_every_probe_returns_a_verdict_rather_than_raising(self):
        for check in health.CHECKS:
            with self.subTest(check=check.name):
                result = check.run()
                self.assertIn(result['status'],
                              {health.UP, health.DOWN, health.SKIPPED})

    def test_the_scheduler_probe_is_quiet_when_nothing_is_scheduled(self):
        """There are no active schedules in production today, so this is the
        live case. Reporting DOWN because an idle system is idle would be a
        false alarm on every poll."""
        from sap_sync.models import SyncSchedule

        self.assertFalse(SyncSchedule.objects.filter(is_active=True).exists())
        verdict = health.probe_scheduler()
        self.assertIsInstance(verdict, health.Skip)
        # The reason must say what is actually true. Reporting "not configured"
        # for an idle scheduler would send whoever reads it to the wrong place.
        self.assertEqual(str(verdict), 'no active schedules')

    def test_the_service_layer_probe_does_not_log_in(self):
        """A login per poll would burn SAP's concurrent-session licence and
        evict the sessions real orders are using. Asserted structurally,
        because the behavioural version would need a live Service Layer.

        The docstring is stripped before matching. The first version of this
        test scanned the raw source and failed on the probe's OWN docstring,
        which explains at length why it does not log in — a check that reads
        prose as if it were code is not checking anything.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(health.probe_service_layer).lstrip())
        function = tree.body[0]
        if (function.body and isinstance(function.body[0], ast.Expr)
                and isinstance(function.body[0].value, ast.Constant)):
            function.body = function.body[1:]

        code = ast.unparse(function).lower()
        self.assertNotIn('login', code)
        self.assertNotIn('sapservicelayermanager', code)
