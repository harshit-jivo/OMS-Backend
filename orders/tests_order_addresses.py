"""The street address behind an order's Bill To / Ship To.

An order stores the address NAME (`bill_to_address`), because the sales form
saves `address_name or full_address` — so for every address that has a name,
the street never reaches the order at all. Every screen that shows an order's
addresses therefore showed "Head Office" and nothing about where that is.

The id IS on the order, and it points at `sap_party_addresses`, so the detail
serializer resolves the street from there rather than duplicating it onto the
order row.

    python manage.py test orders.tests_order_addresses --settings=OMS.test_settings
"""
from django.test import TestCase

from sap_sync.models import PartyAddress
from users.models import User, UserRole

from .models import Order, OrderItem, OrderStatus
from .serializers import OrderDetailSerializer


class OrderFullAddressTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status = OrderStatus.objects.create(code='PENDING', name='Pending')
        role = UserRole.objects.create(name='admin', display_name='Admin')
        cls.user = User.objects.create_user(
            username='qa-address', password='pw', name='QA', role=role)
        cls.bill = PartyAddress.objects.create(
            card_code='C1', address_name='Head Office', address_type='B',
            full_address='12 Mall Road, Ludhiana, Punjab 141001', category='OIL')
        cls.ship = PartyAddress.objects.create(
            card_code='C1', address_name='Main Warehouse', address_type='S',
            full_address='Plot 9, Focal Point, Ludhiana, Punjab 141010', category='OIL')

    def _order(self, **overrides):
        fields = dict(
            order_number='ADDR-1', card_code='C1', card_name='Party One',
            status=self.status, created_by=self.user, total_amount=10,
            bill_to_id=self.bill.id, bill_to_address='Head Office',
            ship_to_id=self.ship.id, ship_to_address='Main Warehouse',
        )
        fields.update(overrides)
        order = Order.objects.create(**fields)
        # `get_party_state` reads `obj.items.first().category` with no guard,
        # so an order with no items cannot be serialised at all. Not this
        # change's bug, but every order here needs a line because of it.
        OrderItem.objects.create(
            order=order, item_code='X1', item_name='Thing', category='OIL',
            qty=1, total=10)
        return order

    def test_resolves_both_streets_from_the_saved_ids(self):
        data = OrderDetailSerializer(self._order()).data

        self.assertEqual(data['bill_to_full_address'],
                         '12 Mall Road, Ludhiana, Punjab 141001')
        self.assertEqual(data['ship_to_full_address'],
                         'Plot 9, Focal Point, Ludhiana, Punjab 141010')

    def test_keeps_the_name_the_order_was_placed_against(self):
        """The street is ADDED, never substituted.

        `bill_to_address` is the historical record of what was chosen; if the
        party later moves, that name is still what the order was placed
        against and must not be rewritten by this.
        """
        data = OrderDetailSerializer(self._order()).data

        self.assertEqual(data['bill_to_address'], 'Head Office')
        self.assertEqual(data['ship_to_address'], 'Main Warehouse')

    def test_an_order_with_no_address_id_reports_none(self):
        # `bill_to_id` defaults to 0, which is not a row — every order placed
        # before the picker existed looks like this.
        data = OrderDetailSerializer(
            self._order(order_number='ADDR-2', bill_to_id=0, ship_to_id=0)).data

        self.assertIsNone(data['bill_to_full_address'])
        self.assertIsNone(data['ship_to_full_address'])

    def test_an_id_that_no_longer_resolves_reports_none(self):
        """A re-sync can delete an address row. The order must still serialise
        — a detail screen that 500s is worse than one missing a street."""
        data = OrderDetailSerializer(
            self._order(order_number='ADDR-3', bill_to_id=999999)).data

        self.assertIsNone(data['bill_to_full_address'])

    def test_a_blank_street_reports_none_rather_than_an_empty_string(self):
        # So the client can fall back on one check instead of two.
        blank = PartyAddress.objects.create(
            card_code='C1', address_name='No Street', address_type='B',
            full_address='   ', category='OIL')
        data = OrderDetailSerializer(
            self._order(order_number='ADDR-4', bill_to_id=blank.id)).data

        self.assertIsNone(data['bill_to_full_address'])

    def test_the_same_address_on_both_sides_is_looked_up_once(self):
        """Bill-to and ship-to are frequently the same premises."""
        order = self._order(order_number='ADDR-5', ship_to_id=self.bill.id)
        serializer = OrderDetailSerializer(order)

        with self.assertNumQueries(1):
            serializer.get_bill_to_full_address(order)
            serializer.get_ship_to_full_address(order)
