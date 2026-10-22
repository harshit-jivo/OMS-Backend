"""An Item bill's GRPO user fields at the Accounts desk (2026-10-22). No SAP, no database."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.services import ap_invoice, grpo_fields


def _f(field, value, editable, kind='text'):
    return {'field': field, 'label': field[2:], 'kind': kind, 'value': value, 'editable': editable}


FOUND = {
    'header': [_f('U_BilltyNumber', '5886', True), _f('U_VehicleNoM', 'HR69F1439', True),
               _f('U_BiltyDate', '', True, 'date'), _f('U_UNE_ACTH', 'FREIGHT', False)],
    'lines': [{'line_num': 0, 'item_code': 'OIL', 'description': 'Oil',
               'fields': [_f('U_UNE_LTS', '1600', True, 'number'), _f('U_CardCode', 'CUSTA001159', False)]}],
}


class ReadingTheGrpo(SimpleTestCase):
    def test_every_filled_field_and_the_editable_ones_each_marked(self):
        columns = [{'table': t, 'name': n, 'type': ty} for t in ('OPDN', 'OPCH')
                   for n, ty in (('U_BilltyNumber', 'NVARCHAR'), ('U_BiltyDate', 'TIMESTAMP'), ('U_POMade', 'NVARCHAR'),
                                 ('U_Empty', 'NVARCHAR'))]
        columns += [{'table': t, 'name': n, 'type': ty} for t in ('PDN1', 'PCH1')
                    for n, ty in (('U_UNE_LTS', 'DECIMAL'), ('U_CardCode', 'NVARCHAR'))]
        columns.append({'table': 'OPDN', 'name': 'U_GrpoOnly', 'type': 'NVARCHAR'})
        labels = [{'table': 'OPDN', 'alias': 'BilltyNumber', 'label': 'Billty Number', 'type': 'A'},
                  {'table': 'PDN1', 'alias': 'UNE_LTS', 'label': 'Litre', 'type': 'B'}]
        head = [{'U_BilltyNumber': '5886', 'U_BiltyDate': None, 'U_POMade': 'Y', 'U_Empty': '', 'U_GrpoOnly': 'x'}]
        lines = [{'line_num': 0, 'item_code': 'OIL', 'description': 'Oil', 'U_UNE_LTS': Decimal('1600.000000'),
                  'U_CardCode': 'CUSTA001159'}]
        with mock.patch.object(grpo_fields, '_schema', return_value='S'), \
                mock.patch.object(grpo_fields, '_run', side_effect=[columns, labels, head, lines]):
            found = grpo_fields.read('OIL', 7)
        header = {f['field']: f for f in found['header']}
        self.assertEqual(header['U_BilltyNumber']['label'], 'Billty Number')
        self.assertTrue(header['U_BilltyNumber']['editable'])
        self.assertEqual(header['U_BiltyDate']['value'], '')  # editable: shown though empty
        self.assertFalse(header['U_POMade']['editable'])
        self.assertNotIn('U_Empty', header)  # read only and empty: not shown
        self.assertNotIn('U_GrpoOnly', header)  # the bill has no such field
        line = {f['field']: f for f in found['lines'][0]['fields']}
        self.assertEqual(line['U_UNE_LTS'], {'field': 'U_UNE_LTS', 'label': 'Litre', 'kind': 'number',
                                             'value': '1600', 'editable': True})
        self.assertFalse(line['U_CardCode']['editable'])


class TheDesksChanges(SimpleTestCase):
    def test_only_editable_fields_each_checked_by_kind(self):
        problems = []
        out = grpo_fields.clean({'header': {'U_VehicleNoM': ' HR55AB1234 ', 'U_BiltyDate': '2026-10-21',
                                            'U_UNE_ACTH': 'X'},
                                 'lines': {'0': {'U_UNE_LTS': '1000.50', 'U_CardCode': 'Y'},
                                           '1': {'U_UNE_LTS': 'abc'}}}, problems)
        self.assertEqual(out, {'header': {'U_VehicleNoM': 'HR55AB1234', 'U_BiltyDate': '2026-10-21'},
                               'lines': {'0': {'U_UNE_LTS': '1000.5'}}})
        self.assertEqual(len(problems), 3)  # U_UNE_ACTH and U_CardCode are not edited; 'abc' is no number

    def test_nothing_changed_is_none(self):
        self.assertIsNone(grpo_fields.clean({}, []))
        self.assertIsNone(grpo_fields.clean(None, []))


class TheInvoiceGetsThem(SimpleTestCase):
    def test_the_grpos_values_with_the_desks_changes_over_the_editable_ones(self):
        header, lines = grpo_fields.for_invoice(FOUND, {'header': {'U_VehicleNoM': 'HR55AB1234', 'U_UNE_ACTH': 'NO'},
                                                        'lines': {'0': {'U_UNE_LTS': '1000'}}})
        self.assertEqual(header, {'U_BilltyNumber': '5886', 'U_VehicleNoM': 'HR55AB1234', 'U_UNE_ACTH': 'FREIGHT'})
        self.assertEqual(lines, {0: {'U_UNE_LTS': 1000.0, 'U_CardCode': 'CUSTA001159'}})

    def test_the_payload_carries_them_on_the_header_and_each_copied_line(self):
        advance = SimpleNamespace(
            partner_code='VENDA1', expense_bill_date=date(2026, 10, 20), expense_bill_number='inv-1',
            expense_due_date=None, expense_tds_code='', effect_month='10-2026',
            expense_lines=SimpleNamespace(all=lambda: []), request_no='AP-1', partner_name='X', budget_code='',
            sub_budget_code='', remarks='')
        grpo = {'doc_entry': 7, 'category': 'ITEM', 'lines': [{'line_num': 0}]}
        payload = ap_invoice.build_payload(advance, posting_date=datetime(2026, 10, 22).date(), series=1, bpl_id=3,
                                           memo='m', grpo=grpo,
                                           user_fields=grpo_fields.for_invoice(FOUND, None))
        self.assertEqual(payload['U_BilltyNumber'], '5886')
        self.assertEqual(payload['DocumentLines'][0]['U_UNE_LTS'], 1600.0)
        self.assertEqual(payload['DocumentLines'][0]['BaseEntry'], 7)
