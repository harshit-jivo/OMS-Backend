from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from sap_sync.services.sync_service import SyncService


class SyncServiceMapOrderToSapTests(SimpleTestCase):
    def test_map_order_to_sap_uses_ordered_qty_instead_of_litres(self):
        item = SimpleNamespace(
            item_code="FG0000003",
            qty=10,
            boxes=240,
            ltrs=240,
            basic_price=1202,
            qty_scheme=0,
        )
        order = SimpleNamespace(
            id=63,
            card_code="CUSTA001070",
            delivery_date=date(2026, 4, 13),
            ship_to_address="SAKSHI SALES DELHI",
            bill_to_address="SAKSHI SALES DELHI",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG0000003")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 10.0)
        self.assertEqual(payload["DocumentLines"][0]["UnitPrice"], 1202.0)
        self.assertEqual(payload["DocDate"], "2026-04-11")
        self.assertEqual(payload["DocDueDate"], "2026-04-13")

    def test_map_order_to_sap_sends_each_saved_scheme_as_separate_lines(self):
        item = SimpleNamespace(
            item_code="FG-MAIN",
            category="OIL",
            qty=10,
            boxes=10,
            basic_price=100,
            market_price=0,
            qty_scheme=3,
            scheme_id=1,
            schemes=SimpleNamespace(all=lambda: [
                SimpleNamespace(scheme_id=1, qty_scheme=2),
                SimpleNamespace(scheme_id=2, qty_scheme=4),
            ]),
        )
        order = SimpleNamespace(
            id=72,
            card_code="CUSTA001070",
            delivery_date=date(2026, 4, 13),
            ship_to_address="SAKSHI SALES DELHI",
            bill_to_address="SAKSHI SALES DELHI",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        def scheme_codes(scheme_id):
            return {
                1: ["FG-SCHEME-A"],
                2: ["FG-SCHEME-B1", "FG-SCHEME-B2"],
            }[scheme_id]

        service = SyncService(triggered_by="test")

        with patch("sap_sync.services.sync_service.get_scheme_item_codes_for_combo", side_effect=scheme_codes), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(
            payload["DocumentLines"],
            [
                {"ItemCode": "FG-MAIN", "Quantity": 10.0, "UnitPrice": 100.0, "WarehouseCode": "GP-FG"},
                {"ItemCode": "FG-SCHEME-A", "Quantity": 2.0, "UnitPrice": 0.0, "WarehouseCode": "GP-FG"},
                {"ItemCode": "FG-SCHEME-B1", "Quantity": 4.0, "UnitPrice": 0.0, "WarehouseCode": "GP-FG"},
                {"ItemCode": "FG-SCHEME-B2", "Quantity": 4.0, "UnitPrice": 0.0, "WarehouseCode": "GP-FG"},
            ],
        )

    def test_map_order_to_sap_falls_back_when_qty_is_missing(self):
        item = SimpleNamespace(
            item_code="FG0000005",
            qty=0,
            boxes=12,
            ltrs=24,
            basic_price=0,
            market_price=0,
            qty_scheme=0,
        )
        order = SimpleNamespace(
            id=64,
            card_code="CUSTA001070",
            delivery_date=date(2026, 4, 13),
            ship_to_address="SAKSHI SALES DELHI",
            bill_to_address="SAKSHI SALES DELHI",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 12.0)

    def test_map_order_to_sap_uses_market_price_when_basic_price_is_zero(self):
        item = SimpleNamespace(
            item_code="FG0000007",
            qty=5,
            boxes=5,
            ltrs=10,
            basic_price=0,
            market_price=975.5,
            qty_scheme=0,
        )
        order = SimpleNamespace(
            id=65,
            card_code="CUSTA001070",
            delivery_date=date(2026, 4, 13),
            ship_to_address="SAKSHI SALES DELHI",
            bill_to_address="SAKSHI SALES DELHI",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(payload["DocumentLines"][0]["UnitPrice"], 975.5)

    def test_map_order_to_sap_adds_customer_po_number_when_present(self):
        item = SimpleNamespace(
            item_code="FG0000007",
            qty=5,
            boxes=5,
            ltrs=10,
            basic_price=975.5,
            qty_scheme=0,
        )
        order = SimpleNamespace(
            id=65,
            card_code="CUSTA001070",
            po_number=" PO-12345 ",
            delivery_date=date(2026, 4, 13),
            ship_to_address="SAKSHI SALES DELHI",
            bill_to_address="SAKSHI SALES DELHI",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(payload["NumAtCard"], "PO-12345")

    def test_map_order_to_sap_normalizes_legacy_web_quantity_shape(self):
        item = SimpleNamespace(
            item_code="FG0000003",
            item_name="Jivo Kachi Ghani 1 LTR",
            qty=240,
            pcs=20,
            boxes=12,
            ltrs=240,
            basic_price=1202,
            qty_scheme=0,
        )
        order = SimpleNamespace(
            id=63,
            card_code="CUSTA001070",
            delivery_date=date(2026, 4, 13),
            ship_to_address="SAKSHI SALES DELHI",
            bill_to_address="SAKSHI SALES DELHI",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 12.0)

    def test_map_order_to_sap_mirrors_punjab_combo_scheme_quantity(self):
        item = SimpleNamespace(
            item_code="FG-COMBO",
            item_name="Jivo 1 LTR + 1 LTR Combo",
            qty=240,
            pcs=20,
            boxes=12,
            ltrs=240,
            basic_price=1202,
            market_price=0,
            qty_scheme=60,
            scheme_id=99,
        )
        order = SimpleNamespace(
            id=66,
            card_code="PUNJAB-CUSTOMER",
            delivery_date=date(2026, 4, 13),
            ship_to_address="PUNJAB SHIP",
            bill_to_address="PUNJAB BILL",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch("sap_sync.services.sync_service.get_scheme_item_codes_for_combo", return_value=["FG-SCHEME"]), \
            patch("sap_sync.services.sync_service.get_party_combo_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.get_party_combo_component_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.get_party_item_unit_price", return_value=950.0), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        # Single scheme item + no separate component: FG-SCHEME appears at price (component) then free (scheme)
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG-SCHEME")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 950.0)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG-SCHEME")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)

    def test_map_order_to_sap_resolves_punjab_combo_scheme_as_separate_line(self):
        item = SimpleNamespace(
            item_code="FG-COMBO",
            item_name="Jivo 1 LTR + 1 LTR Combo",
            category="OIL",
            qty=240,
            pcs=20,
            boxes=12,
            ltrs=240,
            basic_price=1202,
            market_price=0,
            qty_scheme=0,
        )
        order = SimpleNamespace(
            id=68,
            card_code="PUNJAB-CUSTOMER",
            delivery_date=date(2026, 4, 13),
            ship_to_address="PUNJAB SHIP",
            bill_to_address="PUNJAB BILL",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch(
                "sap_sync.services.sync_service.get_party_product_scheme",
                return_value=SimpleNamespace(scheme_id=101, scheme_name="Jivo 1 LTR + 1 LTR Combo"),
            ), \
            patch("sap_sync.services.sync_service.get_scheme_item_codes_for_combo", return_value=["FG-SCHEME"]), \
            patch("sap_sync.services.sync_service.get_party_combo_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.get_party_combo_component_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.get_party_item_unit_price", return_value=950.0), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(len(payload["DocumentLines"]), 3)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG-COMBO")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG-SCHEME")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 950.0)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG-SCHEME")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)

    def test_map_order_to_sap_expands_punjab_combo_scheme_items(self):
        item = SimpleNamespace(
            item_code="FG0000003",
            item_name="Jivo 1 LTR + 1 LTR Combo",
            category="OIL",
            qty=20,
            pcs=20,
            boxes=400,
            ltrs=400,
            basic_price=1255,
            market_price=0,
            qty_scheme=5,
            scheme_id=102,
        )
        order = SimpleNamespace(
            id=69,
            card_code="CUSTA000007",
            delivery_date=date(2026, 4, 23),
            ship_to_address="ARJUN DASS & SONS PUNJAB NEW DELIVERY",
            bill_to_address="ARJUN DASS & SONS PUNJAB NEW BILLING",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch(
                "sap_sync.services.sync_service.get_scheme_item_codes_for_combo",
                return_value=["FG0000005", "FG-COMBO-ITEM"],
            ) as scheme_codes, \
            patch("sap_sync.services.sync_service.get_party_combo_component_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 21)):
            payload = service.map_order_to_sap(order)

        scheme_codes.assert_called_once_with(102, "PB")
        # Multiple scheme items + no components — no proxy, all scheme items added free.
        self.assertEqual(len(payload["DocumentLines"]), 3)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG0000003")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 20.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 20.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 0.0)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG-COMBO-ITEM")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 20.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)

    def test_map_order_to_sap_adds_party_assigned_combo_item(self):
        item = SimpleNamespace(
            item_code="FG0000003",
            item_name="Jivo 1 LTR + 1 LTR Combo",
            category="OIL",
            qty=20,
            pcs=20,
            boxes=400,
            ltrs=400,
            basic_price=1255,
            market_price=0,
            qty_scheme=5,
            scheme_id=103,
        )
        order = SimpleNamespace(
            id=70,
            card_code="CUSTA000007",
            delivery_date=date(2026, 4, 23),
            ship_to_address="ARJUN DASS & SONS PUNJAB NEW DELIVERY",
            bill_to_address="ARJUN DASS & SONS PUNJAB NEW BILLING",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch(
                "sap_sync.services.sync_service.get_scheme_item_codes_for_combo",
                return_value=["FG0000005"],
            ), \
            patch("sap_sync.services.sync_service.get_party_combo_component_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.get_party_item_unit_price", return_value=450.0), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 21)):
            payload = service.map_order_to_sap(order)

        # Single scheme item + no separate component: scheme item appears at price then free.
        self.assertEqual(len(payload["DocumentLines"]), 3)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG0000003")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 20.0)
        self.assertEqual(payload["DocumentLines"][0]["UnitPrice"], 1255.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 20.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 450.0)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 20.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)

    def test_map_order_to_sap_expands_unlinked_punjab_combo_by_item_name(self):
        item = SimpleNamespace(
            item_code="FG0000003",
            item_name="COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS",
            category="OIL",
            qty=5,
            pcs=4,
            boxes=20,
            ltrs=100,
            basic_price=1255,
            market_price=0,
            qty_scheme=5,
            scheme_id=103,
        )
        order = SimpleNamespace(
            id=71,
            card_code="CUSTA000007",
            delivery_date=date(2026, 4, 23),
            ship_to_address="ARJUN DASS & SONS PUNJAB NEW DELIVERY",
            bill_to_address="ARJUN DASS & SONS PUNJAB NEW BILLING",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch("sap_sync.services.sync_service.get_scheme_item_codes_for_combo", return_value=["FG0000005"]), \
            patch("sap_sync.services.sync_service.get_party_combo_item_codes", return_value=[]), \
            patch(
                "sap_sync.services.sync_service.get_party_combo_component_item_codes",
                return_value=[],
            ), \
            patch(
                "sap_sync.services.sync_service.get_party_item_unit_price",
                return_value=450.0,
            ), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 21)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(len(payload["DocumentLines"]), 3)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG0000003")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][0]["UnitPrice"], 1255.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 450.0)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)

    def test_map_order_to_sap_keeps_cold_press_combo_10_set_scheme_quantity(self):
        item = SimpleNamespace(
            item_code="FG-COLD-COMBO",
            item_name="Cold Press 1 LTR + 1 LTR Combo 10 Set",
            qty=240,
            pcs=20,
            boxes=12,
            ltrs=240,
            basic_price=1202,
            market_price=0,
            qty_scheme=60,
            scheme_id=100,
        )
        order = SimpleNamespace(
            id=67,
            card_code="PUNJAB-CUSTOMER",
            delivery_date=date(2026, 4, 13),
            ship_to_address="PUNJAB SHIP",
            bill_to_address="PUNJAB BILL",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch("sap_sync.services.sync_service.get_scheme_item_code_raw", return_value="FG-COLD-SCHEME"), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 11)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 12.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG-COLD-SCHEME")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 60.0)

    def test_map_order_to_sap_punjab_cold_press_5ltr_combo_mirrors_scheme_qty(self):
        """
        Real production data: COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS
        qty=20 (total pcs), pcs=4, boxes=5  →  ordered qty = 5 boxes
        Punjab party: scheme qty must mirror ordered qty (5), not the stored qty_scheme.
        """
        item = SimpleNamespace(
            item_code="FG0000003",
            item_name="COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS",
            category="OIL",
            qty=20,
            pcs=4,
            boxes=5,
            ltrs=100,
            basic_price=1255,
            market_price=0,
            qty_scheme=5,
            scheme_id=7,
        )
        item.__dict__["scheme_id"] = 7

        order = SimpleNamespace(
            id=22,
            card_code="CUSTA000007",
            delivery_date=date(2026, 4, 23),
            ship_to_address="PUNJAB SHIP",
            bill_to_address="PUNJAB BILL",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch("sap_sync.services.sync_service.get_scheme_item_codes_for_combo", return_value=["FG0000005"]), \
            patch("sap_sync.services.sync_service.get_party_combo_component_item_codes", return_value=[]), \
            patch("sap_sync.services.sync_service.get_party_item_unit_price", return_value=450.0), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 22)):
            payload = service.map_order_to_sap(order)

        # Single scheme item + no separate component: FG0000005 at price (component) then free (scheme).
        self.assertEqual(len(payload["DocumentLines"]), 3)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG0000003")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][0]["UnitPrice"], 1255.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 450.0)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)

    def test_map_order_to_sap_punjab_combo_with_scheme_expands_to_components_plus_scheme(self):
        """
        Production case: COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS, scheme_id=7.
        Line 1: FG0000003 (ordered item) at original price.
        Line 2: Extra Light Olive 1 LTR component (FG0000414) at its party price.
        Line 3: FG0000005 (scheme item) at price=0.
        """
        item = SimpleNamespace(
            item_code="FG0000003",
            item_name="COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS",
            category="OIL",
            qty=20,
            pcs=4,
            boxes=5,
            ltrs=100,
            basic_price=1255,
            market_price=0,
            qty_scheme=5,
            scheme_id=7,
        )
        item.__dict__["scheme_id"] = 7

        order = SimpleNamespace(
            id=22,
            card_code="CUSTA000007",
            delivery_date=date(2026, 4, 23),
            ship_to_address="PUNJAB SHIP",
            bill_to_address="PUNJAB BILL",
            dispatch_from_id=2,
            items=SimpleNamespace(all=lambda: [item]),
        )

        service = SyncService(triggered_by="test")

        with patch("orders.scheme_rules.is_punjab_party", return_value=True), \
            patch("sap_sync.services.sync_service.get_party_state_code", return_value="PB"), \
            patch("sap_sync.services.sync_service.get_party_combo_component_item_codes",
                  return_value=["FG0000414"]), \
            patch("sap_sync.services.sync_service.get_scheme_item_codes_for_combo", return_value=["FG0000005"]), \
            patch("sap_sync.services.sync_service.get_party_item_unit_price",
                  return_value=1228.45), \
            patch("sap_sync.services.sync_service.timezone.localdate", return_value=date(2026, 4, 22)):
            payload = service.map_order_to_sap(order)

        self.assertEqual(len(payload["DocumentLines"]), 3)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "FG0000003")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][0]["UnitPrice"], 1255.0)
        self.assertEqual(payload["DocumentLines"][1]["ItemCode"], "FG0000414")
        self.assertEqual(payload["DocumentLines"][1]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][1]["UnitPrice"], 1228.45)
        self.assertEqual(payload["DocumentLines"][2]["ItemCode"], "FG0000005")
        self.assertEqual(payload["DocumentLines"][2]["Quantity"], 5.0)
        self.assertEqual(payload["DocumentLines"][2]["UnitPrice"], 0.0)
