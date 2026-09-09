"""Tests for the v2 scheme engine (orders/scheme_engine.py).

Focus is the two things the redesign exists for: targeting a scheme at one vendor
versus a whole state, and computing a giveaway off the *free* half of a 1+1 combo.
"""

from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from orders.services import scheme_engine
from orders.models import Parties, Scheme, SchemeAssignment, SchemeBenefit, SchemeTrigger
from users.models import State


def make_scheme(code, *, priority=0, stackable=False, **kwargs):
    return Scheme.objects.create(code=code, name=code.title(), priority=priority,
                                 stackable=stackable, **kwargs)


class SchemeScopeTests(TestCase):
    """Who a scheme reaches."""

    def setUp(self):
        State.objects.create(name='Punjab', code='PB')
        State.objects.create(name='Haryana', code='HR')
        Parties.objects.create(card_code='P-PB-1', card_name='Punjab Dealer 1',
                               state='PB', main_group='NORTH')
        Parties.objects.create(card_code='P-PB-2', card_name='Punjab Dealer 2',
                               state='PB', main_group='NORTH')
        Parties.objects.create(card_code='P-HR-1', card_name='Haryana Dealer',
                               state='HR', main_group='NORTH')

    def _codes(self, card_code, category=''):
        ctx = scheme_engine.build_party_context(card_code, category)
        return {c.scheme.code for c in scheme_engine.get_candidate_schemes(ctx)}

    def _with_children(self, scheme):
        SchemeTrigger.objects.create(scheme=scheme, match_type='ALL')
        SchemeBenefit.objects.create(scheme=scheme, free_item_code='FREE-1',
                                     per_qty=10, free_qty=1)
        return scheme

    def test_state_scope_reaches_every_vendor_in_that_state(self):
        scheme = self._with_children(make_scheme('PB-WIDE'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')

        self.assertIn('PB-WIDE', self._codes('P-PB-1'))
        self.assertIn('PB-WIDE', self._codes('P-PB-2'))
        self.assertNotIn('PB-WIDE', self._codes('P-HR-1'))

    def test_state_scope_reaches_a_vendor_onboarded_later(self):
        scheme = self._with_children(make_scheme('PB-WIDE'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')

        Parties.objects.create(card_code='P-PB-NEW', card_name='New Dealer', state='PB')
        self.assertIn('PB-WIDE', self._codes('P-PB-NEW'))

    def test_party_scope_reaches_only_that_vendor(self):
        scheme = self._with_children(make_scheme('ONE-DEALER'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='PARTY', scope_value='P-PB-1')

        self.assertIn('ONE-DEALER', self._codes('P-PB-1'))
        self.assertNotIn('ONE-DEALER', self._codes('P-PB-2'))

    def test_exclusion_carves_one_vendor_out_of_a_state_scheme(self):
        scheme = self._with_children(make_scheme('PB-WIDE'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        SchemeAssignment.objects.create(scheme=scheme, scope_type='PARTY',
                                        scope_value='P-PB-2', is_exclusion=True)

        self.assertIn('PB-WIDE', self._codes('P-PB-1'))
        self.assertNotIn('PB-WIDE', self._codes('P-PB-2'))

    def test_party_assignment_wins_over_state_assignment(self):
        scheme = self._with_children(make_scheme('PB-WIDE'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        SchemeAssignment.objects.create(scheme=scheme, scope_type='PARTY', scope_value='P-PB-1')

        ctx = scheme_engine.build_party_context('P-PB-1')
        candidate = scheme_engine.get_candidate_schemes(ctx)[0]
        self.assertEqual(candidate.scope_type, 'PARTY')
        self.assertEqual(candidate.scope_value, 'P-PB-1')

    def test_category_narrows_a_scope(self):
        scheme = self._with_children(make_scheme('PB-OIL-ONLY'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE',
                                        scope_value='PB', category='OIL')

        self.assertIn('PB-OIL-ONLY', self._codes('P-PB-1', 'OIL'))
        self.assertNotIn('PB-OIL-ONLY', self._codes('P-PB-1', 'BEVERAGES'))

    def test_scheme_category_walls_off_other_categories(self):
        """An OIL scheme must not reach a MART or BEVERAGES order, however it
        was targeted — here by a party row that carries no category of its own."""
        scheme = self._with_children(make_scheme('OIL-ONLY', category='OIL'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='PARTY',
                                        scope_value='P-PB-1')

        self.assertIn('OIL-ONLY', self._codes('P-PB-1', 'OIL'))
        self.assertNotIn('OIL-ONLY', self._codes('P-PB-1', 'MART'))
        self.assertNotIn('OIL-ONLY', self._codes('P-PB-1', 'BEVERAGES'))

    def test_uncategorised_scheme_still_reaches_every_category(self):
        """Schemes created before the field existed keep applying everywhere."""
        scheme = self._with_children(make_scheme('ANY-CATEGORY'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='PARTY',
                                        scope_value='P-PB-1')

        self.assertIn('ANY-CATEGORY', self._codes('P-PB-1', 'OIL'))
        self.assertIn('ANY-CATEGORY', self._codes('P-PB-1', 'MART'))

    def test_inactive_scheme_is_not_a_candidate(self):
        scheme = self._with_children(make_scheme('OFF', is_active=False))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        self.assertNotIn('OFF', self._codes('P-PB-1'))

    def test_blank_state_does_not_collect_blank_scoped_rows(self):
        """A party with no state must not sweep up scope_value='' rows."""
        Parties.objects.create(card_code='P-NONE', card_name='Stateless')
        scheme = self._with_children(make_scheme('EMPTY-STATE'))
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='')
        self.assertNotIn('EMPTY-STATE', self._codes('P-NONE'))


class SchemeQuantityTests(TestCase):
    """How much a scheme gives."""

    def setUp(self):
        State.objects.create(name='Punjab', code='PB')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='PB')

    def _scheme(self, code, *, trigger, benefit, **scheme_kwargs):
        scheme = make_scheme(code, **scheme_kwargs)
        SchemeTrigger.objects.create(scheme=scheme, **trigger)
        SchemeBenefit.objects.create(scheme=scheme, **benefit)
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        return scheme

    def test_ratio_rule_floors_partial_slabs(self):
        self._scheme(
            'BUY10GET1',
            trigger={'match_type': 'ITEM', 'match_value': 'FG-1'},
            benefit={'free_item_code': 'FG-1', 'per_qty': 10, 'free_qty': 1},
        )
        lines = [{'item_code': 'FG-1', 'qty': 25}]
        proposals = scheme_engine.resolve_schemes('P1', '', lines)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].qty, Decimal('2'))
        self.assertEqual(proposals[0].benefit_item_code, 'FG-1')

    def test_min_qty_gates_the_scheme(self):
        self._scheme(
            'MIN20',
            trigger={'match_type': 'ITEM', 'match_value': 'FG-1', 'min_qty': 20},
            benefit={'free_item_code': 'FREE-1', 'per_qty': 0, 'free_qty': 5},
        )
        self.assertEqual(scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 19}]), [])
        self.assertEqual(len(scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 20}])), 1)

    def test_max_free_qty_caps_the_giveaway(self):
        self._scheme(
            'CAPPED',
            trigger={'match_type': 'ITEM', 'match_value': 'FG-1'},
            benefit={'free_item_code': 'FG-1', 'per_qty': 10, 'free_qty': 1, 'max_free_qty': 3},
        )
        proposals = scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 100}])
        self.assertEqual(proposals[0].qty, Decimal('3'))

    def test_null_free_item_code_means_the_trigger_item(self):
        self._scheme(
            'SAME-ITEM',
            trigger={'match_type': 'ITEM', 'match_value': 'FG-9'},
            benefit={'free_item_code': None, 'per_qty': 5, 'free_qty': 1},
        )
        proposals = scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-9', 'qty': 10}])
        self.assertEqual(proposals[0].benefit_item_code, 'FG-9')

    def test_ruleless_benefit_leaves_the_quantity_to_the_user(self):
        """What every migrated legacy scheme looks like — proposed, but unsized."""
        self._scheme(
            'LEGACY',
            trigger={'match_type': 'ITEM', 'match_value': 'FG-1'},
            benefit={'free_item_code': 'FREE-1', 'per_qty': 0, 'free_qty': 0},
        )
        proposals = scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 3}])
        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0].qty_is_user_supplied)
        self.assertEqual(proposals[0].qty, Decimal('0'))

    def test_scheme_category_is_enforced_per_line_on_a_mixed_order(self):
        """The call carries one category, but an order can mix them — Add Sales
        sends the first confirmed row's. The OIL scheme must fire on the OIL
        line only, even though the whole call was labelled OIL."""
        self._scheme(
            'OIL-ANY',
            trigger={'match_type': 'ALL'},
            benefit={'free_item_code': 'FREE-1', 'per_qty': 2, 'free_qty': 1},
            category='OIL',
        )
        lines = [
            {'item_code': 'FG-OIL', 'category': 'OIL', 'qty': 4},
            {'item_code': 'FG-MART', 'category': 'MART', 'qty': 4},
        ]
        proposals = scheme_engine.resolve_schemes('P1', 'OIL', lines)

        self.assertEqual([p.line_index for p in proposals], [0])
        self.assertEqual(proposals[0].trigger_item_code, 'FG-OIL')

    def test_sub_group_trigger_matches_case_insensitively(self):
        self._scheme(
            'BY-SUBGROUP',
            trigger={'match_type': 'SUB_GROUP', 'match_value': 'Olive'},
            benefit={'free_item_code': 'FREE-1', 'per_qty': 2, 'free_qty': 1},
        )
        lines = [{'item_code': 'FG-X', 'sub_group': 'OLIVE', 'qty': 4}]
        self.assertEqual(len(scheme_engine.resolve_schemes('P1', '', lines)), 1)


