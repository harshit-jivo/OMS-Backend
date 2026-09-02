"""Phase 4.2 -- an actual query-count audit for `orders` list endpoints.

The plan document's own words (docs/CODEBASE_AND_REFACTOR_PLAN.md, section 13)
admitted the N+1 remark was "a hypothesis, not a measurement." This module
measures it, using the exact technique the task calls for: seed a handful of
parent rows with related children, call the view via the Django/DRF test
client machinery against the in-memory sqlite DB, and compare the query count
at a small row count against a larger one. A fixed N+1 shows the SAME query
count at both sizes; a real one scales with the row count.

Run with::

    python manage.py test orders --settings=OMS.test_settings
"""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from sap_sync.models import Product as SapProduct
from users.models import PartyProductAssignment, User, UserRole

from .models import Order, OrderItem, OrderStatus
from .views import DashboardChartsView, MartOrderListView, OrderListView, OrdersByUserView
from .views.masters import PartyProductsView


def _query_count(view_callable, request, **kwargs):
    """Invoke `view_callable(request, **kwargs)` and return the number of
    queries it issued. Asserts a 200 so a broken view fails loudly instead of
    silently reporting a deceptively low query count."""
    with CaptureQueriesContext(connection) as ctx:
        response = view_callable(request, **kwargs)
        assert response.status_code == 200, (response.status_code, getattr(response, 'data', None))
    return len(ctx.captured_queries)


