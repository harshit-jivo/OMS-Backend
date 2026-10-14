"""The Expense dashboard: SAP's actual expenses, compared and broken down."""
from datetime import date
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from advance_payment.services import expense_dashboard as dashboard


def row(day, amount, *, budget='Factory', account='ELECTRICITY', kind='Direct', branch='OIL',
        category='Operations & Factory', salary=False):
    return {'doc_date': day, 'amount': amount, 'budget': budget, 'account_name': account,
            'expense_type': kind, 'branch': branch, 'category': category, 'is_salary': salary,
            'sub_budget': '', 'variety': '', 'doc_num': 1, 'trans_id': 1, 'remarks': ''}


SEPT = [row('2026-09-02', 1000), row('2026-09-05', 500, budget='Sales', account='RENT', kind='Indirect',
                                     branch='BEVERAGES', category='Admin & General'),
        row('2026-09-09', 9000, budget='Sal CF', account='SALARY EXPENSES', category='Salaries & HR', salary=True)]
AUG = [row('2026-08-10', 750)]


def sap(start, end):
    """Rows by the asked range, as the procedure answers."""
    every = SEPT + AUG
    return [r for r in every if start <= r['doc_date'] <= end]


class ExpenseDashboard(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def build(self, **kw):
        with mock.patch('cpanel.dashboard.expenses_service.fetch_expense_rows', side_effect=sap), \
                mock.patch.object(dashboard, '_oms_requests', return_value={}):
            return dashboard.build('2026-09-01', '2026-09-30', today=date(2026, 10, 13), **kw)

    def test_totals_leave_salaries_out_and_compare_with_the_period_before(self):
        k = self.build()['kpis']
        self.assertEqual((k['total'], k['direct'], k['indirect'], k['entries']), (1500, 1000, 500, 2))
        self.assertEqual((k['salary'], k['previous'], k['change']), (9000, 750, 100.0))
        self.assertEqual(k['top_head']['name'], 'Factory')

    def test_filters_narrow_every_figure(self):
        self.assertEqual(self.build(branch='BEVERAGES')['kpis']['total'], 500)
        self.assertEqual(self.build(kind='direct')['kpis']['total'], 1000)
        self.assertEqual(self.build(include_salary=True)['kpis']['total'], 10500)

    def test_breakdowns_and_the_twelve_month_trend(self):
        data = self.build()
        self.assertEqual([h['name'] for h in data['heads']], ['Factory', 'Sales'])
        self.assertEqual(data['heads'][0]['previous'], 750)
        self.assertEqual(len(data['trend']), 12)
        self.assertEqual(data['trend'][-1], {'month': '2026-09', 'label': 'Sep 26',
                                             'direct': 1000, 'indirect': 500, 'total': 1500})
        self.assertEqual(data['trend'][-2]['total'], 750)
        self.assertEqual({c['name'] for c in data['categories']}, {'Operations & Factory', 'Admin & General'})

    def test_sap_down_is_an_error_not_zero_spend(self):
        with mock.patch('cpanel.dashboard.expenses_service.fetch_expense_rows', side_effect=RuntimeError('timeout')):
            with self.assertRaises(dashboard.SapUnavailable):
                dashboard.build('2026-09-01', '2026-09-30')

    def test_refuses_a_backwards_or_too_long_range(self):
        with self.assertRaises(dashboard.DashboardInvalid):
            dashboard.build('2026-09-30', '2026-09-01')
        with self.assertRaises(dashboard.DashboardInvalid):
            dashboard.build('2025-01-01', '2026-09-01')
