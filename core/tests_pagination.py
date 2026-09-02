"""Opt-in pagination — Phase 6.3.

The whole value of `OptInPagination` is what it does NOT do. A client that has
not asked for pagination must receive the identical bytes it receives today,
because the live web and mobile clients index straight into the JSON array and
an envelope would break them all at once.

So the tests are weighted towards the unpaginated path, and one of them
compares against a view with no pagination class at all rather than against a
hand-written expectation — the only way to assert "unchanged" that stays true
when the serializer changes.

Run with::

    python manage.py test core.tests_pagination --settings=OMS.test_settings
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.pagination import UNBOUNDED_WARN_THRESHOLD
from sap_sync.models import PartyAddress

User = get_user_model()

PATH = '/api/sap/addresses/'


class OptInPaginationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        PartyAddress.objects.bulk_create([
            PartyAddress(card_code=f'C{i:04d}', address_name=f'A{i}',
                         address_type='S')
            for i in range(30)
        ])

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=User.objects.create_user(
            username='clerk', password='x'))

    def test_without_a_page_param_the_response_is_a_plain_list(self):
        """The load-bearing assertion. `DEFAULT_PAGINATION_CLASS` would turn
        this into an object and break every existing client at once."""
        body = self.client.get(PATH).json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 30)

    def test_without_a_page_param_nothing_is_truncated(self):
        """A pagination class that quietly capped the result would be a
        correctness bug dressed up as a performance fix."""
        self.assertEqual(len(self.client.get(PATH).json()),
                         PartyAddress.objects.count())

    def test_asking_for_a_page_returns_the_envelope(self):
        body = self.client.get(f'{PATH}?page=1&page_size=10').json()
        self.assertIs(body['success'], True)
        self.assertEqual(len(body['data']['results']), 10)
        self.assertEqual(body['data']['pagination']['total'], 30)
        self.assertEqual(body['data']['pagination']['total_pages'], 3)

    def test_page_size_alone_is_enough_to_opt_in(self):
        """A client that only wants a smaller response should not have to know
        it must also send `page=1`."""
        body = self.client.get(f'{PATH}?page_size=5').json()
        self.assertEqual(len(body['data']['results']), 5)

    def test_pagination_composes_with_the_existing_filters(self):
        """The filters predate this and are how the endpoint is actually used;
        pagination must narrow the filtered set, not the whole table."""
        body = self.client.get(f'{PATH}?card_code=C0001&page=1').json()
        self.assertEqual(body['data']['pagination']['total'], 1)

    def test_the_page_size_is_capped(self):
        """`max_page_size` is the reason `?page_size=100000` cannot be used to
        reintroduce the unbounded response through the front door."""
        body = self.client.get(f'{PATH}?page_size=99999').json()
        self.assertLessEqual(len(body['data']['results']), 200)

    def test_an_out_of_range_page_is_a_404_not_an_empty_success(self):
        self.assertEqual(self.client.get(f'{PATH}?page=99').status_code, 404)


class UnboundedWarningTests(TestCase):
    """The measurement half. Nothing is truncated, so the log is the only
    evidence of what these endpoints cost and which client is asking."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=User.objects.create_user(
            username='clerk', password='x'))

    def test_a_small_unpaginated_response_logs_nothing(self):
        PartyAddress.objects.create(card_code='C1', address_name='A')
        with self.assertNoLogs('core.pagination'):
            self.client.get(PATH)

    def test_a_large_unpaginated_response_is_logged_with_its_size(self):
        PartyAddress.objects.bulk_create([
            PartyAddress(card_code=f'C{i}', address_name=f'A{i}')
            for i in range(UNBOUNDED_WARN_THRESHOLD)
        ])
        with self.assertLogs('core.pagination', level='WARNING') as captured:
            self.client.get(PATH)
        record = captured.records[0]
        self.assertEqual(record.row_count, UNBOUNDED_WARN_THRESHOLD)
        self.assertEqual(record.endpoint, PATH)

    def test_the_calling_client_is_recorded(self):
        """Which client still asks for the whole table is the fact that says
        when pagination can become the default."""
        PartyAddress.objects.bulk_create([
            PartyAddress(card_code=f'C{i}', address_name=f'A{i}')
            for i in range(UNBOUNDED_WARN_THRESHOLD)
        ])
        with self.assertLogs('core.pagination', level='WARNING') as captured:
            self.client.get(PATH, HTTP_X_PLATFORM='ANDROID',
                            HTTP_X_APP_VERSION='2.1.0')
        self.assertEqual(captured.records[0].client_platform, 'ANDROID')

    def test_a_paginated_request_is_not_warned_about(self):
        PartyAddress.objects.bulk_create([
            PartyAddress(card_code=f'C{i}', address_name=f'A{i}')
            for i in range(UNBOUNDED_WARN_THRESHOLD)
        ])
        with self.assertNoLogs('core.pagination'):
            self.client.get(f'{PATH}?page=1')


