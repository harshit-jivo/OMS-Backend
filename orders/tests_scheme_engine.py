"""Tests for the v2 scheme engine (orders/scheme_engine.py).

Focus is the two things the redesign exists for: targeting a scheme at one vendor
versus a whole state, and computing a giveaway off the *free* half of a 1+1 combo.
"""

from decimal import Decimal

from django.test import TestCase

from orders import scheme_engine
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

    def _entry(self, **overrides):
        from orders.views import _extract_order_item_schemes

        item = {
            "item_code": "FG-1",
            "qty": 3,
            "schemes": [{
                "scheme_v2_id": 7,
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
        self.assertEqual(entries[0]["scheme_v2_id"], 7)
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
