"""The payee's credit period (SAP payment terms) on every request the pages list."""
from unittest import mock

from django.test import SimpleTestCase, override_settings

from advance_payment import views
from advance_payment.services import sap as sap_service

NET30 = {'terms': 'NET-30', 'days': 30, 'set': True}
NET45 = {'terms': 'NET-45', 'days': 45, 'set': True}
UNSET = {'terms': 'ADVANCE/CASH/0 DAYS', 'days': 0, 'set': False}


def row(pk, company='OIL', request_type='VENDOR', partner='VENDA000001', **extra):
    return {'id': pk, 'company': company, 'request_type': request_type, 'partner_code': partner,
            'partner_not_in_sap': False, **extra}


def bills(*entries):
    return {'documents': [{'kind': 'BILL', 'sap_doc_entry': e} for e in entries]}


def run(rows, terms, dates=None):
    with mock.patch.object(sap_service, 'credit_terms', side_effect=lambda c, codes: terms[c]) as lookup, \
            mock.patch.object(sap_service, 'bill_dates', side_effect=lambda c, entries: (dates or {}).get(c, {})):
        return views._with_credit_terms(rows), lookup


@override_settings(ADVANCE_PAYMENT_CREDIT_TERMS=True)
class CreditTermsOnTheList(SimpleTestCase):
    def test_one_sap_query_per_company_for_the_whole_list(self):
        rows = [row(1), row(2, partner='VENDA000002'), row(3, company='MART')]
        out, lookup = run(rows, {'OIL': {'VENDA000001': NET30}, 'MART': {}})
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(sorted(lookup.call_args_list[0].args[1]), ['VENDA000001', 'VENDA000002'])
        self.assertEqual(out[0]['credit_terms'], {**NET30, 'credit_days': 30, 'assumed': False,
                                                  'due_date': None, 'posting_date': None})
        self.assertEqual([r['credit_terms'] for r in out[1:]], [None, None])

    def test_terms_not_set_in_sap_default_to_30_days_from_the_bill_posting_date(self):
        dates = {'OIL': {7: {'posting_date': '2026-09-10', 'due_date': '2026-09-10'},
                         8: {'posting_date': '2026-09-01', 'due_date': '2026-09-01'}}}
        (out,), _ = run([row(1, **bills(7, 8))], {'OIL': {'VENDA000001': UNSET}}, dates)
        terms = out['credit_terms']
        self.assertEqual((terms['credit_days'], terms['assumed']), (30, True))
        # The earliest bill: posted 1 Sep, so due 30 days later.
        self.assertEqual((terms['due_date'], terms['posting_date']), ('2026-10-01', '2026-09-01'))

    def test_terms_set_in_sap_use_the_bill_due_date_there(self):
        dates = {'OIL': {7: {'posting_date': '2026-09-01', 'due_date': '2026-10-16'}}}
        (out,), _ = run([row(1, **bills(7))], {'OIL': {'VENDA000001': NET45}}, dates)
        self.assertEqual((out['credit_terms']['credit_days'], out['credit_terms']['due_date']), (45, '2026-10-16'))

    def test_an_advance_has_a_credit_period_but_no_due_date(self):
        (out,), _ = run([row(1, documents=[{'kind': 'PO', 'sap_doc_entry': 3}])], {'OIL': {'VENDA000001': UNSET}})
        self.assertEqual((out['credit_terms']['credit_days'], out['credit_terms']['due_date']), (30, None))

    def test_employees_and_partners_not_in_sap_are_not_looked_up(self):
        rows = [row(1, request_type='EMPLOYEE_ADVANCE', partner='1113001'),
                row(2, partner='NOSAP:JWPL0001', partner_not_in_sap=True)]
        with mock.patch.object(sap_service, 'credit_terms') as lookup:
            out = views._with_credit_terms(rows)
        lookup.assert_not_called()
        self.assertEqual([r['credit_terms'] for r in out], [None, None])

    def test_sap_down_leaves_the_list_without_terms_not_broken(self):
        with mock.patch.object(sap_service, 'credit_terms', side_effect=sap_service.SapUnavailable('down')), \
                mock.patch.object(sap_service, 'bill_dates', side_effect=sap_service.SapUnavailable('down')):
            (out,) = views._with_credit_terms([row(1, **bills(7))])
        self.assertIsNone(out['credit_terms'])

    def test_bill_dates_down_still_gives_the_credit_period(self):
        with mock.patch.object(sap_service, 'credit_terms', return_value={'VENDA000001': UNSET}), \
                mock.patch.object(sap_service, 'bill_dates', side_effect=sap_service.SapUnavailable('down')):
            (out,) = views._with_credit_terms([row(1, **bills(7))])
        self.assertEqual((out['credit_terms']['credit_days'], out['credit_terms']['due_date']), (30, None))