class OrderListViewQueryAuditTests(TestCase):
    """`OrderListView` already carries select_related/prefetch_related --
    this confirms that measurement rather than assuming it."""

    @classmethod
    def setUpTestData(cls):
        cls.status = OrderStatus.objects.create(code='NEED_APPROVAL', name='Need Approval')
        admin_role = UserRole.objects.create(name='admin', display_name='Admin')
        cls.admin = User.objects.create_user(
            username='qa-order-admin', password='pw', name='QA Admin', role=admin_role)

    def _add_orders(self, count):
        base = Order.objects.count()
        for i in range(count):
            order = Order.objects.create(
                order_number=f'ORD-{base + i}',
                card_code='C001',
                card_name='Test Party',
                status=self.status,
                created_by=self.admin,
                total_amount=100,
            )
            for j in range(3):
                OrderItem.objects.create(
                    order=order, item_code=f'IT{j}', item_name=f'Item {j}',
                    category='OIL', qty=1, total=10,
                )

    def _request(self):
        request = APIRequestFactory().get('/api/orders/list/')
        force_authenticate(request, user=self.admin)
        return request

    def test_query_count_does_not_scale_with_order_count(self):
        self._add_orders(3)
        count_at_3 = _query_count(OrderListView.as_view(), self._request())

        self._add_orders(3)  # 6 orders total, 3 items each
        count_at_6 = _query_count(OrderListView.as_view(), self._request())

        self.assertEqual(
            count_at_3, count_at_6,
            f'OrderListView issued {count_at_3} queries for 3 orders but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )


class OrdersByUserViewQueryAuditTests(TestCase):
    """Prefetches items + items__schemes -- confirms that holds under load."""

    @classmethod
    def setUpTestData(cls):
        cls.status = OrderStatus.objects.create(code='COMPLETED', name='Completed')
        admin_role = UserRole.objects.create(name='admin', display_name='Admin')
        cls.admin = User.objects.create_user(
            username='qa-obu-admin', password='pw', name='QA Admin', role=admin_role)

    def _add_orders(self, count):
        base = Order.objects.count()
        for i in range(count):
            order = Order.objects.create(
                order_number=f'OBU-{base + i}',
                card_code='C002',
                card_name='Another Party',
                status=self.status,
                created_by=self.admin,
                total_amount=50,
            )
            for j in range(2):
                OrderItem.objects.create(
                    order=order, item_code=f'X{j}', item_name=f'Thing {j}',
                    category='OIL', qty=2, total=5,
                )

    def _request(self):
        request = APIRequestFactory().get(f'/api/orders/ordersbyuser/{self.admin.id}/')
        force_authenticate(request, user=self.admin)
        return request

    def test_query_count_does_not_scale_with_order_count(self):
        self._add_orders(3)
        count_at_3 = _query_count(
            OrdersByUserView.as_view(), self._request(), user_id=self.admin.id)

        self._add_orders(3)
        count_at_6 = _query_count(
            OrdersByUserView.as_view(), self._request(), user_id=self.admin.id)

        self.assertEqual(
            count_at_3, count_at_6,
            f'OrdersByUserView issued {count_at_3} queries for 3 orders but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )


class MartOrderListViewQueryAuditTests(TestCase):
    """The Mart Approval queue -- also already select_related/prefetch_related."""

    @classmethod
    def setUpTestData(cls):
        cls.status = OrderStatus.objects.create(code='MART_APPROVAL', name='Mart Approval')
        admin_role = UserRole.objects.create(name='admin', display_name='Admin')
        cls.admin = User.objects.create_user(
            username='qa-mart-admin', password='pw', name='QA Admin', role=admin_role)

    def _add_orders(self, count):
        base = Order.objects.count()
        for i in range(count):
            order = Order.objects.create(
                order_number=f'MART-{base + i}',
                card_code='C003',
                card_name='Distributor Party',
                status=self.status,
                created_by=self.admin,
                order_type='DISTRIBUTOR',
                total_amount=250,
            )
            for j in range(3):
                OrderItem.objects.create(
                    order=order, item_code=f'M{j}', item_name=f'Mart Item {j}',
                    category='MART', qty=1, total=20,
                )

    def _request(self):
        request = APIRequestFactory().get('/api/orders/mart/list/')
        force_authenticate(request, user=self.admin)
        return request

    def test_query_count_does_not_scale_with_order_count(self):
        self._add_orders(3)
        count_at_3 = _query_count(MartOrderListView.as_view(), self._request())

        self._add_orders(3)
        count_at_6 = _query_count(MartOrderListView.as_view(), self._request())

        self.assertEqual(
            count_at_3, count_at_6,
            f'MartOrderListView issued {count_at_3} queries for 3 orders but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )


class DashboardChartsViewQueryAuditTests(TestCase):
    """The heaviest aggregation endpoint -- five charts, several `.annotate()`
    groupings and a Python-side state/category rollup. The plan document's own
    "hypothesis, not measurement" doubt was strongest here; this confirms the
    aggregations are genuinely batched (one query each) rather than looping a
    query per order."""

    @classmethod
    def setUpTestData(cls):
        cls.status = OrderStatus.objects.create(code='COMPLETED', name='Completed')
        admin_role = UserRole.objects.create(name='admin', display_name='Admin')
        cls.admin = User.objects.create_user(
            username='qa-dash-admin', password='pw', name='QA Admin', role=admin_role)

    def _add_orders(self, count):
        base = Order.objects.count()
        for i in range(count):
            order = Order.objects.create(
                order_number=f'DASH-{base + i}',
                card_code=f'C{base + i}',
                card_name=f'Party {base + i}',
                status=self.status,
                created_by=self.admin,
                total_amount=100,
            )
            for j in range(2):
                OrderItem.objects.create(
                    order=order, item_code=f'D{j}', item_name=f'Item {j}',
                    category='OIL', qty=1, total=10,
                )

    def _request(self):
        request = APIRequestFactory().get('/api/orders/dashboard/charts/')
        force_authenticate(request, user=self.admin)
        return request

    def test_query_count_does_not_scale_with_order_count(self):
        self._add_orders(3)
        count_at_3 = _query_count(DashboardChartsView.as_view(), self._request())

        self._add_orders(3)
        count_at_6 = _query_count(DashboardChartsView.as_view(), self._request())

        self.assertEqual(
            count_at_3, count_at_6,
            f'DashboardChartsView issued {count_at_3} queries for 3 orders but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )


class PartyProductsViewQueryAuditTests(TestCase):
    """`PartyProductsView` -- a REAL N+1 found by this audit and fixed here.

    It used to run one (sometimes two) `SapProduct` lookups per assignment
    instead of one batched query for the whole party. Fixed in
    `orders/views/masters.py`; this test is the regression guard.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='qa-party-products', password='pw', name='QA User')

    def _add_assignments(self, count):
        base = SapProduct.objects.count()
        for i in range(count):
            item_code = f'ITM{base + i:04d}'
            SapProduct.objects.create(
                item_code=item_code, item_name=f'Item {base + i}', category='OIL',
                is_active='Y', sal_factor2=1, tax_rate=5, sal_pack_unit='1',
            )
            PartyProductAssignment.objects.create(
                card_code='C777', item_code=item_code, category='OIL',
                basic_rate=100, is_active=True,
            )

    def _request(self):
        request = APIRequestFactory().get('/api/orders/party-products/C777/')
        force_authenticate(request, user=self.user)
        return request

    def test_query_count_does_not_scale_with_assignment_count(self):
        self._add_assignments(3)
        count_at_3 = _query_count(
            PartyProductsView.as_view(), self._request(), card_code='C777')

        self._add_assignments(3)  # 6 assignments total
        count_at_6 = _query_count(
            PartyProductsView.as_view(), self._request(), card_code='C777')

        self.assertEqual(
            count_at_3, count_at_6,
            f'PartyProductsView issued {count_at_3} queries for 3 assignments '
            f'but {count_at_6} for 6 -- query count scales with row count (N+1).',
        )