class ComboSchemeTests(TestCase):
    """1+1: a scheme computed on top of the combo's free half."""

    def setUp(self):
        State.objects.create(name='Punjab', code='PB')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='PB')

        self.scheme = make_scheme('ON-FREE-HALF')
        SchemeAssignment.objects.create(scheme=self.scheme, scope_type='STATE', scope_value='PB')

        # 10 combos ordered -> 10 free companion units auto-added.
        self.lines = [
            {'item_code': 'COMBO-CP1L', 'qty': 10},
            {'item_code': 'CP-1L', 'qty': 10, 'is_auto_free': True,
             'combo_source_code': 'COMBO-CP1L'},
        ]

    def _trigger(self, applies_to, **kwargs):
        SchemeTrigger.objects.create(scheme=self.scheme, match_type='ITEM',
                                     match_value='COMBO-CP1L', applies_to=applies_to, **kwargs)

    def test_free_line_scope_sizes_the_giveaway_off_the_free_half(self):
        self._trigger('FREE_LINE')
        SchemeBenefit.objects.create(scheme=self.scheme, free_item_code='CP-1L',
                                     per_qty=5, free_qty=1)

        proposals = scheme_engine.resolve_schemes('P1', '', self.lines)
        self.assertEqual(len(proposals), 1)
        # 10 free units / 5 = 2 extra units of the combo's free item, on top.
        self.assertEqual(proposals[0].qualifying_qty, Decimal('10'))
        self.assertEqual(proposals[0].qty, Decimal('2'))
        self.assertEqual(proposals[0].benefit_item_code, 'CP-1L')

    def test_both_scope_sums_paid_and_free(self):
        self._trigger('BOTH')
        SchemeBenefit.objects.create(scheme=self.scheme, free_item_code='CP-1L',
                                     per_qty=5, free_qty=1)
        proposals = scheme_engine.resolve_schemes('P1', '', self.lines)
        self.assertEqual(proposals[0].qualifying_qty, Decimal('20'))
        self.assertEqual(proposals[0].qty, Decimal('4'))

    def test_paid_line_scope_ignores_the_free_half(self):
        self._trigger('PAID_LINE')
        SchemeBenefit.objects.create(scheme=self.scheme, free_item_code='CP-1L',
                                     per_qty=5, free_qty=1)
        proposals = scheme_engine.resolve_schemes('P1', '', self.lines)
        self.assertEqual(proposals[0].qualifying_qty, Decimal('10'))

    def test_companion_line_never_triggers_on_its_own(self):
        """Otherwise the same free stock would be counted twice."""
        SchemeTrigger.objects.create(scheme=self.scheme, match_type='ITEM',
                                     match_value='CP-1L', applies_to='PAID_LINE')
        SchemeBenefit.objects.create(scheme=self.scheme, free_item_code='X', per_qty=1, free_qty=1)
        self.assertEqual(scheme_engine.resolve_schemes('P1', '', self.lines), [])


