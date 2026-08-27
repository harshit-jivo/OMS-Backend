"""Error tracking — Phase 5.4.

Almost all of this is about what must NOT be sent.

An error tracker posts the contents of a failing request to a third party.
This API's requests carry SAP Service Layer credentials, GSTINs, party master
data, invoice totals and NIC e-invoice tokens, so the scrubber is the load-
bearing part of the integration and the `init` call is nearly trivial by
comparison. The tests are weighted accordingly.

Nothing here contacts Sentry, and `sentry_sdk` is not imported at module
level — the suite has to pass on a machine where it is not installed, which
is the same property the application itself needs.

Run with::

    python manage.py test core.tests_error_tracking --settings=OMS.test_settings
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from core import error_tracking, request_context
from core.error_tracking import REDACTED, before_send


def _settings(**overrides):
    return SimpleNamespace(**{
        'SENTRY_DSN': '',
        'SENTRY_ENVIRONMENT': 'test',
        'SENTRY_RELEASE': '',
        'SENTRY_TRACES_SAMPLE_RATE': 0.0,
        **overrides,
    })


class OptInTests(TestCase):

    def test_nothing_initialises_without_a_dsn(self):
        """The default, and what CI and every developer machine run under."""
        self.assertFalse(error_tracking.init(_settings()))

    def test_a_blank_dsn_is_treated_as_absent(self):
        for dsn in ('', '   ' and '', None):
            with self.subTest(dsn=repr(dsn)):
                self.assertFalse(error_tracking.init(_settings(SENTRY_DSN=dsn)))

    def test_a_dsn_initialises_the_sdk(self):
        with patch.dict('sys.modules'):
            with patch('sentry_sdk.init') as sentry_init:
                started = error_tracking.init(
                    _settings(SENTRY_DSN='https://k@example.invalid/1'))
        self.assertTrue(started)
        sentry_init.assert_called_once()

    def test_pii_is_never_sent_by_default(self):
        """The single most consequential setting on the init call. Sentry's own
        documentation encourages turning this on; here it would attach the
        Authorization header — a live JWT — and the body of
        `/api/orders/submit/`, which is a customer's order."""
        with patch('sentry_sdk.init') as sentry_init:
            error_tracking.init(_settings(SENTRY_DSN='https://k@example.invalid/1'))
        self.assertIs(sentry_init.call_args.kwargs['send_default_pii'], False)

    def test_tracing_is_off_unless_asked_for(self):
        """Performance tracing samples spans including SQL text from live
        requests, and is billed per event."""
        with patch('sentry_sdk.init') as sentry_init:
            error_tracking.init(_settings(SENTRY_DSN='https://k@example.invalid/1'))
        self.assertEqual(sentry_init.call_args.kwargs['traces_sample_rate'], 0.0)

    def test_the_scrubber_is_wired_in(self):
        """A correct scrubber that is not passed to `init` protects nothing."""
        with patch('sentry_sdk.init') as sentry_init:
            error_tracking.init(_settings(SENTRY_DSN='https://k@example.invalid/1'))
        self.assertIs(sentry_init.call_args.kwargs['before_send'], before_send)

    def test_a_missing_sdk_does_not_stop_the_application(self):
        """`sentry-sdk` is in requirements.txt, but a deployment that has not
        reinstalled yet must still boot. Loud in the log, harmless otherwise."""
        import builtins

        # `builtins.__import__`, not the `__builtins__` module global. The
        # latter is a dict in some contexts and a module in others, and
        # `core.tests.UnresolvedNameTests` correctly flags it as a name that
        # is never imported — the guard caught this file.
        real_import = builtins.__import__

        def no_sentry(name, *args, **kwargs):
            if name.startswith('sentry_sdk'):
                raise ImportError('no sentry_sdk')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=no_sentry):
            with self.assertLogs('core.error_tracking', level='ERROR'):
                started = error_tracking.init(
                    _settings(SENTRY_DSN='https://k@example.invalid/1'))
        self.assertFalse(started)

    def test_a_malformed_dsn_does_not_stop_the_application(self):
        """`init` runs at import time in settings.py, so anything it raises
        stops the process from starting — an error tracker that can do that
        has inverted its own purpose.

        `sentry_sdk.init` raises `BadDsn` on a malformed DSN — assumed
        otherwise when this was written, and this test is what proved it. A
        typo in `.env` would have taken the whole deployment down.
        """
        with self.assertLogs('core.error_tracking', level='ERROR'):
            started = error_tracking.init(_settings(SENTRY_DSN='not-a-url'))
        self.assertFalse(started)

    def test_an_sdk_failure_of_any_kind_is_contained(self):
        """Broader than the DSN case: nothing `sentry_sdk.init` can throw may
        reach settings.py."""
        with patch('sentry_sdk.init', side_effect=RuntimeError('anything')):
            with self.assertLogs('core.error_tracking', level='ERROR'):
                started = error_tracking.init(
                    _settings(SENTRY_DSN='https://k@example.invalid/1'))
        self.assertFalse(started)


