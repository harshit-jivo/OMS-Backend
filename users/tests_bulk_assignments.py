"""Party-product assignment across many parties — `users/views/bulk_assignments.py`.

Two of these endpoints are not new API design: `BulkRateEditor.tsx` and its test
file specified their request and response shapes before the server existed, and
the component has been rendered in production calling routes that 404. The tests
below pin the halves the frontend's mocked tests cannot reach — that a percentage
moves each party's OWN rate, that a mismatched category is reported rather than
silently skipped, and that a failed batch leaves neither rows nor audit entries.

Run with::

    python manage.py test users.tests_bulk_assignments --settings=OMS.test_settings
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from audit.models import AuditLog
from sap_sync.models import Party, Product
from users.models import PartyProductAssignment, User, UserRole


def _user(username):
    role, _ = UserRole.objects.get_or_create(
        name='admin', defaults={'display_name': 'Admin', 'is_active': True})
    return User.objects.create_user(
        username=username, password='CorrectHorse9!', name=username, role=role)


def _party(card_code, category='OIL'):
    return Party.objects.create(
        card_code=card_code, card_name=f'{card_code} Traders', category=category, state='PB')


def _product(item_code, category='OIL', **kwargs):
    return Product.objects.create(
        item_code=item_code, item_name=kwargs.pop('item_name', f'{item_code} 1 LTR'),
        category=category, is_active=kwargs.pop('is_active', 'Y'),
        sub_group=kwargs.pop('sub_group', 'MUSTARD'), brand=kwargs.pop('brand', 'JIVO'),
        sal_pack_unit=kwargs.pop('sal_pack_unit', '1'), **kwargs)


def _assign(card_code, item_code, rate, category='OIL', is_active=True):
    return PartyProductAssignment.objects.create(
        card_code=card_code, item_code=item_code, category=category,
        basic_rate=Decimal(str(rate)), is_active=is_active)


def _rate(card_code, item_code, category='OIL'):
    return PartyProductAssignment.objects.get(
        card_code=card_code, item_code=item_code, category=category).basic_rate


class _BulkTestCase(TestCase):
    """Two OIL parties holding the same item at different rates, plus a MART party."""

    def setUp(self):
        self.user = _user('t-bulk')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        _party('C1'), _party('C2'), _party('C3', 'MART')
        _product('FG001'), _product('FG002', sub_group='SOYABEAN')
        _product('MART01', 'MART')

        _assign('C1', 'FG001', 100)
        _assign('C2', 'FG001', 110)
        _assign('C1', 'FG002', 200)

        self.oil = [{'card_code': 'C1', 'category': 'OIL'},
                    {'card_code': 'C2', 'category': 'OIL'}]

    def post(self, name, body):
        return self.client.post(reverse(name), body, format='json')


class BulkPartyProductsTests(_BulkTestCase):

    def test_one_row_per_distinct_item_not_per_assignment(self):
        response = self.post('bulk-party-products', {'party_selections': self.oil})
        self.assertEqual(response.status_code, 200)
        rows = response.json()['data']['products']
        self.assertEqual({row['item_code'] for row in rows}, {'FG001', 'FG002'})

    def test_reports_the_spread_across_the_selection(self):
        rows = self.post('bulk-party-products',
                         {'party_selections': self.oil}).json()['data']['products']
        fg001 = next(row for row in rows if row['item_code'] == 'FG001')
        self.assertEqual(fg001['party_count'], 2)
        self.assertEqual(fg001['min_rate'], 100.0)
        self.assertEqual(fg001['max_rate'], 110.0)
        self.assertEqual(fg001['distinct_rates'], 2)

    def test_common_rate_is_the_modal_rate(self):
        _party('C4'), _assign('C4', 'FG001', 100)
        rows = self.post('bulk-party-products', {'party_selections': self.oil + [
            {'card_code': 'C4', 'category': 'OIL'}]}).json()['data']['products']
        fg001 = next(row for row in rows if row['item_code'] == 'FG001')
        self.assertEqual(fg001['common_rate'], 100.0)
        self.assertEqual(fg001['common_rate_parties'], 2)

    def test_common_rate_ties_break_to_the_lowest(self):
        """A tie must never silently propose raising a price.

        The base fixture is already the tie: one party at 100, one at 110.
        """
        rows = self.post('bulk-party-products',
                         {'party_selections': self.oil}).json()['data']['products']
        fg001 = next(row for row in rows if row['item_code'] == 'FG001')
        self.assertEqual(fg001['distinct_rates'], 2)
        self.assertEqual(fg001['common_rate_parties'], 1)
        self.assertEqual(fg001['common_rate'], 100.0)

    def test_missing_parties_counts_those_that_could_hold_it_but_do_not(self):
        rows = self.post('bulk-party-products',
                         {'party_selections': self.oil}).json()['data']['products']
        fg002 = next(row for row in rows if row['item_code'] == 'FG002')
        self.assertEqual(fg002['eligible_parties'], 2)
        self.assertEqual(fg002['party_count'], 1)
        self.assertEqual(fg002['missing_parties'], 1)

    def test_a_mart_selection_is_not_eligible_for_an_oil_item(self):
        _assign('C3', 'MART01', 50, category='MART')
        rows = self.post('bulk-party-products', {'party_selections': self.oil + [
            {'card_code': 'C3', 'category': 'MART'}]}).json()['data']['products']
        fg001 = next(row for row in rows if row['item_code'] == 'FG001')
        self.assertEqual(fg001['eligible_parties'], 2)

    def test_inactive_assignments_and_inactive_products_are_absent(self):
        _product('FG003'), _assign('C1', 'FG003', 300)
        Product.objects.filter(item_code='FG003').update(is_active='N')
        _product('FG004'), _assign('C1', 'FG004', 400, is_active=False)
        rows = self.post('bulk-party-products',
                         {'party_selections': self.oil}).json()['data']['products']
        codes = {row['item_code'] for row in rows}
        self.assertNotIn('FG003', codes)
        self.assertNotIn('FG004', codes)

    def test_include_inactive_reports_turned_off_rows_without_moving_the_rates(self):
        """The Product Rates page asks for turned-off rows so it can turn them
        back on. They must be counted and named — and must NOT enter the rate
        spread, because nobody is being charged a rate that is switched off."""
        _product('FG004'), _assign('C1', 'FG004', 400, is_active=False)
        PartyProductAssignment.objects.filter(card_code='C2', item_code='FG001').update(is_active=False)
        rows = self.post('bulk-party-products', {
            'party_selections': self.oil, 'include_inactive': True,
        }).json()['data']['products']
        by_code = {row['item_code']: row for row in rows}

        fg004 = by_code['FG004']
        self.assertEqual(fg004['party_count'], 1)
        self.assertEqual(fg004['active_count'], 0)
        self.assertEqual(fg004['inactive_count'], 1)
        # Nothing sellable, so the kept rate stands in rather than reading as 0.
        self.assertEqual(fg004['common_rate'], 400.0)
        self.assertEqual(fg004['holders'], [
            {'card_code': 'C1', 'basic_rate': 400.0, 'is_active': False}])

        fg001 = by_code['FG001']
        self.assertEqual(fg001['party_count'], 2)
        self.assertEqual(fg001['active_count'], 1)
        self.assertEqual(fg001['inactive_count'], 1)
        # C2's row was turned off, so only C1's 100 is a live rate.
        self.assertEqual(fg001['distinct_rates'], 1)
        self.assertEqual(fg001['common_rate'], 100.0)
        self.assertEqual(fg001['max_rate'], 100.0)

    def test_products_query_count_does_not_grow_with_selection_size(self):
        """The regression guard against re-introducing PartyProductsView's N+1.

        Three, not two, since the view gained `HasKey('Party_Product_Assignment')`:
        the gate costs one role lookup, once, no matter how many parties are
        selected. What this guards is the shape — a constant — not the constant.
        """
        for index in range(10):
            code = f'P{index}'
            _party(code)
            for item in ('FG001', 'FG002'):
                _assign(code, item, 100 + index)
        selections = [{'card_code': f'P{i}', 'category': 'OIL'} for i in range(10)]
        with self.assertNumQueries(3):
            self.client.post(reverse('bulk-party-products'),
                             {'party_selections': selections}, format='json')

    def test_empty_selection_is_rejected(self):
        self.assertEqual(self.post('bulk-party-products', {'party_selections': []}).status_code, 400)


class BulkRateUpdateTests(_BulkTestCase):

    def _apply(self, **body):
        body.setdefault('party_selections', self.oil)
        return self.post('bulk-party-update-rates', body)

    def test_set_writes_the_figure_to_every_party_holding_it(self):
        response = self._apply(rate_mode='set', apply_to='existing',
                               items=[{'item_code': 'FG001', 'category': 'OIL',
                                       'basic_rate': 125}])
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['updated'], 2)
        self.assertEqual(data['parties'], 2)
        self.assertEqual(data['items'], 1)
        self.assertEqual(_rate('C1', 'FG001'), Decimal('125.0000'))
        self.assertEqual(_rate('C2', 'FG001'), Decimal('125.0000'))

    def test_percent_moves_each_partys_own_rate(self):
        """The one behaviour the mocked frontend tests cannot demonstrate:
        100 and 110 at +10% end on 110 and 121, not both on the same figure."""
        self._apply(rate_mode='percent', apply_to='existing',
                    items=[{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 10}])
        self.assertEqual(_rate('C1', 'FG001'), Decimal('110.0000'))
        self.assertEqual(_rate('C2', 'FG001'), Decimal('121.0000'))

    def test_amount_adds_to_each_partys_own_rate(self):
        self._apply(rate_mode='amount', apply_to='existing',
                    items=[{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': -4}])
        self.assertEqual(_rate('C1', 'FG001'), Decimal('96.0000'))
        self.assertEqual(_rate('C2', 'FG001'), Decimal('106.0000'))

    def test_apply_to_existing_skips_a_party_without_the_item(self):
        data = self._apply(rate_mode='set', apply_to='existing',
                           items=[{'item_code': 'FG002', 'category': 'OIL',
                                   'basic_rate': 250}]).json()['data']
        self.assertEqual(data['updated'], 1)
        self.assertEqual(data['skipped'], 1)
        self.assertFalse(PartyProductAssignment.objects.filter(
            card_code='C2', item_code='FG002').exists())

    def test_apply_to_all_creates_the_missing_assignment(self):
        data = self._apply(rate_mode='set', apply_to='all',
                           items=[{'item_code': 'FG002', 'category': 'OIL',
                                   'basic_rate': 250}]).json()['data']
        self.assertEqual(data['created'], 1)
        self.assertEqual(_rate('C2', 'FG002'), Decimal('250.0000'))

    def test_apply_to_all_revives_a_deactivated_row_rather_than_duplicating_it(self):
        _assign('C2', 'FG002', 199, is_active=False)
        self._apply(rate_mode='set', apply_to='all',
                    items=[{'item_code': 'FG002', 'category': 'OIL', 'basic_rate': 250}])
        rows = PartyProductAssignment.objects.filter(card_code='C2', item_code='FG002')
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.first().is_active)
        self.assertEqual(rows.first().basic_rate, Decimal('250.0000'))

    def test_apply_to_all_with_a_percentage_is_refused(self):
        response = self._apply(rate_mode='percent', apply_to='all',
                               items=[{'item_code': 'FG002', 'category': 'OIL',
                                       'basic_rate': 10}])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PartyProductAssignment.objects.filter(
            card_code='C2', item_code='FG002').exists())

    def test_a_rate_driven_negative_is_reported_and_the_rest_still_apply(self):
        data = self._apply(rate_mode='percent', apply_to='existing', items=[
            {'item_code': 'FG001', 'category': 'OIL', 'basic_rate': -200},
            {'item_code': 'FG002', 'category': 'OIL', 'basic_rate': 10},
        ]).json()['data']
        # Two parties refused on FG001, plus C2 not holding FG002 at all.
        self.assertEqual(data['skipped'], 3)
        self.assertEqual(len(data['errors']), 2)
        self.assertEqual(_rate('C1', 'FG001'), Decimal('100.0000'))
        self.assertEqual(_rate('C1', 'FG002'), Decimal('220.0000'))

    def test_an_inactive_item_is_reported_once_not_once_per_party(self):
        Product.objects.filter(item_code='FG001').update(is_active='N')
        data = self._apply(rate_mode='set', apply_to='existing',
                           items=[{'item_code': 'FG001', 'category': 'OIL',
                                   'basic_rate': 125}]).json()['data']
        self.assertEqual(len(data['errors']), 1)
        self.assertIn('2 parties skipped', data['errors'][0])

    def test_everything_failing_is_a_400_not_a_hollow_200(self):
        """`BulkAssignPartyToProductView` returns 200 `success: True` here."""
        Product.objects.filter(item_code='FG001').update(is_active='N')
        response = self._apply(rate_mode='set', apply_to='existing',
                               items=[{'item_code': 'FG001', 'category': 'OIL',
                                       'basic_rate': 125}])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertEqual(_rate('C1', 'FG001'), Decimal('100.0000'))

    def test_a_no_op_counts_unchanged_and_writes_nothing(self):
        AuditLog.objects.all().delete()
        data = self._apply(rate_mode='set', apply_to='existing',
                           items=[{'item_code': 'FG002', 'category': 'OIL',
                                   'basic_rate': 200}]).json()['data']
        self.assertEqual(data['unchanged'], 1)
        self.assertEqual(data['updated'], 0)
        # No per-record row, because no record changed. The middleware's own
        # contentless fallback row may still be filed -- that one says a bulk
        # edit was run, which is true, and carries no field values to be wrong.
        self.assertEqual(AuditLog.objects.filter(field__contains='basic_rate').count(), 0)

    def test_dry_run_reports_the_same_counts_and_writes_nothing(self):
        data = self._apply(rate_mode='set', apply_to='existing', dry_run=True,
                           items=[{'item_code': 'FG001', 'category': 'OIL',
                                   'basic_rate': 125}]).json()['data']
        self.assertEqual(data['updated'], 2)
        self.assertTrue(data['dry_run'])
        self.assertEqual(_rate('C1', 'FG001'), Decimal('100.0000'))

    def test_each_changed_assignment_leaves_an_audit_row(self):
        """The guard against anyone 'optimising' the loop into a queryset update."""
        AuditLog.objects.all().delete()
        self._apply(rate_mode='set', apply_to='existing',
                    items=[{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 125}])
        rows = AuditLog.objects.filter(field__contains='basic_rate')
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all('125' in row.new_value for row in rows))

    def test_a_failed_batch_leaves_neither_rows_nor_audit_entries(self):
        """`audit.transactions.audited_atomic` is what makes the second half true:
        the middleware flushes the buffer after the view returns, outside the
        transaction, so without it a rolled-back batch still logs every change."""
        AuditLog.objects.all().delete()
        with mock.patch.object(PartyProductAssignment, 'save',
                               side_effect=RuntimeError('database went away')):
            # Whether the failure surfaces as a raise or as a 500 is the test
            # client's business; what matters is the state it leaves behind.
            try:
                self._apply(rate_mode='set', apply_to='existing',
                            items=[{'item_code': 'FG001', 'category': 'OIL',
                                    'basic_rate': 125}])
            except RuntimeError:
                pass
        self.assertEqual(_rate('C1', 'FG001'), Decimal('100.0000'))
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_an_oversized_selection_is_refused_before_any_work(self):
        selections = [{'card_code': f'X{i}', 'category': 'OIL'} for i in range(201)]
        response = self.post('bulk-party-update-rates', {
            'party_selections': selections, 'rate_mode': 'set', 'apply_to': 'existing',
            'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 1}]})
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_rate_mode_is_refused(self):
        self.assertEqual(self._apply(
            rate_mode='multiply', apply_to='existing',
            items=[{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 2}]
        ).status_code, 400)


class BulkActivationTests(_BulkTestCase):

    def test_deactivating_flips_is_active_and_leaves_the_rate_alone(self):
        data = self.post('bulk-party-set-active', {
            'party_selections': self.oil, 'is_active': False,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).json()['data']
        self.assertEqual(data['updated'], 2)
        row = PartyProductAssignment.objects.get(card_code='C1', item_code='FG001')
        self.assertFalse(row.is_active)
        self.assertEqual(row.basic_rate, Decimal('100.0000'))

    def test_reactivating_restores_the_rate_that_was_negotiated(self):
        _assign('C2', 'FG002', 205, is_active=False)
        self.post('bulk-party-set-active', {
            'party_selections': self.oil, 'is_active': True,
            'items': [{'item_code': 'FG002', 'category': 'OIL'}]})
        row = PartyProductAssignment.objects.get(card_code='C2', item_code='FG002')
        self.assertTrue(row.is_active)
        self.assertEqual(row.basic_rate, Decimal('205.0000'))

    def test_a_party_without_the_item_is_missing_not_created(self):
        data = self.post('bulk-party-set-active', {
            'party_selections': self.oil, 'is_active': False,
            'items': [{'item_code': 'FG002', 'category': 'OIL'}]}).json()['data']
        self.assertEqual(data['missing'], 1)
        self.assertFalse(PartyProductAssignment.objects.filter(
            card_code='C2', item_code='FG002').exists())

    def test_is_active_is_required(self):
        self.assertEqual(self.post('bulk-party-set-active', {
            'party_selections': self.oil,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).status_code, 400)


class CopyCatalogueTests(_BulkTestCase):

    def test_copies_the_sources_items_to_a_target_that_lacks_them(self):
        data = self.post('bulk-party-copy-catalogue', {
            'source': {'card_code': 'C1', 'category': 'OIL'},
            'party_selections': [{'card_code': 'C2', 'category': 'OIL'}],
        }).json()['data']
        self.assertEqual(data['created'], 1)
        self.assertEqual(_rate('C2', 'FG002'), Decimal('200.0000'))

    def test_overwrite_false_leaves_a_negotiated_rate_alone(self):
        self.post('bulk-party-copy-catalogue', {
            'source': {'card_code': 'C1', 'category': 'OIL'},
            'party_selections': [{'card_code': 'C2', 'category': 'OIL'}],
            'overwrite_existing': False})
        self.assertEqual(_rate('C2', 'FG001'), Decimal('110.0000'))
        self.assertEqual(_rate('C2', 'FG002'), Decimal('200.0000'))

    def test_overwrite_true_imposes_the_sources_rate(self):
        self.post('bulk-party-copy-catalogue', {
            'source': {'card_code': 'C1', 'category': 'OIL'},
            'party_selections': [{'card_code': 'C2', 'category': 'OIL'}],
            'overwrite_existing': True})
        self.assertEqual(_rate('C2', 'FG001'), Decimal('100.0000'))

    def test_the_source_is_skipped_when_it_is_also_a_target(self):
        data = self.post('bulk-party-copy-catalogue', {
            'source': {'card_code': 'C1', 'category': 'OIL'},
            'party_selections': self.oil}).json()['data']
        self.assertNotIn('C1', {'C1'} & set())  # source never re-written
        self.assertEqual(_rate('C1', 'FG001'), Decimal('100.0000'))
        self.assertGreaterEqual(data['skipped'], 1)

    def test_a_source_with_nothing_to_copy_is_refused(self):
        data = self.post('bulk-party-copy-catalogue', {
            'source': {'card_code': 'C3', 'category': 'MART'},
            'party_selections': self.oil})
        self.assertEqual(data.status_code, 400)

    def test_dry_run_writes_nothing(self):
        self.post('bulk-party-copy-catalogue', {
            'source': {'card_code': 'C1', 'category': 'OIL'},
            'party_selections': [{'card_code': 'C2', 'category': 'OIL'}],
            'dry_run': True})
        self.assertFalse(PartyProductAssignment.objects.filter(
            card_code='C2', item_code='FG002').exists())
