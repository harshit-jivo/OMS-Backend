"""`items_count` on the order lists that use `OrderListByUserIdSerializer`.

The Billing / Auditor / Rate-Approver Tracking pages have an Items column that
read 0 on every order: the serializer had `items_count` commented out, so the
response never carried it and the client fell back to zero. The count now comes
from a `Count('items', distinct=True)` annotation, applied after the tracking
view's filters so it survives the `rate_approvals` join on the rate-approver
branch.

    python manage.py test orders.tests_items_count --settings=OMS.test_settings
"""
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User, UserRole

from .models import Order, OrderItem, OrderRateApproval, OrderStatus
from .views import OrderStatusTrackingView, OrdersByUserView


class ItemsCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status = OrderStatus.objects.create(code='COMPLETED', name='Completed')
        admin_role = UserRole.objects.create(name='admin', display_name='Admin')
        cls.admin = User.objects.create_user(
            username='qa-items-admin', password='pw', name='QA Admin', role=admin_role)
        cls.order = Order.objects.create(
            order_number='IC-1', card_code='C1', card_name='Party One',
            status=cls.status, created_by=cls.admin, total_amount=30)
        for index in range(3):
            OrderItem.objects.create(
                order=cls.order, item_code=f'X{index}', item_name=f'Thing {index}',
                category='OIL', qty=1, total=10)
        cls.empty = Order.objects.create(
            order_number='IC-2', card_code='C1', card_name='Party One',
            status=cls.status, created_by=cls.admin, total_amount=0)

    def _get(self, view, path, **kwargs):
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=self.admin)
        return view.as_view()(request, **kwargs)

    def test_orders_by_user_carries_the_item_count(self):
        response = self._get(OrdersByUserView, f'/api/orders/ordersbyuser/{self.admin.id}/',
                             user_id=self.admin.id)
        by_number = {row['order_number']: row['items_count'] for row in response.data}
        self.assertEqual(by_number, {'IC-1': 3, 'IC-2': 0})

    def test_tracking_carries_the_item_count_through_the_approvals_join(self):
        OrderRateApproval.objects.create(
            order=self.order, approver=self.admin, status='APPROVED')
        response = self._get(OrderStatusTrackingView,
                             '/api/orders/status-tracking/?mode=rate_approver')
        self.assertEqual(response.status_code, 200)
        rows = [row for row in response.data if row['order_number'] == 'IC-1']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['items_count'], 3)
        self.assertEqual(rows[0]['decision_type'], 'accepted')
