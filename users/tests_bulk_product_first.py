"""The product-first direction, and turning a product off for every party.

`tests_bulk_assignments.py` covers the party-first endpoints. This covers the
mirror: pick products, see which parties hold them — and act on every holder
without the client naming them.

That last part is not a convenience. One item is held by up to 522 parties on
the live database, past `MAX_SELECTIONS`, so a blanket turn-off CANNOT send a
party list; and a list built when the page loaded would miss a party assigned
between then and the click. The server resolves the holders inside the same
transaction that acts on them.

Run with::

    python manage.py test users.tests_bulk_product_first --settings=OMS.test_settings
"""
from decimal import Decimal

from users.models import PartyProductAssignment

from .tests_bulk_assignments import _BulkTestCase, _assign, _party, _product, _rate


class BulkProductPartiesTests(_BulkTestCase):
    """Which parties hold this item, and at what rate."""

    def test_lists_every_party_holding_the_item(self):
        response = self.post('bulk-product-parties',
                             {'items': [{'item_code': 'FG001', 'category': 'OIL'}]})
        self.assertEqual(response.status_code, 200)
        item = response.json()['data']['items'][0]
        self.assertEqual(item['party_count'], 2)
        self.assertEqual({party['card_code'] for party in item['parties']}, {'C1', 'C2'})
        self.assertEqual({party['basic_rate'] for party in item['parties']}, {100.0, 110.0})

    def test_inactive_assignments_are_listed_and_flagged(self):
        """Everywhere else they are filtered out. Here they ARE the subject:
        reactivating needs to see what was turned off, and a party missing
        because it was deactivated would read as never assigned."""
        _assign('C3', 'FG001', 90, is_active=False)
        item = self.post('bulk-product-parties', {
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).json()['data']['items'][0]
        self.assertEqual(item['party_count'], 3)
        self.assertEqual(item['active_count'], 2)
        self.assertEqual(item['inactive_count'], 1)
        turned_off = next(p for p in item['parties'] if p['card_code'] == 'C3')
        self.assertFalse(turned_off['is_active'])

    def test_the_spread_ignores_deactivated_rows(self):
        _assign('C3', 'FG001', 1, is_active=False)
        item = self.post('bulk-product-parties', {
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).json()['data']['items'][0]
        self.assertEqual(item['min_rate'], 100.0)
        self.assertEqual(item['distinct_rates'], 2)

    def test_common_rate_agrees_with_the_party_first_view(self):
        """Both tabs show a `common_rate` for the same product. One shared
        function, so a tie breaks to the lowest in both — two copies of that
        rule would eventually disagree about the same product."""
        by_product = self.post('bulk-product-parties', {
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).json()['data']['items'][0]
        by_party = next(
            row for row in self.post('bulk-party-products',
                                     {'party_selections': self.oil}).json()['data']['products']
            if row['item_code'] == 'FG001')
        self.assertEqual(by_product['common_rate'], by_party['common_rate'])
        self.assertEqual(by_product['common_rate'], 100.0)

    def test_items_is_required(self):
        self.assertEqual(self.post('bulk-product-parties', {'items': []}).status_code, 400)


class PartyScopeAllHoldersTests(_BulkTestCase):
    """Acting on every party holding an item, without the client naming them."""

    def test_turns_the_item_off_for_every_holder(self):
        data = self.post('bulk-party-set-active', {
            'party_scope': 'all_holders', 'is_active': False,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).json()['data']
        self.assertEqual(data['updated'], 2)
        self.assertFalse(PartyProductAssignment.objects.get(
            card_code='C1', item_code='FG001').is_active)
        self.assertFalse(PartyProductAssignment.objects.get(
            card_code='C2', item_code='FG001').is_active)

    def test_it_reaches_a_party_no_client_list_would_have_named(self):
        _party('C9')
        _assign('C9', 'FG001', 105)
        self.post('bulk-party-set-active', {
            'party_scope': 'all_holders', 'is_active': False,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]})
        self.assertFalse(PartyProductAssignment.objects.get(
            card_code='C9', item_code='FG001').is_active)

    def test_reactivating_finds_the_rows_a_deactivation_left(self):
        PartyProductAssignment.objects.filter(item_code='FG001').update(is_active=False)
        data = self.post('bulk-party-set-active', {
            'party_scope': 'all_holders', 'is_active': True,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).json()['data']
        self.assertEqual(data['updated'], 2)

    def test_it_ignores_a_party_selections_list_sent_alongside(self):
        """`party_scope` wins, so a client cannot send both and be surprised by
        which one was honoured."""
        self.post('bulk-party-set-active', {
            'party_scope': 'all_holders',
            'party_selections': [{'card_code': 'C1', 'category': 'OIL'}],
            'is_active': False,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]})
        self.assertFalse(PartyProductAssignment.objects.get(
            card_code='C2', item_code='FG001').is_active)

    def test_the_selection_cap_does_not_apply_to_a_list_we_resolved(self):
        """MAX_SELECTIONS bounds what a CLIENT may post. There is no client list
        here, and MAX_TARGET_ROWS still bounds the work."""
        for index in range(210):
            code = f'B{index}'
            _party(code)
            _assign(code, 'FG001', 100 + index)
        response = self.post('bulk-party-set-active', {
            'party_scope': 'all_holders', 'is_active': False,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['updated'], 212)

    def test_a_product_nobody_holds_is_refused(self):
        _product('FG900')
        self.assertEqual(self.post('bulk-party-set-active', {
            'party_scope': 'all_holders', 'is_active': False,
            'items': [{'item_code': 'FG900', 'category': 'OIL'}]}).status_code, 400)

    def test_an_unknown_scope_is_refused(self):
        self.assertEqual(self.post('bulk-party-set-active', {
            'party_scope': 'everyone', 'is_active': False,
            'items': [{'item_code': 'FG001', 'category': 'OIL'}]}).status_code, 400)

    def test_rates_can_be_revised_for_every_holder_too(self):
        self.post('bulk-party-update-rates', {
            'party_scope': 'all_holders', 'rate_mode': 'percent', 'apply_to': 'existing',
            'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 10}]})
        self.assertEqual(_rate('C1', 'FG001'), Decimal('110.0000'))
        self.assertEqual(_rate('C2', 'FG001'), Decimal('121.0000'))

    def test_all_holders_cannot_be_combined_with_apply_to_all(self):
        """One means the parties that HAVE it, the other means reach the ones
        that do not. A client asking for both has not decided what it wants."""
        self.assertEqual(self.post('bulk-party-update-rates', {
            'party_scope': 'all_holders', 'rate_mode': 'set', 'apply_to': 'all',
            'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 125}]
        }).status_code, 400)
