"""The Budget board: every posting in one head by its budget, and the costs across them."""
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.services import budget_board as board
from advance_payment.services.expense_dashboard import DashboardInvalid


def row(amount, *, budget='', account='', company='OIL', day='2026-08-05', salary=False, sub=''):
    return {'amount': amount, 'budget': budget, 'sub_budget': sub, 'account_name': account, 'company': company,
            'doc_date': day, 'is_salary': salary, 'remarks': ''}


class EveryPostingHasOneHead(SimpleTestCase):
    def test_by_its_budget_salaries_and_power_included(self):
        cases = [
            (row(1, budget='Sales', account='SALARY EXPENSES', salary=True), 'SALES'),
            (row(1, budget='ECOM', account='BUSINESS PROMOTION', company='MART'), 'SALES'),
            (row(1, budget='Factory', account='ELECTRICITY EXPENSES'), 'FACTORY'),
            (row(1, budget='BackOff', account='RENT'), 'BACKOFFICE'),
            (row(1, budget='Sal CF', account='SALARY EXPENSE', salary=True), 'BACKOFFICE'),
            (row(1, budget='Del Bkhp', account='LOADING CHARGES'), 'DELIVERY'),
            (row(1, budget='SUPPLY-C', account='WAREHOUSE', company='MART'), 'DELIVERY'),
            (row(1, budget='Interest', account='INTEREST ON BANK LOAN'), 'INTEREST'),
            (row(1, budget='', account='SHORT AND EXCESS'), 'OTHER'),
            # Mart's salaries count under Sales (its card), wherever booked.
            (row(1, budget='HR/ADMIN', account='SALARY EXPENSES', company='MART', salary=True), 'SALES'),
        ]
        for r, head in cases:
            with self.subTest(r['budget'] or r['account_name']):
                self.assertEqual(board.department_of(r), head)


class TheCostsAcrossThem(SimpleTestCase):
    def test_by_what_the_money_was_for(self):
        cases = [
            (row(1, budget='Sales', account='SALARY EXPENSES', salary=True), 'SALARY'),
            (row(1, budget='Factory', account='ELECTRICITY EXPENSES'), 'ELECTRICITY'),
            (row(1, budget='BackOff', account='RENT'), 'RENT'),
            (row(1, budget='Sales', account='FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP'), 'FREIGHT'),
            (row(1, budget='BackOff', account='BANK CHARGES'), 'FINANCE'),
            (row(1, budget='ECOM', account='BUSINESS PROMOTION'), None),
            (row(1, budget='BackOff', account='TRANSPARENT FILM'), None),
        ]
        for r, cost in cases:
            with self.subTest(r['account_name']):
                self.assertEqual(board.cost_of(r), cost)

    def test_mart_counts_only_through_its_salary_and_freight(self):
        self.assertTrue(board.counted(row(1, budget='HR/ADMIN', company='MART', salary=True)))
        self.assertTrue(board.counted(row(1, budget='SUPPLY-C', account='FREIGHT INWARD', company='MART')))
        for budget, account in (('ECOM', 'BUSINESS PROMOTION'), ('SUPPLY-C', 'WAREHOUSE RENT'),
                                ('HR/ADMIN', 'RENT'), ('HR/ADMIN', 'INTEREST ON BANK LOAN'), ('Sales', 'ADS')):
            with self.subTest(account):
                self.assertFalse(board.counted(row(1, budget=budget, account=account, company='MART')))
                self.assertTrue(board.counted(row(1, budget=budget, account=account, company='OIL')))


class WhatTheBoardLeavesOut(SimpleTestCase):
    def test_new_product_development_and_factory_civil_works(self):
        for r in (row(1, budget='NPD1', sub='Legal', account='LEGAL AND PROFESSIONAL'), row(1, budget='NPD2'),
                  row(1, budget='Factory', sub='CIVIL', account='REPAIR AND MAINTENANCE OFFICE & BUILDING')):
            with self.subTest(r['budget'], sub=r['sub_budget']):
                self.assertFalse(board.counted(r))
        # The rest of the factory stays.
        self.assertTrue(board.counted(row(1, budget='Factory', account='ELECTRICITY EXPENSES')))
        self.assertTrue(board.counted(row(1, budget='Factory', sub='PLANT')))


