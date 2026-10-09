"""SAP's own checks on an outgoing payment (`services/sap_rules.py`).

Each is one SAP refused an OMS payment with: 460007 payment mode, 4612 urgency
after 5 PM, 460008 type of advance, 460009 settlement date.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.services import sap_rules

DAY = datetime.date(2026, 10, 9)
UTC = datetime.timezone.utc
EVENING = datetime.datetime(2026, 10, 9, 12, 23, tzinfo=UTC)   # 17:53 IST
MORNING = datetime.datetime(2026, 10, 9, 5, 0, tzinfo=UTC)     # 10:30 IST


def body(doc_type='rSupplier', **extra):
    return {'DocDate': DAY.isoformat(), 'DocType': doc_type, **extra}


def advance(**dates):
    return SimpleNamespace(expected_date=None, expected_bill_date=None, **dates)


class WhatIsFilled(SimpleTestCase):
    def test_a_vendor_advance_on_account(self):
        p = sap_rules.apply(body(), advance(), posting_date=DAY, now=EVENING)
        self.assertEqual(p['U_URGENCY'], 'H')
        self.assertEqual(p['U_Type_of_Advance'], 'One Time Settlement')
        self.assertEqual(p['U_Adv_Settl_Dt'], '2026-11-08')

    def test_a_customer_refund_on_account_is_an_advance_too(self):
        p = sap_rules.apply(body('rCustomer'), advance(), posting_date=DAY, now=MORNING)
        self.assertEqual(p['U_Type_of_Advance'], 'One Time Settlement')
        self.assertEqual(p['U_Adv_Settl_Dt'], '2026-11-08')
        self.assertNotIn('U_URGENCY', p)

    def test_a_payment_against_bills_settles_nothing_later(self):
        p = sap_rules.apply(body(PaymentInvoices=[{'DocEntry': 1}]), advance(), posting_date=DAY, now=MORNING)
        self.assertEqual(p['U_Type_of_Advance'], 'One Time Settlement')
        self.assertNotIn('U_Adv_Settl_Dt', p)

    def test_an_expense_to_gl_accounts_is_not_a_partner_advance(self):
        p = sap_rules.apply(body('rAccount'), advance(), posting_date=DAY, now=EVENING)
        self.assertEqual(p['U_URGENCY'], 'H')  # urgency is every payment's
        self.assertNotIn('U_Type_of_Advance', p)
        self.assertNotIn('U_Adv_Settl_Dt', p)

    def test_apply_twice_is_apply_once(self):
        once = sap_rules.apply(body(), advance(), posting_date=DAY, now=EVENING)
        self.assertEqual(sap_rules.apply(dict(once), advance(), posting_date=DAY, now=EVENING), once)


class CheckedBeforeSending(SimpleTestCase):
    def test_a_filled_body_passes(self):
        p = sap_rules.apply(body(TransferSum=10.0, U_Pymnt_Mode='NEFT'), advance(), posting_date=DAY, now=EVENING)
        self.assertEqual(sap_rules.problems(p, now=EVENING), [])

    def test_each_gap_is_named_with_its_check(self):
        gaps = ' '.join(sap_rules.problems(body(TransferSum=10.0), now=EVENING))
        for check in ('460007', '4612', '460008', '460009'):
            self.assertIn(check, gaps)

    def test_a_settlement_date_before_the_payment_is_a_gap(self):
        p = body(U_Type_of_Advance='One Time Settlement', U_Adv_Settl_Dt='2026-10-01')
        self.assertIn('before the payment date', ' '.join(sap_rules.problems(p, now=MORNING)))


class RefusalsExplained(SimpleTestCase):
    def test_a_known_check_says_the_rule_has_changed(self):
        message = sap_rules.explain(Exception('(4612) After 5:00 PM Urgency field is mandatory'))
        self.assertIn('urgent after 5 PM', message)
        self.assertIn('send this message to IT', message)

    def test_an_unknown_check_is_named_for_it(self):
        message = sap_rules.explain(Exception('(470001) Something new'))
        self.assertIn('Check 470001 is a rule in SAP that OMS does not know yet', message)

    def test_the_code_comes_from_sap_when_it_says_it(self):
        error = Exception('Validation failed')
        error.sap_code = '-460009'
        self.assertEqual(sap_rules.code_of(error), '460009')

    def test_no_code_no_advice(self):
        self.assertEqual(sap_rules.explain(Exception('Balance due exceeded')),
                         'SAP refused the payment: Balance due exceeded')
