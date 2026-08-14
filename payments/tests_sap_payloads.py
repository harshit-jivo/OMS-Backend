"""Payload tests for the SAP Incoming Payment builder.

Pins the CHEQUE behaviour proved in docs/CHEQUE_RECEIPT_SAP_EVIDENCE.md against
real ORCT/JDT1 rows, and guards CASH and UPI against regression while the
cheque path changes around them.

Nothing here contacts SAP. `build_incoming_payment` is a pure function: the
caller resolves accounts, branch and series, so every one is injected.
"""
from datetime import date
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from payments import sap_payloads
from payments.hana_queries import series_name_for


class _Entry:
    """Stand-in for PaymentMethodEntry — the builder only reads attributes."""

    def __init__(self, method, amount, *, upi_reference='', cheque_number='',
                 bank_name='', cheque_date=None):
        self.method = method
        self.amount = Decimal(str(amount))
        self.upi_reference = upi_reference
        self.cheque_number = cheque_number
        self.bank_name = bank_name
        self.cheque_date = cheque_date


class _Manager:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _Receipt:
    def __init__(self, methods, *, remarks='', is_advance=False,
                 allocations=(), payment_date=date(2026, 8, 12),
                 receipt_no='RCP-OIL-2026-27-000123'):
        self.receipt_no = receipt_no
        self.card_code = 'CUSTA000844'
        self.payment_date = payment_date
        self.currency = 'INR'
        self.remarks = remarks
        self.is_advance = is_advance
        self.methods = _Manager(methods)
        self.allocations = _Manager(allocations)


ACCOUNTS = {'CASH': '1105003', 'UPI': '2201102', 'CHEQUE': '1104106'}

CHEQUE_KEYS_NEVER_SENT = (
    'PaymentChecks', 'CheckAccount', 'CheckSum', 'CheckNumber', 'BankCode',
    'DueDate',
)


# ---------------------------------------------------------------------------
# A. Cheque payload
# ---------------------------------------------------------------------------

class ChequePayloadTests(SimpleTestCase):
    def _cheque(self, **kwargs):
        entry = _Entry('CHEQUE', 20000, cheque_number='252525',
                       bank_name='HDFC', cheque_date=date(2026, 8, 6))
        receipt = _Receipt([entry], **kwargs)
        return sap_payloads.build_incoming_payment(
            receipt, bank_accounts=ACCOUNTS, bpl_id=2, series=2564)

    def test_cheque_never_sends_sap_cheque_processing_fields(self):
        """The -2028 failure came from PaymentChecks; it must never return."""
        payload = self._cheque()

        for key in CHEQUE_KEYS_NEVER_SENT:
            self.assertNotIn(key, payload)

    def test_cheque_posts_as_a_bank_transfer(self):
        payload = self._cheque()

        self.assertEqual(payload['TransferAccount'], '1104106')
        self.assertEqual(payload['TransferSum'], 20000.0)
        self.assertEqual(payload['DocType'], 'rCustomer')

    def test_cheque_never_uses_the_cash_account(self):
        """A cheque debits the bank. Using the cash G/L would park money in a
        clearing account that no deposit ever empties."""
        payload = self._cheque()

        self.assertNotIn('CashAccount', payload)
        self.assertNotIn('CashSum', payload)
        self.assertNotEqual(payload.get('TransferAccount'), ACCOUNTS['CASH'])

    def test_dates_come_from_the_posting_date_not_the_cheque_date(self):
        """cheque_date is informational. Posting on it would land the document
        in the wrong period and contradict the resolved series."""
        payload = self._cheque()

        for field in ('DocDate', 'TaxDate', 'TransferDate'):
            self.assertEqual(payload[field], '2026-08-12')
            self.assertNotEqual(payload[field], '2026-08-06')

    def test_series_is_included(self):
        self.assertEqual(self._cheque()['Series'], 2564)

    def test_remarks_carry_every_cheque_detail(self):
        remarks = self._cheque()['Remarks']

        self.assertEqual(
            remarks,
            'CHEQUE | RCP-OIL-2026-27-000123 | CHQ 252525 | HDFC | 2026-08-06')

    def test_user_remarks_are_appended_never_substituted(self):
        remarks = self._cheque(remarks='Collected from Rajouri customer')['Remarks']

        self.assertTrue(remarks.startswith(
            'CHEQUE | RCP-OIL-2026-27-000123 | CHQ 252525 | HDFC | 2026-08-06'))
        self.assertIn('Collected from Rajouri customer', remarks)

    def test_overlong_remarks_clip_the_user_text_not_the_identifier(self):
        remarks = self._cheque(remarks='x' * 400)['Remarks']

        self.assertLessEqual(len(remarks), sap_payloads.REMARKS_MAX)
        # The parts Finance searches on must survive intact.
        self.assertIn('RCP-OIL-2026-27-000123', remarks)
        self.assertIn('CHQ 252525', remarks)

    def test_invoice_allocation_is_unchanged(self):
        class _Alloc:
            sap_doc_entry = 63408
            invoice_type = 13
            amount_applied = Decimal('20000')

        payload = self._cheque(allocations=[_Alloc()])

        self.assertEqual(payload['PaymentInvoices'], [{
            'LineNum': 0, 'DocEntry': 63408,
            'InvoiceType': 'it_Invoice', 'SumApplied': 20000.0,
        }])