class TheBoard(SimpleTestCase):
    AUGUST = [row(100, budget='Sales', company='OIL'), row(300, budget='Sales', company='OIL', sub='E-COM', account='ADS'),
              row(250, budget='Sales', account='SALARY EXPENSES', salary=True), row(50, budget='Factory', company='BEVERAGES'),
              row(40, budget='HR/ADMIN', account='RENT', company='MART'),
              row(30, budget='Sales', account='SALARY EXPENSES', company='MART', salary=True),
              row(900, budget='ECOM', account='BUSINESS PROMOTION', company='MART')]
    OCTOBER = [row(500, budget='Sales', company='OIL', sub='E-COM', day='2026-10-03', account='ADS'),
               row(20, budget='Factory', company='BEVERAGES', day='2026-10-10'),
               row(700, budget='ECOM', account='BUSINESS PROMOTION', company='MART', day='2026-10-04')]

    def build(self):
        def rows(start, end, *, closed):
            return self.AUGUST if start.month == 8 else self.OCTOBER
        with mock.patch.object(board, '_rows', side_effect=rows):
            return board.build(today=date(2026, 10, 14))

    def test_the_heads_add_up_to_the_whole(self):
        data = self.build()
        heads = {h['key']: h for h in data['heads']}
        self.assertEqual([h['key'] for h in data['heads']],
                         ['SALES', 'FACTORY', 'BACKOFFICE', 'DELIVERY', 'INTEREST', 'OTHER'])
        # Sales: its spend and its salaries — Mart's salary in, Mart's promotion (900, 700) out.
        self.assertEqual((heads['SALES']['base'], heads['SALES']['current'], heads['SALES']['ratio']), (680, 500, 0.7353))
        self.assertEqual((data['total']['base'], data['total']['current']), (730, 520))
        self.assertEqual(sum(h['base'] for h in data['heads']), data['total']['base'])
        self.assertEqual((data['current']['elapsed'], data['current']['days']), (14, 31))

    def test_the_costs_cut_across_the_heads(self):
        costs = {c['key']: c for c in self.build()['costs']}
        self.assertEqual(list(costs), ['SALARY', 'FREIGHT', 'FINANCE', 'RENT', 'ELECTRICITY'])
        self.assertEqual(costs['SALARY']['base'], 280)
        self.assertEqual(costs['RENT']['base'], 0)  # Mart's rent is not counted
        self.assertIsNone(costs['RENT']['ratio'])

    def test_sales_names_its_owner_in_each_company(self):
        sales = next(h for h in self.build()['heads'] if h['key'] == 'SALES')
        self.assertEqual([(o['label'], o['name']) for o in sales['owners']],
                         [('Oil', 'Raju'), ('Mart', 'Prabhjot'), ('Beverages', 'Shunty')])
        self.assertEqual([(c['company'], c['base'], c['current']) for c in sales['by_company']],
                         [('OIL', 650, 500), ('MART', 30, 0)])
        # Each company's own breakdown, for its card: Oil's E-COM spend, none of Mart's salary.
        oil = sales['by_company'][0]
        self.assertEqual((oil['entries'], oil['tree'][0]['name'], oil['tree'][0]['current']), (1, 'Sales › E-COM', 500))
        mart = sales['by_company'][1]
        self.assertEqual([(t['name'], t['base']) for t in mart['tree']], [('Sales', 30)])

    def test_opens_level_by_level_budget_then_account_then_postings(self):
        sales = next(h for h in self.build()['heads'] if h['key'] == 'SALES')
        first = sales['tree'][0]
        self.assertEqual((first['name'], first['base'], first['current'], first['entries']), ('Sales › E-COM', 300, 500, 1))
        account = first['accounts'][0]
        self.assertEqual((account['name'], account['base'], account['current']), ('ADS', 300, 500))
        self.assertEqual([(p['company'], p['amount'], p['date']) for p in account['postings']], [('OIL', 500, '2026-10-03')])

    def test_a_month_not_yet_begun_is_refused(self):
        with self.assertRaises(DashboardInvalid):
            board.build('2026-12', today=date(2026, 10, 14))