class SchemeConflictTests(TestCase):
    def setUp(self):
        State.objects.create(name='Punjab', code='PB')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='PB')

    def _scheme(self, code, priority, stackable, free_qty):
        scheme = make_scheme(code, priority=priority, stackable=stackable)
        SchemeTrigger.objects.create(scheme=scheme, match_type='ITEM', match_value='FG-1')
        SchemeBenefit.objects.create(scheme=scheme, free_item_code='FREE-1',
                                     per_qty=10, free_qty=free_qty)
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        return scheme

    def test_non_stackable_winner_applies_alone(self):
        self._scheme('HIGH', priority=10, stackable=False, free_qty=2)
        self._scheme('LOW', priority=1, stackable=True, free_qty=1)

        proposals = scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 10}])
        self.assertEqual([p.scheme_code for p in proposals], ['HIGH'])

    def test_stackable_schemes_combine(self):
        self._scheme('A', priority=10, stackable=True, free_qty=2)
        self._scheme('B', priority=5, stackable=True, free_qty=1)

        proposals = scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 10}])
        self.assertEqual(sorted(p.scheme_code for p in proposals), ['A', 'B'])

    def test_different_giveaway_items_do_not_conflict(self):
        a = self._scheme('A', priority=10, stackable=False, free_qty=1)
        SchemeBenefit.objects.filter(scheme=a).update(free_item_code='FREE-A')
        b = self._scheme('B', priority=1, stackable=False, free_qty=1)
        SchemeBenefit.objects.filter(scheme=b).update(free_item_code='FREE-B')

        proposals = scheme_engine.resolve_schemes('P1', '', [{'item_code': 'FG-1', 'qty': 10}])
        self.assertEqual(sorted(p.benefit_item_code for p in proposals), ['FREE-A', 'FREE-B'])