# ---------------------------------------------------------------------------
# E. Regression — CASH and UPI must not move
# ---------------------------------------------------------------------------

class UnchangedTenderTests(SimpleTestCase):
    def test_cash_payload_is_unchanged(self):
        receipt = _Receipt([_Entry('CASH', 5000)])

        payload = sap_payloads.build_incoming_payment(
            receipt, bank_accounts=ACCOUNTS, bpl_id=2)

        self.assertEqual(payload['CashAccount'], '1105003')
        self.assertEqual(payload['CashSum'], 5000.0)
        self.assertNotIn('TransferAccount', payload)
        self.assertNotIn('TransferSum', payload)
        # Generic remark, not the cheque format.
        self.assertEqual(payload['Remarks'], 'OMS RCP-OIL-2026-27-000123')

    def test_upi_payload_is_unchanged(self):
        receipt = _Receipt([_Entry('UPI', 7500, upi_reference='UPI123456')])

        payload = sap_payloads.build_incoming_payment(
            receipt, bank_accounts=ACCOUNTS, bpl_id=2)

        self.assertEqual(payload['TransferAccount'], '2201102')
        self.assertEqual(payload['TransferSum'], 7500.0)
        self.assertEqual(payload['TransferReference'], 'UPI123456')
        self.assertNotIn('CashAccount', payload)
        self.assertEqual(payload['Remarks'], 'OMS RCP-OIL-2026-27-000123')

    def test_series_is_omitted_when_not_supplied(self):
        """Callers that do not resolve a series must be unaffected."""
        receipt = _Receipt([_Entry('CASH', 100)])

        payload = sap_payloads.build_incoming_payment(
            receipt, bank_accounts=ACCOUNTS)

        self.assertNotIn('Series', payload)

    def test_cash_and_cheque_together_keep_both_tenders(self):
        receipt = _Receipt([
            _Entry('CASH', 1000),
            _Entry('CHEQUE', 2000, cheque_number='99', bank_name='SBI',
                   cheque_date=date(2026, 8, 6)),
        ])

        payload = sap_payloads.build_incoming_payment(
            receipt, bank_accounts=ACCOUNTS, bpl_id=2, series=2564)

        self.assertEqual(payload['CashSum'], 1000.0)
        self.assertEqual(payload['TransferSum'], 2000.0)
        self.assertNotIn('PaymentChecks', payload)
        # A mixed receipt still identifies its cheque.
        self.assertIn('CHQ 99', payload['Remarks'])


# ---------------------------------------------------------------------------
# B. Series resolution
# ---------------------------------------------------------------------------

class SeriesNameTests(SimpleTestCase):
    def test_series_name_is_derived_from_the_calendar_month(self):
        self.assertEqual(series_name_for(date(2026, 7, 15)), 'IP0726')
        self.assertEqual(series_name_for(date(2026, 8, 12)), 'IP0826')

    def test_january_uses_the_calendar_year_not_the_fiscal_year(self):
        """NNM1.Indicator says 'JAN-26-27' for January 2027; SeriesName says
        IP0127. Keying on Indicator would mismatch every Q4 posting."""
        self.assertEqual(series_name_for(date(2027, 1, 10)), 'IP0127')