class ScrubbingTests(TestCase):
    """The load-bearing part. A false positive costs one redacted field in a
    bug report; a false negative costs a credential on a third party's disk."""

    def test_a_password_key_is_redacted(self):
        event = before_send({'extra': {'password': 'Jivo@123'}}, None)
        self.assertEqual(event['extra']['password'], REDACTED)
        self.assertNotIn('Jivo@123', str(event))

    def test_every_sensitive_key_name_is_covered(self):
        for key in ('password', 'PASSWORD', 'db_passwd', 'SECRET_KEY',
                    'access_token', 'authorization', 'api_key', 'apikey',
                    'B1SESSION', 'sessionId', 'private_key', 'gstin',
                    'user_credential', 'signature', 'otp'):
            with self.subTest(key=key):
                event = before_send({'extra': {key: 'SENSITIVE-VALUE'}}, None)
                self.assertNotIn('SENSITIVE-VALUE', str(event),
                                 f'{key} was not redacted')

    def test_nested_structures_are_scrubbed(self):
        event = before_send({
            'extra': {'sap': {'login': {'Password': 'hunter2'}}},
        }, None)
        self.assertNotIn('hunter2', str(event))

    def test_lists_are_scrubbed(self):
        event = before_send({'extra': {'attempts': [{'token': 'abc123xyz'}]}}, None)
        self.assertNotIn('abc123xyz', str(event))

    def test_a_jwt_in_free_text_is_redacted(self):
        """No key name to match on — this is how a leaked Authorization header
        usually escapes, quoted inside an exception message."""
        jwt = ('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
               'eyJ1c2VyX2lkIjoxLCJleHAiOjE3MDAwMDAwMDB9.'
               'dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w')
        event = before_send(
            {'message': f'auth failed for Bearer {jwt}'}, None)
        self.assertNotIn(jwt, str(event))
        self.assertIn(REDACTED, event['message'])

    def test_a_sap_session_cookie_in_free_text_is_redacted(self):
        event = before_send(
            {'message': 'sent Cookie: B1SESSION=abc-def-123; ROUTEID=.node1'},
            None)
        self.assertNotIn('abc-def-123', str(event))

    def test_an_inline_password_assignment_is_redacted(self):
        for text in ('POST body: {"password": "Jivo@!@#"}',
                     'connect user=SYSTEM password=Secret123',
                     'token: ghp_AAAABBBBCCCC'):
            with self.subTest(text=text):
                event = before_send({'message': text}, None)
                for secret in ('Jivo@!@#', 'Secret123', 'ghp_AAAABBBBCCCC'):
                    self.assertNotIn(secret, str(event))

    def test_the_query_string_is_dropped_entirely(self):
        """Not scrubbed — dropped. It carries IRNs, party codes and
        occasionally a token, and unlike a body it is not worth enough in a
        bug report to justify sending at all."""
        event = before_send(
            {'request': {'url': '/api/orders/', 'query_string': 'token=SECRET'}},
            None)
        self.assertNotIn('query_string', event['request'])
        self.assertNotIn('SECRET', str(event))

    def test_the_request_body_is_dropped(self):
        """`/api/orders/submit/` bodies are customer orders."""
        event = before_send(
            {'request': {'data': {'party': 'C001', 'lines': [{'rate': 100}]}}},
            None)
        self.assertNotIn('data', event['request'])

    def test_harmless_fields_survive(self):
        """A scrubber that redacts everything produces unusable bug reports,
        which is its own kind of failure."""
        event = before_send({
            'message': 'order 12345 failed validation',
            'extra': {'order_id': 12345, 'branch': 'OIL', 'stage': 'pre_audit'},
        }, None)
        self.assertEqual(event['extra']['order_id'], 12345)
        self.assertEqual(event['extra']['branch'], 'OIL')
        self.assertIn('order 12345', event['message'])

    def test_deep_nesting_terminates(self):
        """A cyclic or pathologically deep event would otherwise recurse until
        the process dies — while handling an error, which is the worst
        possible moment."""
        node = {'extra': {}}
        cursor = node['extra']
        for _ in range(200):
            cursor['next'] = {}
            cursor = cursor['next']
        cursor['password'] = 'deep'
        before_send(node, None)   # must return, not blow the stack

    def test_non_string_values_pass_through_unharmed(self):
        event = before_send({'extra': {
            'count': 7, 'ratio': 1.5, 'ok': True, 'nothing': None}}, None)
        self.assertEqual(event['extra'],
                         {'count': 7, 'ratio': 1.5, 'ok': True, 'nothing': None})