class MigrateSchemesV2CommandTests(TestCase):
    """The backfill from the legacy flat model."""

    def setUp(self):
        from users.models import PartyProductAssignment, SchemeProduct

        State.objects.create(name='Punjab', code='PB')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='PB')

        # The legacy shape: one offer spread over two rows sharing a scheme_name,
        # which is how a multi-item giveaway was expressed.
        self.legacy_a = SchemeProduct.objects.create(
            scheme_name='1 free pcs on 10 boxes', item_code='FREE-A', state_code='PB')
        self.legacy_b = SchemeProduct.objects.create(
            scheme_name='1 free pcs on 10 boxes', item_code='FREE-B', state_code='PB')

        PartyProductAssignment.objects.create(
            card_code='P1', item_code='FG-1', category='OIL',
            basic_rate=100, scheme=self.legacy_a)

    def _run(self, **opts):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('migrate_schemes_v2', stdout=out, **opts)
        return out.getvalue()

    def test_backfill_produces_one_scheme_with_both_giveaway_items(self):
        self._run()

        scheme = Scheme.objects.get(name='1 free pcs on 10 boxes')
        self.assertEqual(
            sorted(scheme.benefits.values_list('free_item_code', flat=True)),
            ['FREE-A', 'FREE-B'],
        )
        self.assertEqual(
            list(scheme.triggers.values_list('match_type', 'match_value')),
            [('ITEM', 'FG-1')],
        )

    def test_backfill_turns_state_code_into_a_real_scope(self):
        self._run()
        scheme = Scheme.objects.get(name='1 free pcs on 10 boxes')

        self.assertTrue(scheme.assignments.filter(
            scope_type='STATE', scope_value='PB').exists())
        self.assertTrue(scheme.assignments.filter(
            scope_type='PARTY', scope_value='P1', category='OIL').exists())

    def test_migrated_benefits_leave_the_quantity_to_the_user(self):
        """Behaviour-preserving: no ratio is invented for a legacy scheme."""
        self._run()
        scheme = Scheme.objects.get(name='1 free pcs on 10 boxes')
        for benefit in scheme.benefits.all():
            self.assertEqual(benefit.per_qty, Decimal('0'))
            self.assertEqual(benefit.free_qty, Decimal('0'))

    def test_backfill_is_idempotent(self):
        self._run()
        self._run()
        self.assertEqual(Scheme.objects.filter(name='1 free pcs on 10 boxes').count(), 1)
        self.assertEqual(SchemeBenefit.objects.count(), 2)
        self.assertEqual(SchemeTrigger.objects.count(), 1)
        self.assertEqual(SchemeAssignment.objects.count(), 2)

    def test_dry_run_writes_nothing(self):
        self._run(dry_run=True)
        self.assertEqual(Scheme.objects.count(), 0)

    def test_history_is_snapshotted_so_scheme_edits_cannot_rewrite_it(self):
        from orders.models import Order, OrderItem, OrderItemScheme, OrderStatus

        status = OrderStatus.objects.create(code='CREATED', name='Created')
        order = Order.objects.create(order_number='SO-1', card_code='P1',
                                     card_name='Dealer', status=status)
        order_item = OrderItem.objects.create(order=order, item_code='FG-1', qty=10)
        row = OrderItemScheme.objects.create(
            order_item=order_item, scheme=self.legacy_a, qty_scheme=Decimal('2'))

        self._run()

        row.refresh_from_db()
        self.assertEqual(row.benefit_item_code, 'FREE-A')
        self.assertEqual(row.computed_qty, Decimal('2'))

        # Editing the legacy row afterwards must not change what this order ships.
        self.legacy_a.item_code = 'SOMETHING-ELSE'
        self.legacy_a.save(update_fields=['item_code'])
        row.refresh_from_db()
        self.assertEqual(row.benefit_item_code, 'FREE-A')