class OrderingAllowListTests(TestCase):
    """`ordering_from` predates this phase; pinned here because it is the
    other half of 6.3 and had no test."""

    def _request(self, ordering):
        from rest_framework.test import APIRequestFactory
        from rest_framework.request import Request

        return Request(APIRequestFactory().get('/x/', {'ordering': ordering}))

    def test_an_allowed_field_is_honoured(self):
        from core.pagination import ordering_from

        self.assertEqual(
            ordering_from(self._request('created_at'), {'created_at'}, '-id'),
            'created_at')

    def test_a_descending_prefix_is_allowed(self):
        from core.pagination import ordering_from

        self.assertEqual(
            ordering_from(self._request('-created_at'), {'created_at'}, '-id'),
            '-created_at')

    def test_an_unlisted_field_falls_back_to_the_default(self):
        """Never interpolate user input into `order_by()`. An unchecked value
        can order by a related model's column and, through it, read rows the
        caller cannot otherwise see."""
        from core.pagination import ordering_from

        for hostile in ('password', 'user__password', 'id); DROP TABLE x--',
                        '?', ''):
            with self.subTest(ordering=hostile):
                self.assertEqual(
                    ordering_from(self._request(hostile), {'created_at'}, '-id'),
                    '-id')


class OptInFilteringTests(TestCase):
    """The other half of 6.3: `DjangoFilterBackend`, wired onto
    `PartyAddressListView` (same for `ProductListView` / `PartyListView`,
    exercised at the view level by `sap_sync.tests_query_audit`).

    Wired in additively: the fields it filters on (`state`, `city`,
    `country`, `category`) are ones the view's own hand-written `card_code` /
    `address_type` / `gst` filters never touched, and it only ever narrows
    the queryset when a caller actually sends one of its own query params --
    the same opt-in guarantee `OptInPaginationTests` above makes for paging.
    """

    @classmethod
    def setUpTestData(cls):
        PartyAddress.objects.bulk_create([
            PartyAddress(card_code='C0001', address_name='A1',
                         address_type='S', state='Delhi'),
            PartyAddress(card_code='C0002', address_name='A2',
                         address_type='S', state='Maharashtra'),
            PartyAddress(card_code='C0003', address_name='A3',
                         address_type='S', state='Delhi'),
        ])

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=User.objects.create_user(
            username='clerk-filter', password='x'))

    def test_without_a_filter_param_every_row_still_comes_back(self):
        """Declaring `filter_backends` must not narrow anything on its own."""
        body = self.client.get(PATH).json()
        self.assertEqual(len(body), PartyAddress.objects.count())
        self.assertEqual(
            {row['card_code'] for row in body},
            set(PartyAddress.objects.values_list('card_code', flat=True)))

    def test_a_filter_param_narrows_to_the_matching_rows(self):
        body = self.client.get(f'{PATH}?state=Delhi').json()
        self.assertEqual({row['card_code'] for row in body},
                          {'C0001', 'C0003'})

    def test_a_filter_param_with_no_match_returns_an_empty_list(self):
        self.assertEqual(self.client.get(f'{PATH}?state=Gujarat').json(), [])

    def test_filtering_composes_with_the_existing_hand_written_filters(self):
        """`card_code` (hand-written) and `state` (DjangoFilterBackend) must
        narrow together, not have one silently win."""
        body = self.client.get(f'{PATH}?card_code=C0001&state=Delhi').json()
        self.assertEqual([row['card_code'] for row in body], ['C0001'])
        self.assertEqual(
            self.client.get(f'{PATH}?card_code=C0001&state=Maharashtra').json(),
            [])


class OptInOrderingTests(TestCase):
    """The shared `ordering_from` allow-list (see `OrderingAllowListTests`
    above), wired for real onto `PartyAddressListView` via `?ordering=` --
    the same helper, not a second copy of the rule."""

    @classmethod
    def setUpTestData(cls):
        PartyAddress.objects.bulk_create([
            PartyAddress(card_code='C3', address_name='Zeta', address_type='S'),
            PartyAddress(card_code='C1', address_name='Alpha', address_type='S'),
            PartyAddress(card_code='C2', address_name='Mu', address_type='S'),
        ])

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=User.objects.create_user(
            username='clerk-order', password='x'))

    def test_without_an_ordering_param_the_models_own_ordering_holds(self):
        """No `ordering` param must mean `order_by()` is never even called --
        the model's `Meta.ordering` (`card_code`) is what decides, unchanged."""
        body = self.client.get(PATH).json()
        self.assertEqual([row['card_code'] for row in body], ['C1', 'C2', 'C3'])

    def test_an_allowed_ordering_field_is_honoured(self):
        body = self.client.get(f'{PATH}?ordering=-address_name').json()
        self.assertEqual([row['address_name'] for row in body],
                          ['Zeta', 'Mu', 'Alpha'])

    def test_an_unlisted_ordering_field_falls_back_to_the_default(self):
        """Rejected by the same allow-list `ordering_from` already enforces
        elsewhere -- an unlisted field falls back to the view's default
        ordering rather than reaching `order_by()` unchecked."""
        body = self.client.get(f'{PATH}?ordering=synced_at').json()
        self.assertEqual([row['card_code'] for row in body], ['C1', 'C2', 'C3'])
