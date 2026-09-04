from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User, UserRole

from .models import Order, OrderRateApproval, OrderStatus
from .views import OrderListView, WDashboardKPIView, _get_base_orders, _get_rate_approval_reason


class RateApprovalReasonTests(SimpleTestCase):
    def test_no_authorised_rate_means_no_rate_approval(self):
        # No active party/item assignment (or a zero one) means there is no
        # agreed rate to measure against: unmapped master data, not a discount.
        item = {"item_name": "Mustard Oil"}

        self.assertIsNone(_get_rate_approval_reason(item, None, 500))
        self.assertIsNone(_get_rate_approval_reason(item, 0, 0))
        self.assertIsNone(_get_rate_approval_reason(item, 0, 500))

    def test_basic_price_below_price_list_basic_requires_rate_approval(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNotNone(_get_rate_approval_reason(item, 1000, 900))

    def test_valid_basic_price_does_not_require_rate_approval(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 1000))


class ApproverOrderScopeTests(TestCase):
    def setUp(self):
        role = UserRole.objects.create(name="approver", display_name="Approver")
        self.approver = User.objects.create_user(
            username="rate-approver",
            password="password",
            name="Rate Approver",
            role=role,
        )
        self.rate_status = OrderStatus.objects.create(
            code="RATE_APPROVAL",
            name="Rate Approval",
        )
        self.billing_status = OrderStatus.objects.create(
            code="BILLING",
            name="Billing",
        )

    def _create_order(self, order_number, status):
        return Order.objects.create(
            order_number=order_number,
            card_code="C001",
            card_name="Test Party",
            status=status,
            created_by=self.approver,
        )

    def test_approver_scope_excludes_orders_that_have_moved_to_billing(self):
        pending_order = self._create_order("ORD-PENDING", self.rate_status)
        billing_order = self._create_order("ORD-BILLING", self.billing_status)

        OrderRateApproval.objects.create(
            order=pending_order,
            approver=self.approver,
            status="PENDING",
        )
        OrderRateApproval.objects.create(
            order=billing_order,
            approver=self.approver,
            status="APPROVED",
        )

        self.assertQuerySetEqual(
            _get_base_orders(self.approver),
            [pending_order],
            transform=lambda order: order,
        )

    def test_pending_approver_list_excludes_billing_orders_with_stale_pending_rows(self):
        pending_order = self._create_order("ORD-PENDING", self.rate_status)
        billing_order = self._create_order("ORD-BILLING", self.billing_status)

        OrderRateApproval.objects.create(
            order=pending_order,
            approver=self.approver,
            status="PENDING",
        )
        OrderRateApproval.objects.create(
            order=billing_order,
            approver=self.approver,
            status="PENDING",
        )

        request = APIRequestFactory().get(
            "/orders/list/?status=RATE_APPROVAL&approval_pending=true"
        )
        force_authenticate(request, user=self.approver)

        response = OrderListView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([order["order_number"] for order in response.data], ["ORD-PENDING"])

    def test_approver_dashboard_pending_matches_active_pending_scope(self):
        pending_order = self._create_order("ORD-PENDING", self.rate_status)
        billing_order = self._create_order("ORD-BILLING", self.billing_status)

        OrderRateApproval.objects.create(
            order=pending_order,
            approver=self.approver,
            status="PENDING",
        )
        OrderRateApproval.objects.create(
            order=billing_order,
            approver=self.approver,
            status="PENDING",
        )

        request = APIRequestFactory().get("/orders/dashboardW/?month=0")
        force_authenticate(request, user=self.approver)

        response = WDashboardKPIView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pending_review_orders"], 1)