class SchemeV2OrderLineTests(TestCase):
    """The chain from an Add Sales payload to what SAP is told to ship."""

    def setUp(self):
        # `_extract_order_item_schemes` now runs `_scheme_v2_category_allows`,
        # a save-time mirror of the engine's category wall added after this
        # class was written. It requires BOTH the line category and the
        # scheme's own category to be present and identical — a blank anywhere
        # is a mismatch and the scheme is dropped, so a stale or hand-rolled
        # client cannot persist a scheme the UI would never have offered.
        #
        # The fixture had neither, so every entry was silently dropped and the
        # test read `0 != 1`. It needs a real Scheme row to resolve against.
        self.scheme_v2 = make_scheme('V2-CAT', category='OIL')

    def _entry(self, **overrides):
        from orders.services.order_items import _extract_order_item_schemes

        item = {
            "item_code": "FG-1",
            "qty": 3,
            "category": "OIL",
            "schemes": [{
                "scheme_v2_id": self.scheme_v2.pk,
                "benefit_id": 55,
                "benefit_item_code": "FG-FREE",
                "scheme_qty": 6,
                "computed_qty": 6,
                "scope_type": "STATE",
                "scope_value": "DL",
                **overrides,
            }],
        }
        return _extract_order_item_schemes(item, float, bool)

    def test_v2_entry_survives_without_a_legacy_scheme_id(self):
        """The engine's giveaways have no scheme_product row to point at."""
        entries = self._entry()
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["scheme"])
        self.assertEqual(entries[0]["scheme_v2_id"], self.scheme_v2.pk)
        self.assertEqual(entries[0]["benefit_item_code"], "FG-FREE")
        self.assertEqual(entries[0]["scope_value"], "DL")

    def test_zero_qty_entry_is_dropped(self):
        self.assertEqual(self._entry(scheme_qty=0), [])

    def test_sap_ships_the_snapshot_not_a_re_resolved_item(self):
        from orders.models import Order, OrderItem, OrderItemScheme, OrderStatus
        from sap_sync.services.sync_service import _get_order_item_scheme_entries

        status = OrderStatus.objects.create(code='CREATED', name='Created')
        order = Order.objects.create(order_number='SO-V2', card_code='P1',
                                     card_name='Dealer', status=status)
        order_item = OrderItem.objects.create(order=order, item_code='FG-1', qty=3)
        OrderItemScheme.objects.create(
            order_item=order_item,
            scheme=None,               # no legacy row
            scheme_v2=make_scheme('V2-LINE'),
            benefit_item_code='FG-FREE',
            qty_scheme=Decimal('6'),
        )

        entries = _get_order_item_scheme_entries(
            OrderItem.objects.prefetch_related('schemes').get(pk=order_item.pk),
            card_code='P1', item_code='FG-1', category='OIL',
        )
        self.assertEqual(len(entries), 1)
        _raw_scheme_id, qty, snapshot = entries[0]
        self.assertEqual(qty, 6.0)
        self.assertEqual(snapshot, 'FG-FREE')


