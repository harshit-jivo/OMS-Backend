from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from orders.models import Order, OrderStatus
from orders.services.order_flow import _get_initial_flow_status, _get_next_flow_status
from orders.services.rate_approval import (
    _get_rate_approval_reason, _get_rate_approvers_for_item,
)
from users.models import User, UserRole


class SCApprovalTests(TestCase):
    def setUp(self):
        role = UserRole.objects.create(name='approver', display_name='Approver')
        self.raju = User.objects.create(username='Raju Vg', name='Jasbir Singh Raju', role=role)
        User.objects.create(username='Raju VG', name='Tracker', role=UserRole.objects.create(
            name='tracker_user', display_name='Tracker'))
        self.rate = OrderStatus.objects.create(code='RATE_APPROVAL', name='Rate Approval')
        self.auditor = OrderStatus.objects.create(code='AUDITOR_APPROVAL', name='Auditor Approval')
        OrderStatus.objects.create(code='COMPLETED', name='Completed')

    def test_sc_requires_approval_at_any_price_even_without_assignment(self):
        for code in ('SC0000064', ' sc0000078 '):
            for agreed, price in ((.1, 338.9831), (None, 400), (0, 0)):
                with self.subTest(code=code, agreed=agreed, price=price):
                    self.assertIn('Raju Vg', _get_rate_approval_reason(
                        {'item_code': code, 'qty': 100}, agreed, price))

    def test_sc_selects_approver_account_not_tracker_or_subgroup_rule(self):
        self.assertEqual(_get_rate_approvers_for_item(SimpleNamespace(
            item_code='SC0000064', category='OIL', sub_group='INFINITE')), [self.raju])

    def test_inactive_designated_approver_cannot_silently_fall_back(self):
        self.raju.is_active = False
        self.raju.save()
        with self.assertRaises(ValidationError):
            _get_rate_approvers_for_item(SimpleNamespace(item_code='SC1'))

    def test_sc_overrides_disabled_rate_stage_and_foc_bypass(self):
        config = SimpleNamespace(rate_approval_enabled=False, billing_enabled=False,
                                 auditor_enabled=True, rate_conditions=[])
        for flow in ('ASM', 'BILLING'):
            for foc in (False, True):
                result = _get_initial_flow_status(
                    [{'item_code': 'SC1', 'qty': 1}], float, flow_type=flow,
                    force_foc_flow=foc, config=config, requires_rate_approval=False)
                self.assertEqual(result, (self.rate, True))
        self.assertEqual(_get_next_flow_status(self.rate, config=config), self.auditor)

    def test_non_sc_rate_comparison_is_unchanged(self):
        self.assertIsNone(_get_rate_approval_reason({'item_code': 'FG1', 'qty': 1}, .1, 400))
        self.assertIsNotNone(_get_rate_approval_reason({'item_code': 'FG1', 'qty': 1}, 500, 400))

    @patch('orders.views.lifecycle.send_order_notifications')
    def test_create_and_both_billing_edit_endpoints_route_sc_to_raju(self, notify):
        seller = User.objects.create(username='billing', name='Billing', role=UserRole.objects.create(
            name='billing', display_name='Billing'))
        OrderStatus.objects.create(code='CREATED', name='Order Created')
        billing = OrderStatus.objects.create(code='BILLING', name='Billing')
        client = APIClient()
        client.force_authenticate(seller)
        body = {'card_code': 'C001', 'card_name': 'Party', 'items': [{
            'item_code': 'SC0000064', 'item_name': 'Flask', 'category': 'OIL',
            'qty': 100, 'basic_price': 338.9831, 'price_list_basic': .1,
            'total': 33898.31, 'sub_group': 'INFINITE',
        }]}
        response = client.post('/api/orders/create/', body, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get(pk=response.json()['id'])
        self.assertEqual(order.status, self.rate)
        self.assertEqual(list(order.rate_approvals.values_list('approver_id', 'status')),
                         [(self.raju.id, 'PENDING')])
        for method, url in (('post', '/api/orders/create/'),
                            ('put', f'/api/orders/{order.id}/update/')):
            order.status = billing
            order.save()
            order.rate_approvals.update(status='APPROVED')
            response = getattr(client, method)(url, {**body, 'order_id': order.id}, format='json')
            self.assertEqual(response.status_code, 200, response.content)
            order.refresh_from_db()
            self.assertEqual(order.status, self.rate)
            self.assertEqual(order.rate_approvals.get(approver=self.raju).status, 'PENDING')
