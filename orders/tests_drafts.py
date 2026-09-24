"""Save as Draft on Add Sales.

The draft branch of `CreateOrderView` was lost from the views in June while the
button, the `is_draft` flag and the /Drafts page stayed. Every "draft" was
then created as a real order and sent into the approval flow. These pin that
a draft stays a draft, and that a draft save can never touch a live order.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from orders.models import Order, OrderRateApproval, OrderStatus
from users.models import User, UserRole


class DraftOrderTests(TestCase):

    def setUp(self):
        role = UserRole.objects.create(name='billing', display_name='Billing')
        self.user = User.objects.create_user(username='drafter', password='pw', name='Drafter', role=role)
        self.other = User.objects.create_user(username='other', password='pw', name='Other', role=role)
        self.draft = OrderStatus.objects.create(code='DRAFT', name='Draft')
        self.created = OrderStatus.objects.create(code='CREATED', name='Order Created')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _save(self, **extra):
        body = {
            'is_draft': True, 'card_code': 'C001', 'card_name': 'Test Party',
            # A row the user has started: item picked, no quantity or rate yet.
            'items': [{'item_code': 'FG1', 'item_name': 'Oil 1L', 'qty': 0}],
            **extra,
        }
        return self.client.post('/api/orders/create/', body, format='json')

    def test_a_draft_is_saved_as_draft_and_not_sent_into_the_flow(self):
        response = self._save()
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['id'])
        self.assertEqual(order.status, self.draft)
        self.assertEqual(order.items.count(), 1)
        self.assertFalse(OrderRateApproval.objects.filter(order=order).exists())

    def test_a_draft_needs_no_items(self):
        response = self._save(items=[])
        self.assertEqual(response.status_code, 200, response.content)

    def test_re_saving_a_draft_updates_it_in_place(self):
        first = self._save().json()['id']
        response = self._save(order_id=first, card_name='Renamed',
                              items=[{'item_code': 'FG2', 'qty': 3}, {'item_code': 'FG3', 'qty': 1}])
        self.assertEqual(response.json()['id'], first)
        order = Order.objects.get(pk=first)
        self.assertEqual((order.card_name, order.items.count()), ('Renamed', 2))
        self.assertEqual(Order.objects.count(), 1)

    def test_a_live_order_cannot_be_parked_as_a_draft(self):
        live = Order.objects.create(order_number='ORD-LIVE-1', card_code='C001',
                                    status=self.created, created_by=self.user)
        self.assertEqual(self._save(order_id=live.pk).status_code, 409)
        live.refresh_from_db()
        self.assertEqual(live.status, self.created)

    def test_someone_elses_draft_is_not_overwritten(self):
        theirs = Order.objects.create(order_number='ORD-THEIRS-1', card_code='C001',
                                      status=self.draft, created_by=self.other)
        self.assertEqual(self._save(order_id=theirs.pk).status_code, 409)

    def test_an_own_draft_can_be_deleted_but_a_live_order_cannot(self):
        draft_id = self._save().json()['id']
        live = Order.objects.create(order_number='ORD-LIVE-2', card_code='C001',
                                    status=self.created, created_by=self.user)
        self.assertEqual(self.client.delete(f'/api/orders/{draft_id}/delete-draft/').status_code, 200)
        self.assertEqual(self.client.delete(f'/api/orders/{live.pk}/delete-draft/').status_code, 409)
        self.assertFalse(Order.objects.filter(pk=draft_id).exists())
        self.assertTrue(Order.objects.filter(pk=live.pk).exists())