class RequestCorrelationTests(TestCase):

    def setUp(self):
        self.addCleanup(request_context.unbind, [None] * 4)

    def test_the_request_id_is_attached_as_a_tag(self):
        """So a Sentry issue and the lines in logs/oms.log are one incident
        rather than two."""
        tokens = request_context.bind(request_id='corr-123')
        try:
            event = before_send({'message': 'boom'}, None)
        finally:
            request_context.unbind(tokens)
        self.assertEqual(event['tags']['request_id'], 'corr-123')

    def test_existing_tags_are_preserved(self):
        tokens = request_context.bind(request_id='corr-123')
        try:
            event = before_send({'tags': {'module': 'orders'}}, None)
        finally:
            request_context.unbind(tokens)
        self.assertEqual(event['tags']['module'], 'orders')
        self.assertEqual(event['tags']['request_id'], 'corr-123')

    def test_no_tag_is_added_outside_a_request(self):
        event = before_send({'message': 'from a management command'}, None)
        self.assertNotIn('request_id', event.get('tags', {}))


class BeforeSendRobustnessTests(TestCase):

    def test_a_scrubber_failure_drops_the_event_rather_than_leaking_it(self):
        """Fail CLOSED. If the scrubber cannot run, the event has not been
        cleaned, and sending it unscrubbed is the one outcome that must not
        happen. Returning None tells the SDK to discard it.

        The SDK swallows exceptions from `before_send` and drops the event
        silently, so the failure is logged explicitly here — otherwise it
        looks like 'Sentry is quiet today'.
        """
        with patch.object(error_tracking, '_scrub',
                          side_effect=RuntimeError('scrubber broke')):
            with self.assertLogs('core.error_tracking', level='ERROR'):
                result = before_send({'message': 'secret stuff'}, None)
        self.assertIsNone(result)

    def test_an_event_without_a_request_is_handled(self):
        """Management commands, the scheduler worker and app-loading errors
        all produce events with no request attached."""
        self.assertIsNotNone(before_send({'message': 'from a cron job'}, None))


class SettingsWiringTests(TestCase):

    def test_the_dsn_setting_exists_and_defaults_to_empty(self):
        from django.conf import settings

        self.assertEqual(getattr(settings, 'SENTRY_DSN', None), '')

    def test_the_sdk_is_pinned_in_requirements(self):
        """It is imported conditionally, so nothing else would notice its
        absence until the day a DSN is configured."""
        from pathlib import Path

        requirements = (Path(__file__).resolve().parent.parent
                        / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('sentry-sdk', requirements)

    def test_logging_integration_turns_existing_calls_into_issues(self):
        """The reason this needs no call-site changes: every
        `logger.exception` already in the project becomes a Sentry issue."""
        with patch('sentry_sdk.init') as sentry_init:
            error_tracking.init(_settings(SENTRY_DSN='https://k@example.invalid/1'))
        integrations = sentry_init.call_args.kwargs['integrations']
        logging_integration = [
            i for i in integrations if type(i).__name__ == 'LoggingIntegration']
        self.assertEqual(len(logging_integration), 1)
        self.assertEqual(logging_integration[0]._handler.level, logging.ERROR)