class SeriesLookupTests(TestCase):
    def _map(self, company='OIL'):
        from payments.models import SapCompanyMap
        return SapCompanyMap.objects.create(
            company=company, display_name=company, company_db='DB',
            hana_schema='SCHEMA', is_active=True)

    @patch('payments.hana_queries.HANAConnection')
    def test_missing_series_fails_before_posting(self, conn):
        self._map()
        conn.return_value.__enter__.return_value.execute.return_value = []

        from payments.hana_queries import fetch_incoming_payment_series
        with self.assertRaises(ValidationError) as caught:
            fetch_incoming_payment_series(company='OIL',
                                          posting_date=date(2026, 8, 12))

        # The message must name the month so Finance knows what to open.
        self.assertIn('IP0826', str(caught.exception))

    @patch('payments.hana_queries.HANAConnection')
    def test_resolved_series_is_returned_as_an_int(self, conn):
        self._map()
        conn.return_value.__enter__.return_value.execute.return_value = [
            {'series': '2564', 'series_name': 'IP0826', 'indicator': 'AUG-26-27'}
        ]

        from payments.hana_queries import fetch_incoming_payment_series
        self.assertEqual(
            fetch_incoming_payment_series(company='OIL',
                                          posting_date=date(2026, 8, 12)),
            2564)


# ---------------------------------------------------------------------------
# D. Deposit eligibility — only CASH may be banked
# ---------------------------------------------------------------------------

class DepositEligibilityTests(TestCase):
    """A UPI or CHEQUE receipt already debited the bank when it posted, so
    banking it again would debit the bank twice and credit a clearing account
    that was never debited. Only cash waits in the clearing account."""

    def setUp(self):
        from users.models import User, UserRole
        from payments.models import (
            BankDeposit, BankDepositLine, CollectionPerson,
            PaymentMethodEntry, PaymentReceipt,
        )
        self.BankDepositLine = BankDepositLine
        self.PaymentMethodEntry = PaymentMethodEntry

        role = UserRole.objects.create(name='collector', display_name='Collector')
        self.user = User.objects.create_user(
            username='dep-tester', password='pw', name='Dep', role=role)
        # deposited_by is a CollectionPerson, not a login.
        self.person = CollectionPerson.objects.create(
            name='Dep Tester', code='ZZDEPTEST', company='OIL')
        self.receipt = PaymentReceipt.objects.create(
            receipt_no='RCP-DEPTEST-1', company='OIL', card_code='C1',
            card_name='Party', payment_date=date(2026, 8, 12),
            total_amount=Decimal('1000'), status=PaymentReceipt.Status.POSTED,
            created_by=self.user)
        self.deposit = BankDeposit.objects.create(
            deposit_no='DEP-TEST-1', company='OIL',
            deposit_date=date(2026, 8, 12), deposited_by=self.person,
            bank_gl_account='1104106', bank_key='ICICI:1104106',
            collected_amount=Decimal('1000'),
            deposit_amount=Decimal('1000'), created_by=self.user)
        BankDepositLine.objects.create(
            deposit=self.deposit, receipt=self.receipt, amount=Decimal('1000'))

    def _with_method(self, method):
        extra = {}
        if method == 'CHEQUE':
            # payment_method_cheque_requires_details enforces these at the DB.
            extra = {'cheque_number': '252525', 'bank_name': 'HDFC',
                     'cheque_date': date(2026, 8, 6)}
        self.PaymentMethodEntry.objects.create(
            receipt=self.receipt, method=method, amount=Decimal('1000'),
            **extra)

    def test_cash_receipt_can_be_deposited(self):
        from payments.services import validate_deposit
        self._with_method('CASH')

        self.assertTrue(validate_deposit(self.deposit))

    def test_upi_receipt_is_rejected(self):
        from payments.services import validate_deposit
        self._with_method('UPI')

        with self.assertRaises(ValidationError) as caught:
            validate_deposit(self.deposit)
        self.assertIn('UPI', str(caught.exception))

    def test_cheque_receipt_is_accepted(self):
        """A cheque is physically carried to the bank, so the deposit records
        the hand-over. SAP is not posted again — see SapPostableAmountTests."""
        from payments.services import validate_deposit
        self._with_method('CHEQUE')

        self.assertTrue(validate_deposit(self.deposit))


# ---------------------------------------------------------------------------
# Deposit picker — must agree with validate_deposit
# ---------------------------------------------------------------------------

