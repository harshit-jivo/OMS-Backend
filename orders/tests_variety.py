"""One Premium / Commodity / Others rule for an order line.

The item badge and the variety cost cards used to classify with two different
sub-group lists, so an olive line read Premium on its card and was summed
into Commodity. `item_variety_type` is now the only rule: SAP's own item type
first, the sub-group lists only where SAP leaves it blank.

Run with::

    python manage.py test orders.tests_variety --settings=OMS.test_settings
"""
from decimal import Decimal

from django.test import TestCase

from orders.models import Order, OrderItem, OrderStatus
from orders.serializers import OrderDetailSerializer, item_variety_type
from sap_sync.models import Product


def _product(code, sub_group, sap_type):
    return Product.objects.create(item_code=code, category="OIL", sub_group=sub_group, type=sap_type)


class ItemVarietyTypeTests(TestCase):

    def _item(self, code, total=Decimal("100")):
        status, _ = OrderStatus.objects.get_or_create(name="Billing", code="BILLING")
        order = Order.objects.create(order_number=f"ORD-V-{code}", status=status)
        return OrderItem.objects.create(order=order, item_code=code, category="OIL", total=total)

    def test_sap_type_wins_over_the_sub_group(self):
        _product("POMACE", "OLIVE", "COMMODITY")
        self.assertEqual(item_variety_type(self._item("POMACE")), "COMMODITY")

    def test_a_blank_sap_type_falls_back_to_the_sub_group(self):
        _product("EVOO", "OLIVE", "")
        _product("KG", "MUSTARD", None)
        self.assertEqual(item_variety_type(self._item("EVOO")), "PREMIUM")
        self.assertEqual(item_variety_type(self._item("KG")), "COMMODITY")

    def test_unknown_products_are_others(self):
        self.assertEqual(item_variety_type(self._item("NOPE")), "OTHERS")

    def test_the_cost_cards_agree_with_the_badges(self):
        _product("EVOO", "OLIVE", "")
        item = self._item("EVOO", Decimal("250"))

        costs = OrderDetailSerializer().get_vareity_cost(item.order)

        self.assertEqual(costs["premium_total"], Decimal("250"))
        self.assertEqual(costs["commodity_price"], Decimal("0"))
