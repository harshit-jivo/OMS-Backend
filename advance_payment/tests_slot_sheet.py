"""The payment slot sheet: balances before and after, branch, the workbook. No SAP, no database."""
import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.test import SimpleTestCase

from advance_payment.services import slot_sheet


def _lines(*rows):
    return [(trans, day, Decimal(str(net))) for trans, day, net in rows]


class TheBalances(SimpleTestCase):
    def test_opening_is_just_before_the_payment_and_the_balance_just_after(self):
        lines = _lines((10, date(2026, 9, 1), -418505), (20, date(2026, 9, 28), 418505), (30, date(2026, 10, 1), -5))
        opening, debit_days = slot_sheet.standing_before(lines, 20, date(2026, 9, 28))
        self.assertEqual(opening, Decimal('-418505.00'))
        self.assertIsNone(debit_days)  # payable to them: not in debit
        self.assertEqual(slot_sheet.balance_through(lines, 20), Decimal('0.00'))  # later lines do not count

    def test_debit_since_counts_from_when_it_last_went_into_debit(self):
        lines = _lines((1, date(2026, 7, 1), -100), (2, date(2026, 8, 17), 7608), (3, date(2026, 9, 28), 633700))
        opening, debit_days = slot_sheet.standing_before(lines, 3, date(2026, 9, 28))
        self.assertEqual(opening, Decimal('7508.00'))
        self.assertEqual(debit_days, 42)
        self.assertEqual(slot_sheet.balance_through(lines, 3), Decimal('641208.00'))

    def test_a_balance_back_to_nil_starts_afresh(self):
        lines = _lines((1, date(2026, 1, 1), 500), (2, date(2026, 2, 1), -500), (3, date(2026, 9, 1), 200))
        self.assertEqual(slot_sheet.standing_before(lines, 4, date(2026, 9, 11)), (Decimal('200.00'), 10))


class TheCells(SimpleTestCase):
    def test_branch_is_hr_for_the_plants_heads_and_dl_for_the_rest(self):
        for head in ('Factory', 'FACTORY', 'Del Bkhp', 'FACT_COM'):
            self.assertEqual(slot_sheet.branch_of(head), 'HR')
        for head in ('ECOM', 'BackOff', 'Sales', 'Del Mayp', ''):
            self.assertEqual(slot_sheet.branch_of(head), 'DL')

    def test_the_workbook_has_a_block_per_day_and_company(self):
        cells = {h: '' for h in slot_sheet.HEADERS}
        row = lambda day, company, no: {  # noqa: E731
            'day': day, 'company': company, 'company_name': f'{company} LTD',
            'cells': {**cells, 'VOUCHER NO': no, 'OPENING': Decimal('-418505'), 'PAYMENT': Decimal('418505'),
                      'BALANCE': Decimal('0')}}
        data = slot_sheet.workbook([row(date(2026, 9, 28), 'OIL', 'AP-1'), row(date(2026, 9, 28), 'OIL', 'AP-2'),
                                    row(date(2026, 9, 29), 'MART', 'AP-3')])
        sheet = openpyxl.load_workbook(io.BytesIO(data)).active
        self.assertEqual([sheet['A1'].value, sheet['B1'].value, sheet['C1'].value, sheet['F1'].value],
                         ['SLOT', 1, 'DATE - 28/09/2026', 'OIL LTD'])
        self.assertEqual(sheet['A2'].value, 'VOUCHER NO')
        self.assertEqual([sheet['A3'].value, sheet['B3'].value, sheet['A4'].value, sheet['B4'].value],
                         ['AP-1', 1, 'AP-2', 2])
        self.assertEqual(sheet['F3'].value, -418505)
        self.assertEqual(sheet['C6'].value, 'DATE - 29/09/2026')
        self.assertEqual(sheet['A8'].value, 'AP-3')