class OrderCreateResolvesSchemesTests(TestCase):
    """The order-create path must resolve giveaways itself.

    The client echoes back the proposals it displayed, but an older client, a
    resumed draft, or a screen that never called the preview endpoint sends
    none — and the SAP push builds its free lines from OrderItemScheme, so the
    customer's free stock would silently never ship.
    """

    def setUp(self):
        from orders.models import OrderStatus

        State.objects.create(name='Delhi', code='DL')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='DL')
        self.status = OrderStatus.objects.create(code='CREATED', name='Created')

        self.scheme = make_scheme('BUY10GET1', category='OIL')
        SchemeTrigger.objects.create(scheme=self.scheme, match_type='ITEM', match_value='FG-1')
        SchemeBenefit.objects.create(scheme=self.scheme, free_item_code='FG-FREE',
                                     per_qty=10, free_qty=1)
        SchemeAssignment.objects.create(scheme=self.scheme, scope_type='STATE', scope_value='DL')

    def _order_with(self, lines):
        from orders.models import Order, OrderItem

        order = Order.objects.create(order_number='SO-E', card_code='P1',
                                     card_name='Dealer', status=self.status)
        created = [
            OrderItem.objects.create(
                order=order,
                item_code=line['item_code'],
                category=line.get('category', ''),
                qty=line.get('qty', 0),
            )
            for line in lines
        ]
        return order, created

    def test_giveaway_is_saved_even_when_the_client_sends_none(self):
        from orders.models import OrderItemScheme
        from orders.services.order_items import _apply_engine_schemes

        lines = [{'item_code': 'FG-1', 'category': 'OIL', 'qty': 30}]
        order, created = self._order_with(lines)

        _apply_engine_schemes(order, lines, created)

        rows = OrderItemScheme.objects.filter(order_item__order=order)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].benefit_item_code, 'FG-FREE')
        self.assertEqual(rows[0].qty_scheme, Decimal('3'))
        self.assertEqual(rows[0].scheme_v2_id, self.scheme.id)
        self.assertEqual(rows[0].scope_value, 'DL')

        created[0].refresh_from_db()
        self.assertEqual(created[0].qty_scheme, Decimal('3'))
        self.assertTrue(created[0].is_scheme_visible)

    def test_a_giveaway_the_client_already_sent_is_not_duplicated(self):
        from orders.models import OrderItemScheme
        from orders.services.order_items import _apply_engine_schemes

        lines = [{'item_code': 'FG-1', 'category': 'OIL', 'qty': 30}]
        order, created = self._order_with(lines)
        # What the client saved, with a hand-typed quantity.
        OrderItemScheme.objects.create(
            order_item=created[0], scheme=None, scheme_v2=self.scheme,
            benefit_item_code='FG-FREE', qty_scheme=Decimal('5'),
            is_manual_override=True,
        )

        _apply_engine_schemes(order, lines, created)

        rows = OrderItemScheme.objects.filter(order_item__order=order)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].qty_scheme, Decimal('5'))
        self.assertTrue(rows[0].is_manual_override)

    def test_a_scheme_for_another_category_is_not_added(self):
        from orders.models import OrderItemScheme
        from orders.services.order_items import _apply_engine_schemes

        lines = [{'item_code': 'FG-1', 'category': 'MART', 'qty': 30}]
        order, created = self._order_with(lines)

        _apply_engine_schemes(order, lines, created)

        self.assertEqual(OrderItemScheme.objects.filter(order_item__order=order).count(), 0)

    def test_a_ruleless_scheme_proposes_nothing(self):
        """Quantity still typed by hand — a zero-qty free line would ship nothing."""
        from orders.models import OrderItemScheme
        from orders.services.order_items import _apply_engine_schemes

        self.scheme.benefits.update(per_qty=0, free_qty=0)
        lines = [{'item_code': 'FG-1', 'category': 'OIL', 'qty': 30}]
        order, created = self._order_with(lines)

        _apply_engine_schemes(order, lines, created)

        self.assertEqual(OrderItemScheme.objects.filter(order_item__order=order).count(), 0)


