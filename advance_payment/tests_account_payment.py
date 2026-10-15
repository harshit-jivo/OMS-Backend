"""Account Payment: paid straight to one G/L account that is no business partner.

Mart keeps some parties as ledger accounts, not partners — Katebaa
(2120014-2120026), its TPT and BSU units (1110401 / 1110403) — and SAP pays
them with an account-type outgoing payment (OVPM DocType A). What is pinned:

* only the accounts `sap.PAYMENT_ACCOUNT_PARENTS` lists may be paid, the
  account's name is SAP's, and a company with none has no Account Payment;
* it is a typed amount, On Account, with no Department Head asked;
* it posts as SAP's own do: `rAccount`, the account in PaymentAccounts;
* it always goes to Avtar, then Payment, Audit, Final — and to no other route.
"""
from unittest import mock

from django.test import SimpleTestCase

from advance_payment import hierarchy
from advance_payment.models import RequestType
from advance_payment.purposes import needs_department_head
from advance_payment.services import requests as request_service
from advance_payment.services import sap as sap_service
from advance_payment.tests_requests import TheSapPayment, _clean, advance_of, line, payout

KATEBAA = {'code': '2120014', 'name': 'KATEBAA RURAL SERVICES & FOUNDATION', 'group': 'CREDITORS MART',
           'balance': '-31459549.000000'}


def account_request(**overrides):
    data = {
        'company': 'MART', 'request_type': 'ACCOUNT', 'payment_against': 'ON_ACCOUNT',
        'partner_code': '2120014', 'partner_name': 'Katebaa', 'amount': '5000000',
        'payment_date': '2026-10-15', 'remarks': 'Payment to Katebaa', 'owner_label': 'Accounts',
        'budget_code': 'BackOff', 'purpose_code': 'INTERCOMPANY',
    }
    data.update(overrides)
    return data


def clean(data, accounts=(KATEBAA,)):
    with mock.patch('advance_payment.services.sap.payment_accounts',
                    side_effect=lambda company, code=None, **_kw: [a for a in accounts if a['code'] == code]):
        return _clean(data)


class RaisingOne(SimpleTestCase):
    def test_pays_an_allowed_account_named_as_sap_names_it(self):
        cleaned = clean(account_request())
        self.assertEqual(cleaned.fields['request_type'], RequestType.ACCOUNT)
        self.assertEqual(cleaned.fields['payment_against'], 'ON_ACCOUNT')
        self.assertEqual(cleaned.fields['partner_code'], '2120014')
        self.assertEqual(cleaned.fields['partner_name'], 'KATEBAA RURAL SERVICES & FOUNDATION')
        self.assertEqual(str(cleaned.fields['amount']), '5000000.00')
        self.assertEqual(cleaned.documents, [])

    def test_refuses_an_account_it_may_not_pay(self):
        with self.assertRaises(request_service.RequestInvalid) as caught:
            clean(account_request(partner_code='1104108', partner_name='ICICI BANK'))
        self.assertIn('1104108 is not an account an Account Payment may pay in MART.', caught.exception.problems)

    def test_is_on_account_with_a_typed_amount_only(self):
        with self.assertRaises(request_service.RequestInvalid) as caught:
            clean(account_request(payment_against='AGAINST_BILL'))
        self.assertIn('That Type and Payment Against pair is not offered.', caught.exception.problems)
        with self.assertRaises(request_service.RequestInvalid) as caught:
            clean(account_request(amount='0'))
        self.assertIn('Enter an amount above zero.', caught.exception.problems)

    def test_asks_no_department_head_in_any_company(self):
        for company in ('OIL', 'BEVERAGES', 'MART'):
            with self.subTest(company):
                self.assertFalse(needs_department_head(company, 'ACCOUNT', 'EMP_ADVANCE'))


class TheAccountsItMayPay(SimpleTestCase):
    def run_sql(self, company, **kwargs):
        with mock.patch.object(sap_service, '_schema', return_value='JIVO_MART_HANADB'), \
                mock.patch.object(sap_service, '_run', return_value=[dict(KATEBAA)]) as run:
            rows = sap_service.payment_accounts(company, **kwargs)
        return rows, run

    def test_mart_pays_its_creditors_and_its_tpt_and_bsu_accounts(self):
        self.assertEqual(sap_service.PAYMENT_ACCOUNT_PARENTS['MART'], ('2120000', '1110400'))
        rows, run = self.run_sql('MART', search='kateb')
        sql, params = run.call_args.args[1], run.call_args.args[2]
        self.assertIn('"FatherNum" IN (?, ?)', sql)
        self.assertEqual(params[:2], ['2120000', '1110400'])
        self.assertEqual(rows[0]['code'], '2120014')

    def test_one_account_by_its_code(self):
        _rows, run = self.run_sql('MART', code='2120014')
        self.assertIn('A."AcctCode" = ?', run.call_args.args[1])
        self.assertEqual(run.call_args.args[2], ['2120000', '1110400', '2120014'])

    def test_a_company_without_any_has_none(self):
        rows, run = self.run_sql('OIL')
        self.assertEqual(rows, [])
        run.assert_not_called()


class ItsSapPayment(TheSapPayment):
    def test_goes_to_the_account_as_sap_s_own_do(self):
        body = self.build(advance_of('ACCOUNT', amount='5000000', partner='2120014'),
                          payout(line(1, 'RTGS', '5000000', '1104108')))
        self.assertEqual(body['DocType'], 'rAccount')
        self.assertNotIn('CardCode', body)
        self.assertEqual(body['PaymentAccounts'][0]['AccountCode'], '2120014')
        self.assertEqual(body['PaymentAccounts'][0]['SumPaid'], 5000000.0)
        # Not a partner: no Type of Advance, no settlement date.
        self.assertNotIn('U_Type_of_Advance', body)
        self.assertNotIn('U_Adv_Settl_Dt', body)


class ItsRoute(SimpleTestCase):
    def test_always_goes_to_avtar_then_payment_audit_final(self):
        route = {r['code']: r for r in hierarchy.routes()}['AP_ACCOUNT']
        self.assertEqual(route['company'], 'ALL')
        self.assertEqual(route['query'], "SELECT id FROM advance_payment_request WHERE request_type = 'ACCOUNT'")
        self.assertEqual(hierarchy.full_stages(route), [
            (hierarchy.OWNER_STAGE, 'avtar'), ('Payment Approval', 'payment'),
            ('Audit Approval', 'audit'), ('Final Approval', 'final')])

    def test_no_other_route_takes_it(self):
        for route in hierarchy.routes():
            if route['code'] == 'AP_ACCOUNT':
                continue
            with self.subTest(route['code']):
                query = route['query']
                # Every other route names the types it takes, or leaves this one out.
                self.assertTrue(
                    "request_type <> 'ACCOUNT'" in query
                    or "request_type NOT IN ('EXPENSE', 'ACCOUNT')" in query
                    or "request_type = 'EXPENSE'" in query
                    or "request_type IN ('EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST')" in query
                    or "request_type = 'EMPLOYEE_ADVANCE'" in query
                    or "request_type = 'EMPLOYEE_IMPREST'" in query,
                    query)
