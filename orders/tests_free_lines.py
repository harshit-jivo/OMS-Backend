"""A line deliberately given away on a paid order.

Before this there was no way to say "free": a zero basic price was billed at
the price list when one existed, went out at 0 with no approval when it did
not, and a token 0.001 was treated as a discount. A free line is now priced at
the FOC token rate, never the price list, and always goes to Rate Approval
with its reason.
"""
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.test import APIClient

from orders.models import Order, OrderStatus
from orders.services.order_items import FOC_TOKEN_BASIC_PRICE, _create_order_item
from orders.services.rate_approval import _get_rate_approval_reason
from orders.views.lifecycle import _free_line_error
from sap_sync.services.sync_service import _get_sap_unit_price
from users.models import User, UserRole


def _to_float(value, default=0.0):
    try:
        return float(value) if value not in (None, '') else float(default)
    except (TypeError, ValueError):
        return float(default)


def _to_bool(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


FREE_LINE = {
    'item_code': 'FG0000005', 'item_name': 'EXTRA LIGHT OLIVE 1 LTR', 'qty': 400,
    'basic_price': 426, 'price_list_basic': 426, 'total': 170400,
    'is_free': True, 'free_reason': 'Launch sample',
}


class FreeLineTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='seller', password='pw', name='Seller',
            role=UserRole.objects.create(name='billing', display_name='Billing'))
        self.order = Order.objects.create(
            order_number='ORD-FREE-1', card_code='C001', created_by=self.user,
            status=OrderStatus.objects.create(code='CREATED', name='Order Created'))

    def test_a_free_line_is_stored_at_the_token_rate_whatever_the_form_sent(self):
        line = _create_order_item(self.order, FREE_LINE, _to_float, _to_bool)
        line.refresh_from_db()
        self.assertTrue(line.is_free)
        self.assertEqual(line.free_reason, 'Launch sample')
        self.assertEqual(float(line.basic_price), FOC_TOKEN_BASIC_PRICE)
        self.assertEqual(float(line.price_list_basic), 0)
        self.assertEqual(float(line.total), round(400 * FOC_TOKEN_BASIC_PRICE, 4))

    def test_a_free_line_always_needs_rate_approval_with_its_reason(self):
        # Even with no agreed rate and a zero price, which otherwise read as a
        # giveaway that needs nobody's signature.
        reason = _get_rate_approval_reason({**FREE_LINE, 'basic_price': 0}, None, 0)
        self.assertEqual(reason, 'EXTRA LIGHT OLIVE 1 LTR: FREE (Launch sample)')

    def test_a_zero_priced_line_not_marked_free_still_needs_no_approval(self):
        self.assertIsNone(_get_rate_approval_reason({'qty': 5, 'basic_price': 0}, None, 0))

    def test_a_free_line_without_a_reason_is_refused(self):
        self.assertIn('give a reason', _free_line_error([{**FREE_LINE, 'free_reason': '  '}]))
        self.assertIsNone(_free_line_error([FREE_LINE, {'item_code': 'FG1', 'qty': 1}]))

    def test_the_order_endpoint_refuses_a_free_line_without_a_reason(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post('/api/orders/create/', {
            'card_code': 'C001', 'items': [{**FREE_LINE, 'free_reason': ''}],
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('reason', response.json()['error'])

    def test_sap_gets_the_token_rate_never_the_price_list(self):
        line = SimpleNamespace(is_free=True, basic_price=426, price_list_basic=426)
        self.assertEqual(_get_sap_unit_price(line), FOC_TOKEN_BASIC_PRICE)
