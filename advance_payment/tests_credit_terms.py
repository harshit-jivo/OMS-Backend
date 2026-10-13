"""The payee's credit period (SAP payment terms) on every request the pages list."""
from unittest import mock

from django.test import SimpleTestCase, override_settings

from advance_payment import views
from advance_payment.services import sap as sap_service

NET30 = {'terms': 'NET-30', 'days': 30, 'set': True}


def row(pk, company='OIL', request_type='VENDOR', partner='VENDA000001', **extra):
    return {'id': pk, 'company': company, 'request_type': request_type, 'partner_code': partner,
            'partner_not_in_sap': False, **extra}


@override_settings(ADVANCE_PAYMENT_CREDIT_TERMS=True)
class CreditTermsOnTheList(SimpleTestCase):
    def test_one_sap_query_per_company_for_the_whole_list(self):
        rows = [row(1), row(2, partner='VENDA000002'), row(3, company='MART')]
        terms = {'OIL': {'VENDA000001': NET30}, 'MART': {}}
        with mock.patch.object(sap_service, 'credit_terms', side_effect=lambda c, codes: terms[c]) as lookup:
            out = views._with_credit_terms(rows)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(sorted(lookup.call_args_list[0].args[1]), ['VENDA000001', 'VENDA000002'])
        self.assertEqual([r['credit_terms'] for r in out], [NET30, None, None])

    def test_employees_and_partners_not_in_sap_are_not_looked_up(self):
        rows = [row(1, request_type='EMPLOYEE_ADVANCE', partner='1113001'),
                row(2, partner='NOSAP:JWPL0001', partner_not_in_sap=True)]
        with mock.patch.object(sap_service, 'credit_terms') as lookup:
            out = views._with_credit_terms(rows)
        lookup.assert_not_called()
        self.assertEqual([r['credit_terms'] for r in out], [None, None])

    def test_sap_down_leaves_the_list_without_terms_not_broken(self):
        with mock.patch.object(sap_service, 'credit_terms', side_effect=sap_service.SapUnavailable('down')):
            (out,) = views._with_credit_terms([row(1)])
        self.assertIsNone(out['credit_terms'])
