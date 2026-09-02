"""Phase 4.2 -- query-count audit for the `sap_sync` master-data list endpoints.

Same technique as `orders.tests_query_audit` and the task instructions: seed a
handful of rows, capture the query count via
`django.test.utils.CaptureQueriesContext`, seed more, capture again, and
assert the count did not scale. See that module's docstring for the full
rationale.

Run with::

    python manage.py test sap_sync --settings=OMS.test_settings
"""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User

from .models import Party, PartyAddress, Product
from .views import PartyAddressListView, PartyListView, ProductListView


def _query_count(view_callable, request):
    with CaptureQueriesContext(connection) as ctx:
        response = view_callable(request)
        assert response.status_code == 200, (response.status_code, getattr(response, 'data', None))
    return len(ctx.captured_queries)


class SapSyncListQueryAuditTests(TestCase):
    """`ProductListView`, `PartyListView` and `PartyAddressListView` all use
    flat serializers with no nested relation, so none of them should show an
    N+1 -- measured here rather than assumed.

    NOTE (found during this audit, left alone -- out of this task's scope):
    `sap_sync.PartySerializer` declares an `addresses` nested field and
    `PartyDetailView` / `PartyByCodeView` / `GetPartyByCategoryView` all use
    it, but migration `sap_sync/migrations/0004_...` removed the `party` FK
    that used to back that related name (`RemoveField partyaddress.party`).
    `Party` has no `addresses` relation any more -- serializing one raises
    `AttributeError`, so those three endpoints 500 on every real call. That is
    a correctness bug, not an N+1 (there is no query to count; it never gets
    that far), so it is reported here rather than fixed under this task.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='qa-sap-list', password='pw', name='QA User')

    def _request(self, path):
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=self.user)
        return request

    def test_product_list_view_query_count_does_not_scale(self):
        base = Product.objects.count()
        for i in range(3):
            Product.objects.create(
                item_code=f'PL{base + i}', item_name=f'Product {base + i}',
                category='OIL', is_active='Y')
        count_at_3 = _query_count(ProductListView.as_view(), self._request('/api/sap/products/'))

        for i in range(3, 6):
            Product.objects.create(
                item_code=f'PL{base + i}', item_name=f'Product {base + i}',
                category='OIL', is_active='Y')
        count_at_6 = _query_count(ProductListView.as_view(), self._request('/api/sap/products/'))

        self.assertEqual(
            count_at_3, count_at_6,
            f'ProductListView issued {count_at_3} queries for 3 products but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )

    def test_party_list_view_query_count_does_not_scale(self):
        base = Party.objects.count()
        for i in range(3):
            Party.objects.create(
                card_code=f'PARTY{base + i}', card_name=f'Party {base + i}',
                category='OIL')
        count_at_3 = _query_count(PartyListView.as_view(), self._request('/api/sap/parties/'))

        for i in range(3, 6):
            Party.objects.create(
                card_code=f'PARTY{base + i}', card_name=f'Party {base + i}',
                category='OIL')
        count_at_6 = _query_count(PartyListView.as_view(), self._request('/api/sap/parties/'))

        self.assertEqual(
            count_at_3, count_at_6,
            f'PartyListView issued {count_at_3} queries for 3 parties but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )

    def test_party_address_list_view_query_count_does_not_scale(self):
        base = PartyAddress.objects.count()
        for i in range(3):
            PartyAddress.objects.create(
                card_code=f'ADDR{base + i}', address_name=f'Address {base + i}',
                address_type='B')
        count_at_3 = _query_count(
            PartyAddressListView.as_view(), self._request('/api/sap/addresses/'))

        for i in range(3, 6):
            PartyAddress.objects.create(
                card_code=f'ADDR{base + i}', address_name=f'Address {base + i}',
                address_type='B')
        count_at_6 = _query_count(
            PartyAddressListView.as_view(), self._request('/api/sap/addresses/'))

        self.assertEqual(
            count_at_3, count_at_6,
            f'PartyAddressListView issued {count_at_3} queries for 3 addresses '
            f'but {count_at_6} for 6 -- query count scales with row count (N+1).',
        )
