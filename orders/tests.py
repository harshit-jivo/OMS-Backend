from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User, UserRole

from .models import Order, OrderRateApproval, OrderStatus
from sap_sync.services.sync_service import FOC_TOKEN_UNIT_PRICE, _get_sap_unit_price

from .views import OrderListView, WDashboardKPIView, _get_base_orders, _get_rate_approval_reason


class RateApprovalReasonTests(SimpleTestCase):
    """Which lines need a signature.

    The rule is one thing: the line sells BELOW the rate agreed with this party
    on `party_product_assignments`. The second argument is that agreed rate, NOT
    the `price_list_basic` the form sends — comparing against that field flagged
    94% of orders (613 of 647) between 28 Jul and 27 Aug, because it is
    tax-inclusive on some screens and pre-tax on others.

    Every item below carries its own `sub_group`, which keeps
    `_get_item_sub_group` from falling through to a SapProduct lookup — that is
    what lets these stay a `SimpleTestCase`.
    """

    def test_selling_below_the_agreed_rate_requires_approval(self):
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNotNone(_get_rate_approval_reason(item, 1000, 900))

    def test_selling_at_the_agreed_rate_does_not(self):
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 1000))

    def test_selling_above_the_agreed_rate_does_not(self):
        # Charging MORE than agreed is not a concession and needs nobody.
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 1100))

    def test_a_rounding_shortfall_is_within_tolerance(self):
        # Rates are stored to 4dp and a tax-inclusive round trip lands on
        # 3549.9999 rather than 3550. Without the tolerance every correctly
        # priced line would be flagged.
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, 3550, 3549.9999))

    def test_a_real_shortfall_beyond_tolerance_is_flagged(self):
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNotNone(_get_rate_approval_reason(item, 3550, 3549.5))

    def test_a_zero_priced_line_is_a_giveaway_not_a_discount(self):
        # A scheme companion or an FOC line. There is nothing to approve.
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 0))

    def test_no_assignment_row_stays_exempt(self):
        # Unmapped master data, not a discount — ~43% of lines, and they buried
        # the real ones. It belongs in an unmapped party/item report.
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, None, 842.85))

    def test_a_zero_quantity_line_is_not_an_order_line_at_all(self):
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 0}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 900))

    def test_a_malformed_quantity_does_not_blow_up_the_order(self):
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": "abc"}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 900))


class CommodityRateApprovalTests(SimpleTestCase):
    """A zero agreed rate means "no rate agreed", not "free".

    For a commodity that is the norm rather than an omission — the price tracks
    the market — so the line goes for approval every time. For anything else a
    zero is unmapped master data and stays exempt.
    """

    def test_commodity_with_zero_agreed_rate_requires_approval(self):
        item = {"item_name": "Soyabean Oil", "sub_group": "SOYABEAN", "qty": 600}

        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 2285.71))

    def test_the_sub_group_match_is_case_insensitive(self):
        item = {"item_name": "Mustard Oil", "sub_group": "mustard", "qty": 200}

        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 842.85))

    def test_the_sub_group_can_arrive_as_variety(self):
        # The order form calls this field `variety` on some screens.
        item = {"item_name": "Sesame Oil", "variety": "SESAME", "qty": 10}

        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 900))

    def test_a_premium_sub_group_with_no_agreed_rate_stays_exempt(self):
        # OLIVE is premium, not a commodity — per
        # OrderItemSerializer.get_variety_type.
        item = {"item_name": "Olive Oil", "sub_group": "OLIVE", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, 0, 999))

    def test_a_zero_priced_commodity_line_stays_exempt(self):
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, 0, 0))

    def test_a_commodity_with_no_assignment_row_stays_exempt(self):
        # A MISSING assignment is not the same as an assignment carrying zero:
        # only the explicit zero routes for approval.
        item = {"item_name": "Mustard Oil", "sub_group": "MUSTARD", "qty": 10}

        self.assertIsNone(_get_rate_approval_reason(item, None, 842.85))


class FocSapUnitPriceTests(SimpleTestCase):
    """What an FOC line is worth to SAP.

    Zero is not an option: an invoice totalling 0 generates no IRN. Falling
    through to the price list is worse — that is how a giveaway gets invoiced
    at full value.
    """

    class _Line:
        def __init__(self, price_list_basic, basic_price):
            self.price_list_basic = price_list_basic
            self.basic_price = basic_price

    def test_foc_line_with_no_basic_price_gets_the_token_rate(self):
        line = self._Line(price_list_basic=3550, basic_price=0)

        self.assertEqual(_get_sap_unit_price(line, is_foc=True), FOC_TOKEN_UNIT_PRICE)

    def test_foc_line_never_falls_through_to_the_price_list(self):
        # The bug this closes: a free line invoiced at 3550.
        line = self._Line(price_list_basic=3550, basic_price=0)

        self.assertNotEqual(_get_sap_unit_price(line, is_foc=True), 3550)

    def test_a_rate_someone_keyed_on_an_foc_line_is_kept(self):
        line = self._Line(price_list_basic=0, basic_price=1.5)

        self.assertEqual(_get_sap_unit_price(line, is_foc=True), 1.5)

    def test_a_normal_line_still_falls_through_to_the_price_list(self):
        # Not FOC: a blank basic price means the operator priced off the price
        # list, which is the intended rate. Returning 0 would invoice a real
        # sale as free.
        line = self._Line(price_list_basic=3550, basic_price=0)

        self.assertEqual(_get_sap_unit_price(line, is_foc=False), 3550)


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
