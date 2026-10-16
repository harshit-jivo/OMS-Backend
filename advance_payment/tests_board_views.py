"""Each owner's own board (user, 2026-10-16): an owner signed in sees only
their head (or their company's part of it); an administrator sees the whole
board, and any owner's board as the owner sees it."""
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from advance_payment.services import budget_board as board
from advance_payment.tests_budget_board import TheBoard
from workflow.tests.factories import make_user

KEY = 'Expense_Dashboard'


class TheOwnersAndTheirParts(TheBoard):
    def test_every_head_names_its_owner(self):
        data = self.build()
        owners = {h['key']: [o['name'] for o in h['owners']] for h in data['heads'] + data['costs']}
        self.assertEqual(owners['FACTORY'], ['Gagan Vg', 'Arvinder Vg'])
        self.assertEqual(owners['BACKOFFICE'], ['Nirmal Didi'])
        self.assertEqual(owners['DELIVERY'], ['Sandeep Vg'])
        self.assertEqual(owners['INTEREST'], ['Avtar Vg'])
        self.assertEqual(owners['FINANCE'], ['Avtar Vg'])

    def test_a_company_owner_sees_their_company_part_alone(self):
        oil = board.scoped(self.build(), board.view_by_key('SALES_OIL'))
        (sales,) = oil['heads']
        self.assertEqual((sales['key'], sales['base'], sales['current']), ('SALES', 650, 500))
        self.assertEqual([c['company'] for c in sales['by_company']], ['OIL'])
        self.assertEqual([o['name'] for o in sales['owners']], ['Raju Vg'])
        self.assertEqual(sales['tree'][0]['name'], 'Sales › E-COM')
        self.assertEqual((oil['total']['base'], oil['total']['current']), (650, 500))
        self.assertEqual(oil['costs'], [])
        self.assertEqual(oil['view'], {'key': 'SALES_OIL', 'label': 'Sales · Oil', 'owner': 'Raju Vg'})

    def test_a_whole_head_owner_and_avtar_with_the_finance_cost(self):
        avtar = board.scoped(self.build(), board.view_by_key('FINANCE'))
        self.assertEqual([h['key'] for h in avtar['heads']], ['INTEREST'])
        self.assertEqual([c['key'] for c in avtar['costs']], ['FINANCE'])


@override_settings(ADVANCE_PAYMENT_BOARD_VIEW_USERS={'FREIGHT': ['sandeep']})
class TheEndpoints(TestCase):
    def setUp(self):
        self.full = {'current': {}, 'baseline': {}, 'total': {'base': 1, 'current': 1, 'ratio': 1},
                     'heads': [{'key': 'SALES', 'name': 'Sales', 'owners': board.OWNERS['SALES'], 'base': 10,
                                'current': 5, 'ratio': 0.5, 'entries': 1, 'tree': [],
                                'by_company': [{'company': 'OIL', 'label': 'Oil', 'base': 10, 'current': 5,
                                                'entries': 1, 'tree': []}]}],
                     'costs': []}
        patcher = mock.patch.object(board, 'build', side_effect=lambda *a, **k: {**self.full})
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, user, path='/api/advance-payments/budget-board/', **params):
        client = APIClient()
        client.force_authenticate(user)
        return client.get(path, params)

    def test_an_owner_sees_their_own_head_and_cannot_switch(self):
        raju = make_user('Raju VG', extra_pages=[KEY])  # either spelling of his login
        data = self.get(raju).json()['data']
        self.assertEqual(data['view']['key'], 'SALES_OIL')
        self.assertFalse(data['can_switch'])
        self.assertEqual(data['views'], [])
        # Asking for another view is ignored: an owner only ever gets their own.
        self.assertEqual(self.get(raju, view='FACTORY_OIL').json()['data']['view']['key'], 'SALES_OIL')
        self.assertEqual(self.get(raju, '/api/advance-payments/budget-board/me/').json()['data'],
                         {'view': {'key': 'SALES_OIL', 'label': 'Sales · Oil', 'owner': 'Raju Vg'},
                          'can_switch': False})
        # …and none of the requests boards.
        self.assertEqual(self.get(raju, '/api/advance-payments/oms-dashboard/').status_code, 403)
        self.assertEqual(self.get(raju, '/api/advance-payments/expense-dashboard/').status_code, 403)

    def test_a_login_set_in_settings_takes_the_view(self):
        sandeep = make_user('sandeep', extra_pages=[KEY])
        self.assertEqual(self.get(sandeep, '/api/advance-payments/budget-board/me/').json()['data']['view']['key'],
                         'FREIGHT')

    def test_an_administrator_sees_everything_or_any_owners_board(self):
        admin = make_user('board-admin', extra_pages=[KEY])
        admin.is_staff = True
        admin.save()
        data = self.get(admin).json()['data']
        self.assertIsNone(data['view'])
        self.assertTrue(data['can_switch'])
        self.assertEqual(len(data['views']), len(board.VIEWS))
        as_raju = self.get(admin, view='SALES_OIL').json()['data']
        self.assertEqual((as_raju['view']['owner'], as_raju['total']['current']), ('Raju Vg', 5))
        self.assertEqual(self.get(admin, view='NOPE').status_code, 400)

    def test_anyone_else_with_the_dashboard_sees_the_whole_board(self):
        other = make_user('board-viewer', extra_pages=[KEY])
        data = self.get(other).json()['data']
        self.assertIsNone(data['view'])
        self.assertFalse(data['can_switch'])
