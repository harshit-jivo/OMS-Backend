"""Request correlation and structured logging — Phase 5.2.

The point of a request ID is that it appears on log lines nobody wrote it
into. So most of what follows captures real records through the real filter,
rather than asserting that a function returns a string.

Two areas get disproportionate attention:

* **Log injection.** `X-Request-ID` is caller-supplied and lands verbatim in
  every line of the request. That is a forgery primitive if it is not
  validated, and the resulting fake lines are indistinguishable from real ones
  after the fact.
* **Logging must never raise.** A formatter that throws on an unserialisable
  `extra=` value, or a context lookup that throws between requests, converts a
  successful response into a 500 from the logging layer. Several tests here
  exist only to pin that.

Run with::

    python manage.py test core.tests_logging --settings=OMS.test_settings
"""
import json
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core import request_context
from core.logging import JsonFormatter, RequestContextFilter
from core.middleware import HEADER, RequestContextMiddleware

User = get_user_model()


def _record(msg='hello', level=logging.INFO, **extra):
    record = logging.LogRecord('somewhere', level, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class RequestIdSanitisingTests(TestCase):
    """`sanitize_request_id` is a security control. See core/request_context.py."""

    def test_a_reasonable_id_is_honoured(self):
        for value in ('abc123', 'trace-42', 'a.b_c-D9', 'f' * 64):
            with self.subTest(value=value):
                self.assertEqual(request_context.sanitize_request_id(value), value)

    def test_a_newline_cannot_forge_a_log_line(self):
        """The attack: a header of `x\\n2026-01-01 ERROR admin: deleted all`
        writes a second line that looks exactly like a server-emitted one."""
        forged = 'x\n2026-01-01 00:00:00 ERROR    core: payment approved'
        result = request_context.sanitize_request_id(forged)
        self.assertNotIn('\n', result)
        self.assertNotIn('payment approved', result)

    def test_every_line_breaking_character_is_refused(self):
        for char in ('\n', '\r', '\x00', '\x1b', ' ', ' '):
            with self.subTest(char=repr(char)):
                self.assertNotIn(char, request_context.sanitize_request_id(f'a{char}b'))

    def test_an_overlong_id_is_refused(self):
        """Uncapped, a caller writes as many bytes per line as they like — into
        a size-capped rotating file, which then discards real history."""
        self.assertNotEqual(request_context.sanitize_request_id('a' * 65), 'a' * 65)

    def test_a_refused_id_is_replaced_not_repaired(self):
        """Stripping a bad value into shape would still correlate to whatever
        the caller believes it sent, which is worse than not honouring it."""
        result = request_context.sanitize_request_id('bad id with spaces')
        self.assertNotIn('bad', result)
        self.assertEqual(len(result), 32)

    def test_an_absent_header_gets_a_generated_id(self):
        self.assertEqual(len(request_context.sanitize_request_id('')), 32)

    def test_generated_ids_are_unique(self):
        ids = {request_context.new_request_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)


class ContextBindingTests(TestCase):

    def setUp(self):
        # A ContextVar outlives the test that set it. Without this, one test
        # that fails to unbind leaks its value into every test that runs after
        # it in the same context — which is how the `unbind` TypeError bug
        # above first showed up: as four unrelated failures.
        self.addCleanup(request_context.unbind, [None] * 4)

    def test_context_is_empty_outside_a_request(self):
        """Management commands, the shell and the scheduler worker all log."""
        self.assertEqual(request_context.get_request_id(), '')

    def test_unbinding_restores_the_previous_value(self):
        """A ContextVar, not a thread-local, precisely so this holds. In a
        pooled worker a thread-local keeps the last request's ID until
        something overwrites it, and a line logged in between is attributed to
        whoever was served last."""
        tokens = request_context.bind(request_id='first')
        self.assertEqual(request_context.get_request_id(), 'first')
        request_context.unbind(tokens)
        self.assertEqual(request_context.get_request_id(), '')

    def test_unbinding_a_foreign_token_does_not_raise(self):
        """Happens when a response is produced on a different thread than the
        one that bound the context. Raising here would replace the real
        outcome with a confusing error from the logging layer."""
        request_context.bind(request_id='x')
        request_context.unbind(['not-a-token'])
        self.assertEqual(request_context.get_request_id(), '')


class FilterTests(TestCase):

    def setUp(self):
        self.filter = RequestContextFilter()
        self.addCleanup(request_context.unbind, [None] * 4)

    def test_the_filter_never_rejects_a_record(self):
        """It exists to enrich, not to drop. Returning a falsy value would
        silently discard the log line."""
        self.assertTrue(self.filter.filter(_record()))

    def test_the_request_id_reaches_a_record_nobody_annotated(self):
        """The whole design. Several hundred existing `logger.*` calls gain
        correlation without one of them changing."""
        tokens = request_context.bind(request_id='abcdef1234')
        try:
            record = _record()
            self.filter.filter(record)
        finally:
            request_context.unbind(tokens)
        self.assertEqual(record.request_id, 'abcdef1234')

    def test_fields_are_present_even_outside_a_request(self):
        """`{context_tag}` in the format string raises `ValueError` on a record
        that lacks the attribute, and it raises AT THE POINT OF LOGGING — so a
        management command would crash while writing a log line."""
        record = _record()
        self.filter.filter(record)
        for field in ('request_id', 'user_id', 'path', 'method', 'context_tag'):
            self.assertTrue(hasattr(record, field), f'{field} missing')

    def test_the_tag_is_readable_outside_a_request(self):
        record = _record()
        self.filter.filter(record)
        self.assertEqual(record.context_tag, '[-]')

    def test_an_explicit_extra_beats_the_ambient_context(self):
        """A call site that knows better — logging on behalf of another user,
        say — must not be overwritten by the request it happens to run in."""
        tokens = request_context.bind(request_id='ambient', user_id='1')
        try:
            record = _record(user_id='99')
            self.filter.filter(record)
        finally:
            request_context.unbind(tokens)
        self.assertEqual(record.user_id, '99')

    def test_the_configured_format_strings_render_a_plain_record(self):
        """The failure this catches is total: a format string referencing a
        field the filter does not set breaks EVERY log line in the project, and
        only at runtime."""
        for name in ('standard', 'console'):
            with self.subTest(formatter=name):
                spec = settings.LOGGING['formatters'][name]
                formatter = logging.Formatter(spec['format'], style=spec['style'])
                record = _record()
                self.filter.filter(record)
                self.assertIn('hello', formatter.format(record))


class JsonFormatterTests(TestCase):

    def setUp(self):
        self.formatter = JsonFormatter()
        self.filter = RequestContextFilter()

    def test_it_emits_one_json_object_per_line(self):
        output = self.formatter.format(_record())
        self.assertNotIn('\n', output)
        self.assertEqual(json.loads(output)['message'], 'hello')

    def test_context_fields_are_top_level_keys(self):
        tokens = request_context.bind(request_id='rid1', path='/api/x',
                                      method='POST')
        try:
            record = _record()
            self.filter.filter(record)
            payload = json.loads(self.formatter.format(record))
        finally:
            request_context.unbind(tokens)
        self.assertEqual(payload['request_id'], 'rid1')
        self.assertEqual(payload['path'], '/api/x')
        self.assertEqual(payload['method'], 'POST')

    def test_an_empty_context_field_is_omitted_entirely(self):
        """`"user_id": ""` on every anonymous request is noise in a format
        whose whole advantage is that a field's presence means something."""
        record = _record()
        self.filter.filter(record)
        payload = json.loads(self.formatter.format(record))
        self.assertNotIn('user_id', payload)
        self.assertNotIn('request_id', payload)

    def test_extra_fields_are_included_without_being_enumerated(self):
        payload = json.loads(self.formatter.format(
            _record(duration_ms=12.5, status=200)))
        self.assertEqual(payload['duration_ms'], 12.5)
        self.assertEqual(payload['status'], 200)

    def test_an_unserialisable_extra_does_not_raise(self):
        """`extra=` takes arbitrary objects — a Decimal, a model instance, a
        datetime. Without `default=str` the dump raises INSIDE the logging
        call, turning a log line into an application error."""
        from decimal import Decimal

        class Opaque:
            def __repr__(self):
                return '<opaque>'

        payload = json.loads(self.formatter.format(
            _record(amount=Decimal('1.50'), thing=Opaque())))
        self.assertEqual(payload['amount'], '1.50')
        self.assertEqual(payload['thing'], '<opaque>')

    def test_an_exception_is_rendered_into_the_object(self):
        try:
            raise ValueError('boom')
        except ValueError:
            import sys
            record = _record(level=logging.ERROR)
            record.exc_info = sys.exc_info()
        payload = json.loads(self.formatter.format(record))
        self.assertIn('ValueError: boom', payload['exception'])

    def test_non_ascii_survives_intact(self):
        """Party names in this system are not all ASCII, and `\\uXXXX` escapes
        make a log unreadable exactly when someone is reading it."""
        payload = json.loads(self.formatter.format(_record('जीवो मार्ट')))
        self.assertEqual(payload['message'], 'जीवो मार्ट')


class MiddlewareTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        self.addCleanup(request_context.unbind, [None] * 4)

    def _run(self, request, view=None):
        view = view or (lambda r: __import__('django.http', fromlist=['HttpResponse'])
                        .HttpResponse('ok'))
        return RequestContextMiddleware(view)(request)

    def test_the_response_carries_the_request_id(self):
        response = self._run(self.factory.get('/api/orders/'))
        self.assertEqual(len(response[HEADER]), 32)

    def test_a_caller_supplied_id_is_echoed_back(self):
        response = self._run(
            self.factory.get('/api/orders/', HTTP_X_REQUEST_ID='trace-9'))
        self.assertEqual(response[HEADER], 'trace-9')

    def test_a_hostile_id_is_not_echoed_back(self):
        response = self._run(self.factory.get(
            '/api/orders/', HTTP_X_REQUEST_ID='a\r\nSet-Cookie: admin=1'))
        self.assertNotIn('Set-Cookie: admin=1', response[HEADER])

    def test_the_id_is_visible_to_the_view(self):
        seen = {}

        def view(request):
            from django.http import HttpResponse
            seen['id'] = request_context.get_request_id()
            seen['attr'] = request.request_id
            return HttpResponse('ok')

        response = self._run(self.factory.get('/api/orders/'), view)
        self.assertEqual(seen['id'], response[HEADER])
        self.assertEqual(seen['attr'], response[HEADER])

    def test_the_context_is_cleared_after_the_request(self):
        """Otherwise a background thread or a later management command logs
        under a request ID that finished hours ago."""
        self._run(self.factory.get('/api/orders/'))
        self.assertEqual(request_context.get_request_id(), '')

    def test_the_context_is_cleared_even_when_the_view_raises(self):
        def boom(request):
            raise RuntimeError('kaboom')

        with self.assertLogs('core.request', level='ERROR'):
            with self.assertRaises(RuntimeError):
                self._run(self.factory.get('/api/orders/'), boom)
        self.assertEqual(request_context.get_request_id(), '')

    def test_a_view_exception_is_re_raised_untouched(self):
        """Handling it is not this middleware's job — swallowing it would turn
        a 500 into a silent empty response."""
        def boom(request):
            raise ValueError('specific message')

        with self.assertLogs('core.request', level='ERROR'):
            with self.assertRaises(ValueError) as caught:
                self._run(self.factory.get('/api/orders/'), boom)
        self.assertEqual(str(caught.exception), 'specific message')

    def test_an_access_line_is_written(self):
        with self.assertLogs('core.request', level='INFO') as captured:
            self._run(self.factory.get('/api/orders/'))
        self.assertIn('GET /api/orders/ 200', captured.output[0])

    def test_a_server_error_logs_at_error_level(self):
        def failing(request):
            from django.http import HttpResponse
            return HttpResponse(status=500)

        with self.assertLogs('core.request', level='ERROR') as captured:
            self._run(self.factory.get('/api/orders/'), failing)
        self.assertIn('500', captured.output[0])

    def test_a_client_error_logs_at_warning_level(self):
        def forbidden(request):
            from django.http import HttpResponse
            return HttpResponse(status=403)

        with self.assertLogs('core.request', level='WARNING') as captured:
            self._run(self.factory.get('/api/orders/'), forbidden)
        self.assertIn('403', captured.output[0])

    def test_the_query_string_is_never_logged(self):
        """Query strings on this API carry tokens, IRNs and party codes, and a
        rotating log file is a lower-trust store than the database they came
        from. `request.path`, never `get_full_path()`."""
        with self.assertLogs('core.request', level='INFO') as captured:
            self._run(self.factory.get('/api/orders/?token=SECRET&irn=abc'))
        joined = '\n'.join(captured.output)
        self.assertNotIn('SECRET', joined)
        self.assertNotIn('token', joined)

    def test_health_checks_are_not_logged(self):
        """A load balancer polls readiness every few seconds. Left in, those
        lines are the overwhelming majority of the file and rotate the real
        traffic out of it."""
        with self.assertNoLogs('core.request'):
            self._run(self.factory.get('/api/health/ready/'))

    def test_x_forwarded_for_is_ignored_without_a_proxy(self):
        """The header is caller-supplied. Trusting it by default lets any
        client write any address into the access log, which is precisely the
        record used to attribute abuse."""
        with self.assertLogs('core.request', level='INFO') as captured:
            self._run(self.factory.get(
                '/api/orders/', HTTP_X_FORWARDED_FOR='1.2.3.4'))
        self.assertNotIn('1.2.3.4', '\n'.join(captured.output))

    @override_settings(USE_X_FORWARDED_FOR=True)
    def test_the_last_forwarded_entry_wins_when_a_proxy_is_declared(self):
        """The first entry is whatever the client claimed; the last was
        appended by the proxy nearest to us and is the only trustworthy one."""
        captured = {}

        def view(request):
            from django.http import HttpResponse
            return HttpResponse('ok')

        with self.assertLogs('core.request', level='INFO') as logs:
            self._run(self.factory.get(
                '/api/orders/', HTTP_X_FORWARDED_FOR='9.9.9.9, 10.0.0.7'), view)
        record = logs.records[0]
        captured['ip'] = record.client_ip
        self.assertEqual(captured['ip'], '10.0.0.7')


class MiddlewareOrderTests(TestCase):
    """Position in `MIDDLEWARE` is load-bearing, so it is pinned."""

    PATH = 'core.middleware.RequestContextMiddleware'

    def test_it_is_installed(self):
        self.assertIn(self.PATH, settings.MIDDLEWARE)

    def test_it_is_first(self):
        """Middleware wraps inward, so only the outermost entry sees requests
        that SecurityMiddleware redirects, CorsMiddleware answers as a
        preflight, or VersionPolicy rejects with 426 — which are exactly the
        requests someone investigating a problem is looking for."""
        self.assertEqual(settings.MIDDLEWARE[0], self.PATH)

    def test_it_runs_outside_the_audit_middleware(self):
        """So an audit-trail failure is logged with the request ID of the
        request that caused it."""
        self.assertLess(settings.MIDDLEWARE.index(self.PATH),
                        settings.MIDDLEWARE.index('audit.middleware.AuditMiddleware'))


class LoggingConfigurationTests(TestCase):

    def test_every_installed_app_has_a_logger(self):
        """The hand-maintained list had dropped `HAIS`, `SKU`, `audit` and
        `notifications`, so their INFO records fell through to root — level
        WARNING — and were discarded. `audit` is the one that mattered: it
        logs the failures of the audit trail itself.

        Now derived from INSTALLED_APPS, and this asserts the derivation still
        covers everything. The symptom of a miss is silence, which is why a
        test has to look rather than a person.
        """
        third_party = ('django', 'rest_framework', 'corsheaders', 'whitenoise',
                       'drf_')
        local = {app.split('.')[0] for app in settings.INSTALLED_APPS
                 if not app.startswith(third_party)}
        self.assertEqual(local - set(settings.LOGGING['loggers']), set())

    def test_every_handler_carries_the_request_context_filter(self):
        """A handler without it receives records lacking `context_tag`, and
        the format string then raises for every line that reaches it."""
        for name, handler in settings.LOGGING['handlers'].items():
            with self.subTest(handler=name):
                self.assertIn('request_context', handler.get('filters', []))

    def test_the_console_stays_human_readable_under_json_mode(self):
        """`LOG_FORMAT=json` is for log shipping. The console exists to be read
        by a person standing in front of it."""
        self.assertEqual(settings.LOGGING['handlers']['console']['formatter'],
                         'console')

    def test_the_configuration_actually_loads(self):
        """`dictConfig` validates lazily in places — a bad `()` reference or a
        filter naming a class that does not exist surfaces only when applied."""
        import logging.config

        logging.config.dictConfig(settings.LOGGING)


class EndToEndTests(TestCase):
    """Through the real stack, since that is where the wiring lives."""

    def test_the_header_round_trips_through_a_real_view(self):
        response = self.client.get(reverse('health-live'),
                                   HTTP_X_REQUEST_ID='e2e-trace')
        self.assertEqual(response[HEADER], 'e2e-trace')

    def test_the_header_is_exposed_to_browser_javascript(self):
        """A response header the browser receives but refuses to expose is
        invisible to `fetch`. Without this the front end cannot show the user
        an ID to quote in a bug report, which is most of the point."""
        self.assertIn('x-request-id',
                      [h.lower() for h in settings.CORS_EXPOSE_HEADERS])

    def test_a_browser_may_send_its_own_id(self):
        """`X-Request-ID` is a non-simple header, so the browser preflights it.
        Missing from CORS_ALLOW_HEADERS, the preflight fails and the REAL
        request is cancelled — which looks like the server being down."""
        self.assertIn('x-request-id',
                      [h.lower() for h in settings.CORS_ALLOW_HEADERS])

    def test_an_application_log_line_carries_the_request_id_of_its_request(self):
        """The end the whole phase exists for: a line written by ordinary
        application code, correlated without that code knowing anything."""
        seen = {}
        app_logger = logging.getLogger('orders')

        class Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.addFilter(RequestContextFilter())

            def emit(self, record):
                seen['request_id'] = getattr(record, 'request_id', None)
                seen['path'] = getattr(record, 'path', None)

        handler = Capture()
        app_logger.addHandler(handler)
        try:
            from django.http import HttpResponse

            def view(request):
                app_logger.error('something happened deep in the order flow')
                return HttpResponse('ok')

            response = RequestContextMiddleware(view)(
                RequestFactory().get('/api/orders/submit/',
                                     HTTP_X_REQUEST_ID='deep-trace'))
        finally:
            app_logger.removeHandler(handler)

        self.assertEqual(seen['request_id'], 'deep-trace')
        self.assertEqual(seen['path'], '/api/orders/submit/')
        self.assertEqual(response[HEADER], 'deep-trace')