class SchemeUomTests(TestCase):
    """Schemes measure in pieces or cartons, and never silently swap the two."""

    def setUp(self):
        State.objects.create(name='Punjab', code='PB')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='PB')

    def _scheme(self, code, *, min_uom, per_qty):
        scheme = make_scheme(code)
        SchemeTrigger.objects.create(scheme=scheme, match_type='ITEM',
                                     match_value='FG-1', min_uom=min_uom)
        SchemeBenefit.objects.create(scheme=scheme, free_item_code='FREE-1',
                                     per_qty=per_qty, free_qty=1)
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        return scheme

    # A real Add Sales line: 120 cartons of a 4-per-carton pack = 480 pieces.
    LINE = {'item_code': 'FG-1', 'boxes': 120, 'qty': 480, 'pcs': 4, 'ltrs': 2400}

    def test_pcs_measures_the_pieces_ordered_not_the_pack_size(self):
        """`pcs` on an order line is sal_factor2 — the carton size, a constant.
        Measuring against it would compare the slab to 4, not to 480."""
        self._scheme('PER-10-PCS', min_uom='PCS', per_qty=10)
        proposals = scheme_engine.resolve_schemes('P1', '', [self.LINE])
        self.assertEqual(proposals[0].qualifying_qty, Decimal('480'))
        self.assertEqual(proposals[0].qty, Decimal('48'))

    def test_box_measures_cartons(self):
        self._scheme('PER-10-BOX', min_uom='BOX', per_qty=10)
        proposals = scheme_engine.resolve_schemes('P1', '', [self.LINE])
        self.assertEqual(proposals[0].qualifying_qty, Decimal('120'))
        self.assertEqual(proposals[0].qty, Decimal('12'))

    def test_box_slab_does_not_fall_back_to_pieces(self):
        """A blank `boxes` must disqualify, not quietly measure 480 pieces as
        480 cartons and hand out ~40x the intended free stock."""
        self._scheme('PER-10-BOX', min_uom='BOX', per_qty=10)
        line = {'item_code': 'FG-1', 'qty': 480}
        self.assertEqual(scheme_engine.resolve_schemes('P1', '', [line]), [])

    def test_only_pieces_and_boxes_are_offered(self):
        from orders.models import UOM_CHOICES

        self.assertEqual([code for code, _label in UOM_CHOICES], ['PCS', 'BOX'])