class DepositablePickerTests(TestCase):
    """The picker and validate_deposit must offer/accept the SAME receipts.

    Offering a receipt the submit step then refuses is the worst outcome: the
    user selects it, fills the form, and only then learns it was never
    eligible.
    """

    def setUp(self):
        from users.models import User, UserRole
        from payments.models import PaymentMethodEntry, PaymentReceipt
        self.PaymentMethodEntry = PaymentMethodEntry
        self.PaymentReceipt = PaymentReceipt
        role = UserRole.objects.create(name='admin', display_name='Admin')
        self.user = User.objects.create_superuser(
            username='pick-admin', password='pw', name='Pick')
        self.user.role = role
        self.user.save()

    def _receipt(self, no, methods):
        r = self.PaymentReceipt.objects.create(
            receipt_no=no, company='OIL', card_code='C1', card_name='P',
            payment_date=date(2026, 8, 12), total_amount=Decimal('1000'),
            status=self.PaymentReceipt.Status.POSTED, created_by=self.user)
        for m in methods:
            extra = ({'cheque_number': '1', 'bank_name': 'HDFC',
                      'cheque_date': date(2026, 8, 6)} if m == 'CHEQUE' else {})
            self.PaymentMethodEntry.objects.create(
                receipt=r, method=m, amount=Decimal('1000'), **extra)
        return r

    def _offered(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get('/api/payments/depositable-receipts/?company=OIL')
        body = response.json()
        rows = (body.get('data') or {}).get('results') or body.get('results') or []
        return {row['receipt_no'] for row in rows}

    def test_pure_cash_is_offered(self):
        self._receipt('ZZ-CASH', ['CASH'])
        self.assertIn('ZZ-CASH', self._offered())

    def test_upi_is_not_offered(self):
        self._receipt('ZZ-UPI', ['UPI'])
        self.assertNotIn('ZZ-UPI', self._offered())

    def test_cheque_is_offered(self):
        """Cheques ARE physically carried to the bank, so the deposit records
        the hand-over — even though SAP already has the money."""
        self._receipt('ZZ-CHQ', ['CHEQUE'])
        self.assertIn('ZZ-CHQ', self._offered())

    def test_mixed_cash_and_cheque_is_offered(self):
        self._receipt('ZZ-MIX', ['CASH', 'CHEQUE'])
        self.assertIn('ZZ-MIX', self._offered())

    def test_mixed_cash_and_upi_is_not_offered(self):
        """The case a naive filter() gets wrong: the receipt HAS a depositable
        line, so it matches — but one UPI line disqualifies the whole receipt."""
        self._receipt('ZZ-MIX-UPI', ['CASH', 'UPI'])
        self.assertNotIn('ZZ-MIX-UPI', self._offered())

    def test_mixed_cheque_and_upi_is_not_offered(self):
        self._receipt('ZZ-CHQ-UPI', ['CHEQUE', 'UPI'])
        self.assertNotIn('ZZ-CHQ-UPI', self._offered())

    def test_all_three_methods_is_not_offered(self):
        self._receipt('ZZ-ALL3', ['CASH', 'CHEQUE', 'UPI'])
        self.assertNotIn('ZZ-ALL3', self._offered())


# ---------------------------------------------------------------------------
# E/F. SAP posting behaviour — the double-debit guard
# ---------------------------------------------------------------------------

class SapPostableAmountTests(TestCase):
    """A cheque already debited the bank when its RECEIPT posted, so the
    deposit must never post it again. Only the CASH share reaches SAP."""

    def setUp(self):
        from users.models import User, UserRole
        from payments.models import (
            BankDeposit, BankDepositLine, CollectionPerson,
            PaymentMethodEntry, PaymentReceipt,
        )
        self.BankDepositLine = BankDepositLine
        self.PaymentMethodEntry = PaymentMethodEntry
        self.PaymentReceipt = PaymentReceipt
        self.BankDeposit = BankDeposit
        UserRole.objects.create(name='c', display_name='C')
        self.user = User.objects.create_user(
            username='sap-amt', password='pw', name='S')
        self.person = CollectionPerson.objects.create(
            name='P', code='ZZAMT', company='OIL')

    def _deposit_with(self, methods_and_amounts):
        """One receipt per (method, amount), all in one deposit."""
        deposit = self.BankDeposit.objects.create(
            deposit_no=f'DEP-{len(methods_and_amounts)}-{id(self)%9999}',
            company='OIL', deposit_date=date(2026, 8, 13),
            deposited_by=self.person, bank_gl_account='1104107',
            bank_key='ICICI:1104107',
            collected_amount=sum(a for _, a in methods_and_amounts),
            deposit_amount=sum(a for _, a in methods_and_amounts),
            created_by=self.user)
        for index, (method, amount) in enumerate(methods_and_amounts):
            receipt = self.PaymentReceipt.objects.create(
                receipt_no=f'RCP-AMT-{id(self)%9999}-{index}', company='OIL',
                card_code='C1', card_name='P', payment_date=date(2026, 8, 13),
                total_amount=Decimal(str(amount)),
                status=self.PaymentReceipt.Status.POSTED, created_by=self.user)
            extra = ({'cheque_number': '1470', 'bank_name': 'ICICI',
                      'cheque_date': date(2026, 8, 13)}
                     if method == 'CHEQUE' else {})
            self.PaymentMethodEntry.objects.create(
                receipt=receipt, method=method,
                amount=Decimal(str(amount)), **extra)
            self.BankDepositLine.objects.create(
                deposit=deposit, receipt=receipt, amount=Decimal(str(amount)))
        return deposit

    def test_cash_only_posts_the_full_amount(self):
        from payments.services import sap_postable_amount
        deposit = self._deposit_with([('CASH', 50000)])

        self.assertEqual(sap_postable_amount(deposit), Decimal('50000'))

    def test_cheque_only_posts_nothing(self):
        from payments.services import sap_postable_amount
        deposit = self._deposit_with([('CHEQUE', 20000)])

        self.assertEqual(sap_postable_amount(deposit), Decimal('0'))

    def test_mixed_posts_only_the_cash_share(self):
        """₹50,000 cash + ₹20,000 cheque = ₹70,000 collected, ₹50,000 to SAP."""
        from payments.services import sap_postable_amount
        deposit = self._deposit_with([('CASH', 50000), ('CHEQUE', 20000)])

        self.assertEqual(deposit.collected_amount, Decimal('70000'))
        self.assertEqual(sap_postable_amount(deposit), Decimal('50000'))

    # Patched at its definition site: services imports post_document INSIDE
    # the function, so there is no services.post_document attribute to patch.
    @patch('payments.sap_poster.post_document')
    def test_cheque_only_deposit_makes_zero_sap_calls(self, post_document):
        from payments.services import post_deposit_to_sap
        deposit = self._deposit_with([('CHEQUE', 20000)])
        deposit.company_db = 'DB'
        deposit.save(update_fields=['company_db'])

        result = post_deposit_to_sap(deposit)

        post_document.assert_not_called()
        self.assertEqual(result.status, self.BankDeposit.Status.POSTED)
        # No SAP document exists, so no identifier may be invented.
        self.assertIsNone(result.sap_doc_entry)
        self.assertIsNone(result.sap_doc_num)
        self.assertIsNone(result.sap_trans_id)
        self.assertIn('No SAP posting is required', result.sap_response)


class DepositPayloadTests(SimpleTestCase):
    def test_deposit_is_an_account_type_incoming_payment(self):
        """Verified against TransId 224988: DocType A, CardCode = cash G/L,
        destination bank in the TRANSFER fields."""
        deposit = SimpleNamespace(
            deposit_no='DEP-1', deposit_date=date(2026, 8, 13),
            currency='INR', bank_gl_account='1104107', remarks='',
            slip_number='SLIP-9', deposit_amount=Decimal('50000'))

        payload = sap_payloads.build_deposit(
            deposit, bpl_id=2, series=2564, amount=Decimal('50000'),
            source_gl='1105003')

        self.assertEqual(payload['DocType'], 'A')
        self.assertEqual(payload['CardCode'], '1105003')
        self.assertEqual(payload['TransferAccount'], '1104107')
        self.assertEqual(payload['TransferSum'], 50000.0)
        self.assertEqual(payload['Series'], 2564)

    def test_no_odps_fields_survive(self):
        """ODPS has 0 rows in every company DB — those keys must not be sent."""
        deposit = SimpleNamespace(
            deposit_no='DEP-2', deposit_date=date(2026, 8, 13),
            currency='INR', bank_gl_account='1104107', remarks='',
            slip_number='', deposit_amount=Decimal('100'))

        payload = sap_payloads.build_deposit(deposit, amount=Decimal('100'),
                                             source_gl='1105003')

        for key in ('DepositType', 'DepositAccount', 'AllocationAccount',
                    'Commission', 'JournalRemarks', 'CheckLines', 'TotalLC'):
            self.assertNotIn(key, payload)
