"""The Budget board: every posting under one main head, this month against August."""
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.services import budget_board as board
from advance_payment.services.expense_dashboard import DashboardInvalid


def row(amount, *, budget='', account='', company='OIL', day='2026-08-05', salary=False, sub=''):
    return {'amount': amount, 'budget': budget, 'sub_budget': sub, 'account_name': account, 'company': company,
            'doc_date': day, 'is_salary': salary, 'remarks': ''}


class EveryPostingHasOneHead(SimpleTestCase):
    def test_accounts_first_then_budget_heads(self):
        cases = [
            (row(1, budget='Sales', account='SALARY EXPENSES', salary=True), 'SALARY'),
            (row(1, budget='Sal CF', account='PF CONTRIBUTION'), 'SALARY'),
            (row(1, budget='Factory', account='ELECTRICITY EXPENSES'), 'ELECTRICITY'),
            (row(1, budget='BackOff', account='RENT'), 'RENT'),
            (row(1, budget='Sales', account='FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP'), 'FREIGHT'),
            (row(1, budget='Del Bkhp', account='LOADING CHARGES'), 'FREIGHT'),
            (row(1, budget='Interest', account='INTEREST ON BANK LOAN'), 'FINANCE'),
            (row(1, budget='BackOff', account='BANK CHARGES'), 'FINANCE'),
            (row(1, budget='ECOM', account='BUSINESS PROMOTION'), 'SALES'),
            (row(1, budget='FACT_COM', account='REPAIR AND MAINTENANCE'), 'FACTORY'),
            (row(1, budget='HR/ADMIN', account='PRINTING AND STATIONERY'), 'BACKOFFICE'),
            (row(1, budget='', account='SHORT AND EXCESS'), 'OTHER'),
        ]
        for r, head in cases:
            with self.subTest(r['account_name']):
                self.assertEqual(board.head_of(r), head)

    def test_a_parent_account_is_not_rent(self):
        self.assertEqual(board.head_of(row(1, budget='BackOff', account='TRANSPARENT FILM')), 'BACKOFFICE')


class TheBoard(SimpleTestCase):
    AUGUST = [row(100, budget='Sales', company='OIL'), row(300, budget='Sales', company='MART', sub='E-COM', account='ADS'),
              row(250, budget='Sal CF', salary=True), row(50, budget='Factory', company='BEVERAGES')]
    OCTOBER = [row(500, budget='Sales', company='MART', sub='E-COM', day='2026-10-03', account='ADS'),
               row(20, budget='Factory', company='BEVERAGES', day='2026-10-10')]

    def build(self):
        def rows(start, end, *, closed):
            return self.AUGUST if start.month == 8 else self.OCTOBER
        with mock.patch.object(board, '_rows', side_effect=rows):
            return board.build(today=date(2026, 10, 14))

    def test_ranks_the_heads_by_august_and_measures_this_month_against_it(self):
        data = self.build()
        heads = {h['key']: h for h in data['heads']}
        self.assertEqual([h['key'] for h in data['heads']][:3], ['SALES', 'SALARY', 'FACTORY'])
        self.assertEqual((heads['SALES']['base'], heads['SALES']['current'], heads['SALES']['ratio']), (400, 500, 1.25))
        self.assertEqual(heads['SALARY']['ratio'], 0)
        self.assertIsNone(heads['RENT']['ratio'])  # nothing in August to measure against
        self.assertEqual(data['heads'][-1]['key'], 'OTHER')
        self.assertEqual((data['total']['base'], data['total']['current']), (700, 520))
        self.assertEqual((data['current']['elapsed'], data['current']['days']), (14, 31))

    def test_sales_names_its_owner_in_each_company(self):
        sales = next(h for h in self.build()['heads'] if h['key'] == 'SALES')
        self.assertEqual([(o['label'], o['name']) for o in sales['owners']],
                         [('Oil', 'Raju'), ('Mart', 'Prabhjot'), ('Beverages', 'Shunty')])
        self.assertEqual([(c['company'], c['base'], c['current']) for c in sales['by_company']],
                         [('OIL', 100, 0), ('MART', 300, 500)])

    def test_opens_level_by_level_budget_then_account_then_postings(self):
        sales = next(h for h in self.build()['heads'] if h['key'] == 'SALES')
        first = sales['tree'][0]
        self.assertEqual((first['name'], first['base'], first['current'], first['entries']), ('Sales › E-COM', 300, 500, 1))
        account = first['accounts'][0]
        self.assertEqual((account['name'], account['base'], account['current']), ('ADS', 300, 500))
        self.assertEqual([(p['company'], p['amount'], p['date']) for p in account['postings']], [('MART', 500, '2026-10-03')])
        # Sales with no sub budget, August only: there, with nothing this month.
        self.assertEqual((sales['tree'][1]['name'], sales['tree'][1]['current']), ('Sales', 0))

    def test_a_month_not_yet_begun_is_refused(self):
        with self.assertRaises(DashboardInvalid):
            board.build('2026-12', today=date(2026, 10, 14))
