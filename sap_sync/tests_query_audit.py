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

    NOTE (raised during this audit, then investigated and RESOLVED -- the
    original claim recorded here was wrong, and is corrected in place so the
    wrong version does not get quoted onward):

    The claim was that `sap_sync.PartySerializer`'s `addresses` field made
    `PartyDetailView` / `PartyByCodeView` / `GetPartyByCategoryView` return 500
    on every call, because migration `0004_...` removed the `PartyAddress.party`
    FK backing that related name.

    The premise was right and the conclusion was wrong. There is indeed no
    `addresses` relation on `Party`. But the field was declared `read_only=True`,
    and DRF sets `required=False` for read-only fields -- so `get_attribute()`
    raising `AttributeError` was converted to `SkipField` and the key was
    silently dropped. Verified by execution, not by reading: serializing a real
    `Party` returns 200 with every other field intact and no `addresses` key.

    The actual defect was in the published contract. drf-spectacular introspects
    this serializer, so the schema advertised `addresses` and the frontend's
    generated types declared it REQUIRED (`readonly addresses: PartyAddress[]`)
    -- an array that had not been sent since February. The dead field has been
    removed from the serializer; response bodies are unchanged, and the schema
    now matches what these endpoints actually return.
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


class PartySerializerContractTests(TestCase):
    """`PartySerializer` must publish exactly what it delivers.

    Regression guard for the `addresses` field removed from that serializer: it
    referenced a relation deleted by migration 0004, and because it was
    `read_only=True` DRF silently skipped it (`required=False` turns the missing
    attribute's AttributeError into SkipField) instead of raising. The result
    was a schema — and a set of generated frontend types — advertising a
    REQUIRED `addresses: PartyAddress[]` that no response has carried since.

    These assertions fail if anyone re-adds a serializer field that `Party`
    cannot actually resolve, whether it fails loudly or silently.
    """

    @classmethod
    def setUpTestData(cls):
        cls.party = Party.objects.create(card_code='CONTRACT1',
                                         card_name='Contract Test Party')

    def test_every_declared_field_is_actually_delivered(self):
        """No declared field may be silently dropped from the payload."""
        from .serializers import PartySerializer

        serializer = PartySerializer(self.party)
        declared = set(serializer.fields.keys())
        delivered = set(serializer.data.keys())

        self.assertEqual(
            declared - delivered, set(),
            'PartySerializer declares field(s) it does not deliver: '
            f'{sorted(declared - delivered)}. A read_only field naming a '
            'relation Party does not have is skipped silently at runtime, but '
            'still lands in the OpenAPI schema and the frontend types.',
        )

    def test_addresses_is_not_advertised(self):
        """Party has no `addresses` relation; nothing may claim otherwise."""
        from .serializers import PartySerializer

        self.assertNotIn(
            'addresses', PartySerializer(self.party).data,
            'Party has no addresses relation (PartyAddress.card_code is a plain '
            'CharField since migration 0004). Addresses come from '
            '/sap/addresses/ (PartyAddressListView) instead.',
        )