class SchemeBoxToPiecesTests(TestCase):
    """A carton giveaway must reach SAP as pieces.

    A SAP DocumentLine quantity is always single units — the paid line sends the
    order's `qty`, which is pieces. Shipping "1 BOX" unconverted delivers one
    bottle instead of one carton.
    """

    def setUp(self):
        from sap_sync.models import Product

        State.objects.create(name='Punjab', code='PB')
        Parties.objects.create(card_code='P1', card_name='Dealer', state='PB')
        # 4 pieces to a carton, the same shape as POMACE OLIVE 5 LTR TIN 4 PCS.
        Product.objects.create(item_code='FREE-4PK', item_name='Free 4-pack',
                               category='OIL', sal_factor2=4, is_active='Y')
        Product.objects.create(item_code='FREE-LOOSE', item_name='Free single',
                               category='OIL', sal_factor2=1, is_active='Y')

    def _scheme(self, code, *, free_item, free_uom, free_qty=1):
        scheme = make_scheme(code)
        SchemeTrigger.objects.create(scheme=scheme, match_type='ITEM',
                                     match_value='FG-1', min_uom='PCS')
        SchemeBenefit.objects.create(scheme=scheme, free_item_code=free_item,
                                     free_uom=free_uom, per_qty=0, free_qty=free_qty)
        SchemeAssignment.objects.create(scheme=scheme, scope_type='STATE', scope_value='PB')
        return scheme

    LINE = {'item_code': 'FG-1', 'qty': 10, 'boxes': 10}

    def test_box_benefit_converts_by_the_giveaway_items_pack_size(self):
        self._scheme('ONE-BOX', free_item='FREE-4PK', free_uom='BOX')
        proposal = scheme_engine.resolve_schemes('P1', '', [self.LINE])[0]

        self.assertEqual(proposal.qty, Decimal('1'))          # as written
        self.assertEqual(proposal.free_uom, 'BOX')
        self.assertEqual(proposal.qty_pieces, Decimal('4'))   # as shipped

    def test_pcs_benefit_needs_no_conversion(self):
        self._scheme('TEN-PCS', free_item='FREE-4PK', free_uom='PCS', free_qty=10)
        proposal = scheme_engine.resolve_schemes('P1', '', [self.LINE])[0]
        self.assertEqual(proposal.qty, Decimal('10'))
        self.assertEqual(proposal.qty_pieces, Decimal('10'))

    def test_unknown_product_falls_back_to_one_piece_per_box(self):
        """Never silently multiply by zero — an unmapped code ships as written."""
        self._scheme('MYSTERY', free_item='NOT-IN-SAP', free_uom='BOX', free_qty=3)
        proposal = scheme_engine.resolve_schemes('P1', '', [self.LINE])[0]
        self.assertEqual(proposal.qty_pieces, Decimal('3'))

    def test_pieces_figure_is_what_reaches_the_sap_mapper(self):
        from orders.models import Order, OrderItem, OrderItemScheme, OrderStatus
        from sap_sync.services.sync_service import _get_order_item_scheme_entries

        status = OrderStatus.objects.create(code='CREATED', name='Created')
        order = Order.objects.create(order_number='SO-BOX', card_code='P1',
                                     card_name='Dealer', status=status)
        item = OrderItem.objects.create(order=order, item_code='FG-1', qty=10)
        OrderItemScheme.objects.create(
            order_item=item,
            scheme_v2=make_scheme('BOX-LINE'),
            benefit_item_code='FREE-4PK',
            benefit_uom='BOX',
            benefit_qty=Decimal('1'),
            qty_scheme=Decimal('4'),   # pieces — what ships
        )

        entries = _get_order_item_scheme_entries(
            OrderItem.objects.prefetch_related('schemes').get(pk=item.pk),
            card_code='P1', item_code='FG-1', category='OIL',
        )
        _scheme_id, qty, snapshot = entries[0]
        self.assertEqual(qty, 4.0)
        self.assertEqual(snapshot, 'FREE-4PK')


class SchemeProposalShapeTests(SimpleTestCase):
    """The giveaway carries its own NAME to the client.

    A STATE- or VENDOR-scoped scheme deliberately gives away items the party
    holds no assignment for, so the order form's catalogues cannot name them.
    It fell back to the bare item code, and the Items step showed
    "FG0000031" where every other line showed a product name.

    No database: this pins the wire shape, which is the half the client
    depends on. `_apply_box_factors` fills the value from sap_products.
    """

    def _proposal(self, **over):
        base = dict(
            line_index=0,
            trigger_item_code="FG0000012",
            scheme_id=1,
            scheme_code="SCH-1",
            scheme_name="BUY 1 GET 1 FREE",
            benefit_id=2,
            benefit_item_code="FG0000031",
            free_uom="PCS",
            qty=Decimal("960"),
            qty_pieces=Decimal("960"),
            qualifying_qty=Decimal("480"),
            scope_type="STATE",
            scope_value="DL",
        )
        base.update(over)
        return scheme_engine.SchemeProposal(**base)

    def test_the_payload_carries_the_benefit_item_name(self):
        body = self._proposal(benefit_item_name="EXTRA LIGHT OLIVE 1 LTR").as_dict()

        self.assertEqual(body["benefit_item_name"], "EXTRA LIGHT OLIVE 1 LTR")

    def test_the_name_defaults_to_empty_not_missing(self):
        # The client falls back to the item code on an empty string. A MISSING
        # key would be an undefined read on a typed field instead.
        body = self._proposal().as_dict()

        self.assertIn("benefit_item_name", body)
        self.assertEqual(body["benefit_item_name"], "")

    def test_it_did_not_disturb_the_rest_of_the_contract(self):
        # The client reads every one of these; adding a field must not move one.
        body = self._proposal().as_dict()

        for key in (
            "line_index", "trigger_item_code", "scheme_id", "scheme_code",
            "scheme_name", "benefit_id", "benefit_item_code", "free_uom",
            "qty", "qty_pieces", "qualifying_qty", "scope_type", "scope_value",
            "priority", "stackable", "qty_is_user_supplied",
        ):
            self.assertIn(key, body)
