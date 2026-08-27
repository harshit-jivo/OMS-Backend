"""API versioning and deprecation — Phase 6.2 and 6.4.

Versioning was added the only way it safely could be on a system with live web
and mobile clients: additively. Every route answers at both `/api/...` and
`/api/v1/...`, the same view behind each. So the tests that matter are the ones
proving the addition changed NOTHING about the existing prefix — same status,
same body, same permissions, same `reverse()` — because a versioning change
that quietly moves an endpoint is worse than no versioning at all.

Run with::

    python manage.py test core.tests_api_contract --settings=OMS.test_settings
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLPattern, URLResolver
from rest_framework.test import APIClient

from core.deprecation import (
    DEPRECATION_HEADER, LINK_HEADER, REGISTRY, SUNSET_HEADER, deprecated,
)

User = get_user_model()

VERSION_PREFIX = '/api/v1/'


def _routes():
    def walk(resolver, prefix=''):
        for p in resolver.url_patterns:
            if isinstance(p, URLResolver):
                yield from walk(p, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield '/' + prefix + str(p.pattern), p.callback
    return list(walk(get_resolver()))


class VersionedMountTests(TestCase):

    def setUp(self):
        self.routes = _routes()
        self.paths = {path for path, _ in self.routes}

    def test_the_versioned_prefix_exists(self):
        self.assertTrue(any(p.startswith(VERSION_PREFIX) for p in self.paths))

    def test_every_api_route_is_reachable_at_both_prefixes(self):
        """The additive property, asserted route by route. A partial mount
        would leave some endpoints versioned and some not, which is worse than
        either state on its own — a client could not tell which is which."""
        unversioned = {
            p for p in self.paths
            if p.startswith('/api/') and not p.startswith(
                (VERSION_PREFIX, '/api/schema/'))
        }
        versioned = {
            '/api/' + p[len(VERSION_PREFIX):]
            for p in self.paths if p.startswith(VERSION_PREFIX)
        }
        self.assertEqual(unversioned - versioned, set(),
                         'routes reachable only at the unversioned prefix')
        self.assertEqual(versioned - unversioned, set(),
                         'routes reachable only at the versioned prefix')

    def test_both_prefixes_resolve_to_the_same_view(self):
        """Not just the same path shape — the same callback. Two mounts of two
        different views would be a silent fork of the API."""
        by_path = dict(self.routes)
        for path, callback in self.routes:
            if not path.startswith(VERSION_PREFIX):
                continue
            legacy = '/api/' + path[len(VERSION_PREFIX):]
            with self.subTest(path=path):
                self.assertIs(by_path[legacy], callback)

    def test_reverse_still_returns_the_unversioned_path(self):
        """Including the same patterns twice registers every route name twice.
        Without the namespace, `reverse()` would resolve to whichever was
        registered last and every generated URL in the project would move —
        silently, since `reverse` cannot fail here."""
        self.assertEqual(reverse('health-live'), '/api/health/live/')

    def test_the_versioned_name_is_reachable_through_its_namespace(self):
        self.assertEqual(reverse('v1:health-live'), '/api/v1/health/live/')

    def test_the_schema_routes_are_not_duplicated(self):
        """`/api/schema/` describes the API; it is not part of it."""
        self.assertNotIn('/api/v1/schema/', self.paths)


class VersionedBehaviourTests(TestCase):
    """Same view, so the same answer — checked through the stack rather than
    inferred from the URLconf."""

    def setUp(self):
        self.client = APIClient()

    def test_a_public_route_answers_identically_at_both_prefixes(self):
        legacy = self.client.get('/api/health/live/')
        versioned = self.client.get('/api/v1/health/live/')
        self.assertEqual(legacy.status_code, versioned.status_code)
        self.assertEqual(legacy.json(), versioned.json())

    def test_an_authenticated_route_is_protected_at_both_prefixes(self):
        """The failure this rules out is the serious one: a new prefix that
        skips a permission check would republish the entire API anonymously."""
        for path in ('/api/orders/notifications/', '/api/v1/orders/notifications/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_the_versioned_prefix_is_not_public(self):
        """Broader than one route: no route may become AllowAny purely by
        being reached through the new prefix.

        `core.tests.PublicEndpointAllowlistTests` normalises the two prefixes
        to one path before auditing, which is correct — but it means that test
        alone cannot notice a difference BETWEEN them. This one can.
        """
        from core.tests import _permission_names

        by_path = dict(_routes())
        for path, callback in _routes():
            if not path.startswith(VERSION_PREFIX):
                continue
            legacy = '/api/' + path[len(VERSION_PREFIX):]
            with self.subTest(path=path):
                self.assertEqual(_permission_names(callback),
                                 _permission_names(by_path[legacy]))


class SchemaShapeTests(TestCase):

    def _schema(self):
        from drf_spectacular.generators import SchemaGenerator

        return SchemaGenerator().get_schema(request=None, public=True)

    def test_the_schema_describes_only_the_versioned_prefix(self):
        """Both mounts in one document would list every endpoint twice, and a
        reader could not tell which of the two to call."""
        paths = list(self._schema()['paths'])
        self.assertTrue(paths)
        stray = [p for p in paths if not p.startswith(VERSION_PREFIX)]
        self.assertEqual(stray, [], f'unversioned paths in the schema: {stray}')

    def test_operation_ids_are_unique(self):
        """The concrete harm of describing both mounts. drf-spectacular
        resolves collisions with numeric suffixes, so the published contract
        would name operations `orders_submit_create` and
        `orders_submit_create_2` with nothing to say which is which."""
        seen = {}
        for path, operations in self._schema()['paths'].items():
            for method, operation in operations.items():
                if method not in {'get', 'post', 'put', 'patch', 'delete'}:
                    continue
                op_id = operation.get('operationId')
                if op_id:
                    seen.setdefault(op_id, []).append(f'{method} {path}')
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(duplicates, {}, f'duplicate operationIds: {duplicates}')


class DeprecationDecoratorTests(TestCase):

    def setUp(self):
        # `REGISTRY` is module-level, so the throwaway views below would stay
        # in it for the rest of the run — and did: they failed
        # `test_every_registered_successor_resolves`, which checks that every
        # registered successor is a real route, against a fixture pointing at
        # `/api/v1/new/`. Snapshot and restore.
        snapshot = dict(REGISTRY)

        def restore():
            REGISTRY.clear()
            REGISTRY.update(snapshot)

        self.addCleanup(restore)

    def _view(self, **kwargs):
        from rest_framework.response import Response
        from rest_framework.views import APIView

        @deprecated(**kwargs)
        class Legacy(APIView):
            permission_classes = []
            authentication_classes = []

            def get(self, request):
                return Response({'ok': True})

            def post(self, request):
                return Response({'ok': True})

        return Legacy

    def _call(self, view, method='get'):
        from rest_framework.test import APIRequestFactory

        request = getattr(APIRequestFactory(), method)('/legacy/')
        return view.as_view()(request)

    def test_the_response_is_unchanged(self):
        """A deprecation must not alter what it is deprecating. Headers, never
        the body — a new body field would change the response shape of exactly
        the endpoints being retired."""
        response = self._call(self._view(successor='/api/v1/new/'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'ok': True})

    def test_the_deprecation_header_is_set(self):
        response = self._call(self._view())
        self.assertEqual(response[DEPRECATION_HEADER], 'true')

    def test_the_successor_is_named_in_a_link_header(self):
        """`rel="successor-version"` is the registered relation for this, so a
        gateway or an HTTP-aware client understands it untaught."""
        response = self._call(self._view(successor='/api/v1/notifications/'))
        self.assertIn('/api/v1/notifications/', response[LINK_HEADER])
        self.assertIn('rel="successor-version"', response[LINK_HEADER])

    def test_no_sunset_header_without_a_date(self):
        """An invented date is worse than none — clients plan against it."""
        self.assertNotIn(SUNSET_HEADER, self._call(self._view()))

    def test_a_sunset_date_is_passed_through(self):
        response = self._call(
            self._view(sunset='Wed, 31 Dec 2026 23:59:59 GMT'))
        self.assertEqual(response[SUNSET_HEADER], 'Wed, 31 Dec 2026 23:59:59 GMT')

    def test_every_method_is_covered(self):
        """Wrapping `dispatch` rather than the handlers is what makes this
        true — a per-method decorator can be forgotten on one of them."""
        view = self._view(successor='/x/')
        for method in ('get', 'post'):
            with self.subTest(method=method):
                self.assertEqual(self._call(view, method)[DEPRECATION_HEADER],
                                 'true')

    def test_the_call_is_logged_with_the_client_that_made_it(self):
        """The field that decides when an endpoint can be removed. A call
        count says nothing; knowing every call comes from one stale build says
        exactly when."""
        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().get(
            '/legacy/', HTTP_X_PLATFORM='ANDROID', HTTP_X_APP_VERSION='2.1.0',
            HTTP_X_BUILD_NUMBER='214')
        with self.assertLogs('core.deprecation', level='WARNING') as captured:
            self._view(successor='/x/').as_view()(request)
        record = captured.records[0]
        self.assertEqual(record.client_platform, 'ANDROID')
        self.assertEqual(record.client_version, '2.1.0')
        self.assertEqual(record.client_build, '214')

    def test_a_logging_failure_cannot_break_the_endpoint(self):
        """Measurement must never be able to break the thing it measures."""
        from unittest.mock import patch

        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().get('/legacy/')
        with patch('core.deprecation.logger.warning',
                   side_effect=RuntimeError('log backend down')):
            response = self._view().as_view()(request)
        self.assertEqual(response.status_code, 200)

    def test_an_existing_link_header_is_preserved(self):
        """`Link` is list-valued; overwriting it would drop whatever a
        pagination or documentation helper had already set."""
        from rest_framework.response import Response
        from rest_framework.test import APIRequestFactory
        from rest_framework.views import APIView

        @deprecated(successor='/api/v1/new/')
        class WithLink(APIView):
            permission_classes = []
            authentication_classes = []

            def get(self, request):
                response = Response({'ok': True})
                response['Link'] = '</docs>; rel="describedby"'
                return response

        response = WithLink.as_view()(APIRequestFactory().get('/x/'))
        self.assertIn('rel="describedby"', response[LINK_HEADER])
        self.assertIn('rel="successor-version"', response[LINK_HEADER])


class LegacyNotificationEndpointTests(TestCase):
    """The first real application of 6.4, and the one that unblocks 3.5.

    `orders.Notification` (table `notifications`) is superseded by
    `notifications.Notification` (table `notifications_notification`). Plan item
    3.5 retires it, and has been stuck on a question nobody could answer: is
    the old endpoint still being called, and by whom? These routes now say so
    in the response and record it in the log.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='clerk', password='x')
        self.client.force_authenticate(user=self.user)

    def test_the_legacy_list_endpoint_is_marked_deprecated(self):
        response = self.client.get('/api/orders/notifications/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response[DEPRECATION_HEADER], 'true')

    def test_it_names_the_replacement(self):
        response = self.client.get('/api/orders/notifications/')
        self.assertIn('/api/v1/notifications/', response[LINK_HEADER])

    def test_it_still_returns_what_it_always_did(self):
        """Deprecating an endpoint must not degrade it. The mobile client is
        still on this route and will be until someone sets a date."""
        response = self.client.get('/api/orders/notifications/')
        self.assertEqual(response.json(), [])

    def test_it_carries_no_sunset_date(self):
        """Deliberate. The date belongs to whoever owns the client migration,
        and 3.5 is still awaiting that decision."""
        self.assertNotIn(SUNSET_HEADER, self.client.get('/api/orders/notifications/'))

    def test_the_versioned_prefix_is_deprecated_too(self):
        """Mounting a superseded route under `/api/v1/` does not un-deprecate
        it. The decorator is on the view, so both prefixes carry the header —
        which is the reason to put it there rather than on a URL pattern."""
        response = self.client.get('/api/v1/orders/notifications/')
        self.assertEqual(response[DEPRECATION_HEADER], 'true')

    def test_it_is_registered(self):
        self.assertIn('NotificationListView', REGISTRY)
        self.assertEqual(REGISTRY['NotificationListView']['successor'],
                         '/api/v1/notifications/')

    def test_the_successor_route_exists(self):
        """A `Link: rel="successor-version"` pointing at a 404 is worse than
        no header — it sends a migrating client somewhere that does not
        answer."""
        successor = REGISTRY['NotificationListView']['successor']
        self.assertEqual(self.client.get(successor).status_code, 200)

    def test_every_registered_successor_resolves(self):
        """The same check for whatever gets marked next."""
        from django.urls import Resolver404, resolve

        for name, entry in REGISTRY.items():
            successor = entry.get('successor')
            if not successor:
                continue
            with self.subTest(view=name):
                try:
                    resolve(successor)
                except Resolver404:
                    self.fail(f'{name} names a successor that does not resolve: '
                              f'{successor}')
