"""The receiving account is chosen by the user and frozen onto the payment.

Until now a payment stored no account at all: it was recomputed from the admin
mapping every time it was posted or read, so an administrator's edit silently
rewrote where old money went. These tests pin the replacement — the client
sends a KEY, the server resolves it against that company's own SAP list, and
what it resolved to is stored on the line and never recomputed.

Every SAP read is mocked. No HANA connection is opened and no document is
posted.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.db.models import Max
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework import serializers as drf
from rest_framework.test import APIClient

from users.models import User, UserRole

from . import bank_master
from .models import BankDeposit, PaymentMethodEntry, PaymentReceipt
from .serializers import resolve_receiving_account
from .tests_support import only, uniq
from .permissions import PAYMENTS_CREATE

OIL_BANKS = [
    {'bank_code': 'INB', 'display_name': 'INDIAN BANK', 'account_name': 'INB CC',
     'gl_account': '1104106', 'account_number': '7051847887', 'branch': 'DELHI',
     'ifsc': 'IDIB000D001', 'key': 'INB:1104106', 'label': 'INDIAN BANK — 1104106'},
    {'bank_code': 'ICI', 'display_name': 'ICICI BANK', 'account_name': 'ICICI CA',
     'gl_account': '1104105', 'account_number': '623935842322', 'branch': 'DELHI',
     'ifsc': 'ICIC0000004', 'key': 'ICI:1104105', 'label': 'ICICI BANK — 1104105'},
]
BEVERAGES_BANKS = [
    {'bank_code': 'HDF', 'display_name': 'HDFC BANK', 'account_name': 'HDFC CA',
     'gl_account': '1104201', 'account_number': '5001', 'branch': 'DELHI',
     'ifsc': 'HDFC0000001', 'key': 'HDF:1104201', 'label': 'HDFC BANK — 1104201'},
]
OIL_CASH = [
    {'gl_account': '1105001', 'account_name': 'CASH SALE', 'key': '1105001',
     'label': '1105001 - CASH SALE'},
    {'gl_account': '1105003', 'account_name': 'CASH SALE MAYAPURI',
     'key': '1105003', 'label': '1105003 - CASH SALE MAYAPURI'},
]


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(username=username, name=username.title(),
                               role=role, extra_pages=list(keys))


def _sap(company_banks=None, company_cash=None):
    """Patch both SAP lists, company-scoped, for the duration of a block."""
    banks = company_banks or {'OIL': OIL_BANKS, 'BEVERAGES': BEVERAGES_BANKS,
                              'MART': []}
    cash = company_cash or {'OIL': OIL_CASH, 'BEVERAGES': [], 'MART': []}

    def get_banks(company, **kwargs):
        return banks.get(company, []), {'source': 'sap', 'available': True,
                                        'stale': False, 'synced_at': None}

    def get_cash(company, **kwargs):
        return cash.get(company, []), {'source': 'sap', 'available': True,
                                       'stale': False, 'synced_at': None}

    return (patch.object(bank_master, 'get_company_banks', get_banks),
            patch.object(bank_master, 'get_company_cash_accounts', get_cash))


class ReceivingAccountSnapshotTests(TestCase):
    """Creating a receipt stores the account the user picked."""

    def setUp(self):
        self.creator = _user(uniq('ra_creator-'), [PAYMENTS_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.creator)
        self.url = reverse('payment-receipt-list')
        # The suite runs against the shared TEST database, which already holds
        # real receipts and tender lines. Everything asserted below is scoped
        # to what the request under test creates, which is what these tests
        # always meant: "the entry I just made", not "the only entry there is".
        self._high_water = (
            PaymentReceipt.objects.aggregate(m=Max('pk'))['m'] or 0)

    @property
    def _created(self):
        """Receipts this test created — never the pre-existing ones."""
        return PaymentReceipt.objects.filter(pk__gt=self._high_water)

    def _entry(self):
        """The single tender line this test created."""
        return only(PaymentMethodEntry.objects.all(), receipt__in=self._created)

    def _create(self, method, account_key, company='OIL', extra=None):
        entry = {'method': method, 'amount': '100.00'}
        if account_key is not None:
            entry['account_key'] = account_key
        entry.update(extra or {})
        if method == 'CASH':
            entry.setdefault('denominations',
                             [{'denomination': 100, 'quantity': 1}])
        payload = {'company': company, 'card_code': 'CUST1',
                   'card_name': 'A Customer', 'payment_date': str(date.today()),
                   'is_advance': True, 'sap_branch_id': 1, 'methods': [entry]}
        banks, cash = _sap()
        with banks, cash:
            return self.client.post(self.url, payload, format='json')

    CHEQUE_EXTRA = {'cheque_number': '000123', 'bank_name': 'PNB',
                    'cheque_date': str(date.today())}

    def test_a_bank_account_key_is_resolved_and_stored(self):
        resp = self._create('CHEQUE', 'INB:1104106', extra=self.CHEQUE_EXTRA)
        self.assertEqual(resp.status_code, 201, resp.data)
        entry = self._entry()
        self.assertEqual(entry.account_key, 'INB:1104106')
        self.assertEqual(entry.gl_account, '1104106')
        self.assertEqual(entry.bank_code, 'INB')
        self.assertEqual(entry.receiving_bank_name, 'INDIAN BANK')
        self.assertEqual(entry.account_number, '7051847887')
        self.assertEqual(entry.branch, 'DELHI')

    def test_the_customer_cheque_bank_stays_separate(self):
        """`bank_name` is the payer's bank; ours is `receiving_bank_name`."""
        self._create('CHEQUE', 'INB:1104106', extra=self.CHEQUE_EXTRA)
        entry = self._entry()
        self.assertEqual(entry.bank_name, 'PNB')
        self.assertEqual(entry.receiving_bank_name, 'INDIAN BANK')

    def test_a_cash_account_key_is_resolved_and_stored(self):
        resp = self._create('CASH', '1105001')
        self.assertEqual(resp.status_code, 201, resp.data)
        entry = self._entry()
        self.assertEqual(entry.account_key, '1105001')
        self.assertEqual(entry.gl_account, '1105001')
        self.assertEqual(entry.receiving_bank_name, 'CASH SALE')
        # A drawer has no house bank: blank because it does not apply.
        self.assertEqual(entry.bank_code, '')
        self.assertEqual(entry.account_number, '')

    def test_any_of_several_bank_accounts_may_be_chosen(self):
        """No single mapped account per company any more."""
        self._create('UPI', 'ICI:1104105')
        self.assertEqual(self._entry().gl_account, '1104105')

    def test_upi_and_cheque_share_one_bank_list(self):
        """SAP draws no distinction; the same house bank receives both."""
        self._create('UPI', 'INB:1104106')
        self.assertEqual(self._entry().bank_code, 'INB')

    def test_another_companys_account_is_rejected(self):
        resp = self._create('UPI', 'HDF:1104201', company='OIL')
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(self._created.count(), 0)

    def test_an_unknown_account_key_is_rejected(self):
        resp = self._create('UPI', 'NOPE:9999999')
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(self._created.count(), 0)

    def test_a_bank_account_is_rejected_for_cash(self):
        """A drawer and a house bank are not interchangeable."""
        resp = self._create('CASH', 'INB:1104106')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_a_cash_account_is_rejected_for_a_banked_tender(self):
        resp = self._create('UPI', '1105001')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_a_client_supplied_gl_account_is_ignored(self):
        """Only the key decides; the G/L is the server's answer, not input."""
        resp = self._create('UPI', 'INB:1104106',
                            extra={'gl_account': '9999999',
                                   'bank_code': 'HACK'})
        self.assertEqual(resp.status_code, 201, resp.data)
        entry = self._entry()
        self.assertEqual(entry.gl_account, '1104106')
        self.assertEqual(entry.bank_code, 'INB')

    def test_a_receipt_without_a_key_is_still_accepted(self):
        """TEMPORARY: app builds released before the picker send no key."""
        resp = self._create('UPI', None)
        self.assertEqual(resp.status_code, 201, resp.data)
        entry = self._entry()
        self.assertEqual(entry.account_key, '')
        self.assertEqual(entry.gl_account, '')

    def test_sap_unavailable_refuses_rather_than_guesses(self):
        def unavailable(company, **kwargs):
            return [], {'source': 'none', 'available': False, 'stale': False,
                        'synced_at': None}

        payload = {'company': 'OIL', 'card_code': 'CUST1',
                   'card_name': 'A Customer', 'payment_date': str(date.today()),
                   'is_advance': True, 'sap_branch_id': 1,
                   'methods': [{'method': 'UPI', 'amount': '100.00',
                                'account_key': 'INB:1104106'}]}
        with patch.object(bank_master, 'get_company_banks', unavailable):
            resp = self.client.post(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 400, resp.data)


class HistoricalSnapshotTests(TestCase):
    """What was stored stays stored, whatever SAP says later."""

    def setUp(self):
        self.creator = _user('ra_hist', [PAYMENTS_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.creator)
        self.receipt = PaymentReceipt.objects.create(
            receipt_no='RC-RA-1', company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1, created_by=self.creator)
        self.entry = PaymentMethodEntry.objects.create(
            receipt=self.receipt, method=PaymentMethodEntry.Method.CHEQUE,
            amount=Decimal('100.00'), cheque_number='1', bank_name='PNB',
            cheque_date=date.today(),
            account_key='INB:1104106', gl_account='1104106', bank_code='INB',
            receiving_bank_name='INDIAN BANK', account_number='7051847887',
            branch='DELHI')

    def test_the_account_survives_disappearing_from_sap(self):
        """The bank closes; the payment still says where the money went."""
        def gone(company, **kwargs):
            return [], {'source': 'sap', 'available': True, 'stale': False,
                        'synced_at': None}

        with patch.object(bank_master, 'get_company_banks', gone):
            self.client.get(
                reverse('payment-receipt-detail', args=[self.receipt.pk]))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.gl_account, '1104106')
        self.assertEqual(self.entry.receiving_bank_name, 'INDIAN BANK')

    def test_reading_the_receipt_never_rewrites_the_snapshot(self):
        banks, cash = _sap(company_banks={'OIL': [
            {'bank_code': 'INB', 'display_name': 'RENAMED BANK',
             'account_name': 'x', 'gl_account': '9999999',
             'account_number': 'z', 'branch': 'MUMBAI', 'ifsc': '',
             'key': 'INB:9999999', 'label': 'RENAMED'}]})
        with banks, cash:
            self.client.get(
                reverse('payment-receipt-detail', args=[self.receipt.pk]))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.gl_account, '1104106')
        self.assertEqual(self.entry.branch, 'DELHI')


class DepositFieldTests(TestCase):
    """The deposit gains a source account column and no new tender."""

    def test_source_gl_account_exists_and_defaults_blank(self):
        deposit = BankDeposit.objects.create(
            deposit_no='DEP-RA-1', company='OIL', deposit_date=date.today(),
            collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'))
        self.assertEqual(deposit.source_gl_account, '')


# ---------------------------------------------------------------------------
# Database-free: these run through Django's runner with no test database.
# ---------------------------------------------------------------------------

# OIL holds several accounts per bank — the case that exposed the defect.
OIL_MULTI = [
    {'bank_code': 'INB', 'display_name': 'INDIAN BANK', 'account_name': 'a',
     'gl_account': '1104102', 'account_number': '6994996254',
     'branch': 'SONEPAT', 'ifsc': '', 'key': 'INB:1104102', 'label': 'x'},
    {'bank_code': 'INB', 'display_name': 'INDIAN BANK', 'account_name': 'b',
     'gl_account': '1104104', 'account_number': '1', 'branch': 'SONEPAT',
     'ifsc': '', 'key': 'INB:1104104', 'label': 'y'},
    {'bank_code': 'ICICI', 'display_name': 'ICICI BANK', 'account_name': 'c',
     'gl_account': '1104106', 'account_number': '629305042322',
     'branch': 'RAJOURI GARDEN', 'ifsc': '', 'key': 'ICICI:1104106',
     'label': 'z'},
]
BEV_ONLY = [
    {'bank_code': 'HDF', 'display_name': 'HDFC BANK', 'account_name': 'h',
     'gl_account': '1104201', 'account_number': '5001', 'branch': 'DELHI',
     'ifsc': '', 'key': 'HDF:1104201', 'label': 'h'},
]
_META = {'source': 'sap', 'available': True, 'stale': False,
         'synced_at': None}


def _lists(banks=None, cash=None):
    """Company-scoped bank and cash lists, patched for one block."""
    banks = banks if banks is not None else {'OIL': OIL_MULTI,
                                             'BEVERAGES': BEV_ONLY}
    cash = cash if cash is not None else {'OIL': OIL_CASH}
    return (patch.object(bank_master, 'get_company_banks',
                         lambda company, **k: (banks.get(company, []), _META)),
            patch.object(bank_master, 'get_company_cash_accounts',
                         lambda company, **k: (cash.get(company, []), _META)))


class ExactAccountResolutionTests(SimpleTestCase):
    """Incoming Payment accepts only a complete, exact account key."""

    def _resolve(self, company, method, key):
        banks, cash = _lists()
        with banks, cash:
            return resolve_receiving_account(company, method, key)

    def _rejected(self, company, method, key):
        with self.assertRaises(drf.ValidationError):
            self._resolve(company, method, key)

    def test_cash_exact_key_is_accepted(self):
        self.assertEqual(self._resolve('OIL', 'CASH', '1105001')['gl_account'],
                         '1105001')

    def test_cash_with_a_bank_key_is_rejected(self):
        self._rejected('OIL', 'CASH', 'INB:1104102')

    def test_cash_with_the_parent_node_is_rejected(self):
        self._rejected('OIL', 'CASH', '1105000')

    def test_cheque_exact_key_is_accepted(self):
        snap = self._resolve('OIL', 'CHEQUE', 'INB:1104104')
        self.assertEqual(snap['account_key'], 'INB:1104104')
        self.assertEqual(snap['gl_account'], '1104104')

    def test_cheque_with_a_bank_code_only_is_rejected(self):
        # "INB" names three accounts; none may be chosen for the user.
        self._rejected('OIL', 'CHEQUE', 'INB')

    def test_cheque_with_a_gl_only_is_rejected(self):
        self._rejected('OIL', 'CHEQUE', '1104102')

    def test_cheque_with_an_invalid_key_is_rejected(self):
        self._rejected('OIL', 'CHEQUE', 'NOPE:0000000')

    def test_upi_exact_key_is_accepted(self):
        self.assertEqual(
            self._resolve('OIL', 'UPI', 'ICICI:1104106')['bank_code'], 'ICICI')

    def test_upi_with_a_bank_code_only_is_rejected(self):
        self._rejected('OIL', 'UPI', 'ICICI')

    def test_upi_with_a_gl_only_is_rejected(self):
        self._rejected('OIL', 'UPI', '1104106')

    def test_a_partial_or_altered_key_is_rejected(self):
        for key in ('INB:110410', 'INB:1104102X', 'inb:1104102'):
            with self.subTest(key=key):
                self._rejected('OIL', 'UPI', key)

    def test_an_account_only_in_another_company_is_rejected(self):
        # HDF:1104201 exists in BEVERAGES and genuinely not in OIL.
        self._rejected('OIL', 'UPI', 'HDF:1104201')

    def test_the_same_key_resolves_within_each_company(self):
        """Identical keys in two company databases are two valid accounts,
        not a collision — each resolves inside its own company."""
        shared = dict(OIL_MULTI[0])
        banks, cash = _lists(banks={'OIL': [shared], 'BEVERAGES': [shared]})
        with banks, cash:
            for company in ('OIL', 'BEVERAGES'):
                snap = resolve_receiving_account(company, 'UPI', 'INB:1104102')
                self.assertEqual(snap['gl_account'], '1104102')


class DepositFallbackPreservedTests(SimpleTestCase):
    """find_bank keeps its fallbacks; deposits still rely on them.

    BankDeposit create (serializers) and submit (services) resolve stored
    values that may predate the full key. Only the Incoming Payment path moved
    to find_bank_exact.
    """

    def _find(self, selector):
        banks, cash = _lists()
        with banks, cash:
            return bank_master.find_bank('OIL', selector)

    def test_full_key_still_resolves(self):
        self.assertEqual(self._find('ICICI:1104106')['gl_account'], '1104106')

    def test_gl_only_still_resolves_for_deposits(self):
        self.assertEqual(self._find('1104106')['key'], 'ICICI:1104106')

    def test_bank_code_only_still_resolves_for_deposits(self):
        self.assertEqual(self._find('INB')['key'], 'INB:1104102')

    def test_exact_resolver_refuses_what_find_bank_accepts(self):
        banks, cash = _lists()
        with banks, cash:
            for selector in ('INB', '1104106'):
                with self.subTest(selector=selector):
                    self.assertIsNotNone(bank_master.find_bank('OIL', selector))
                    with self.assertRaises(DjangoValidationError):
                        bank_master.find_bank_exact('OIL', selector)


class SnapshotSchemaTests(SimpleTestCase):
    """The columns keep a DATABASE default, so old code can still insert."""

    FIELDS = {PaymentMethodEntry: ('account_key', 'gl_account', 'bank_code',
                                   'receiving_bank_name', 'account_number',
                                   'branch'),
              BankDeposit: ('source_gl_account',)}

    def test_every_snapshot_column_has_a_blank_db_default(self):
        for model, names in self.FIELDS.items():
            for name in names:
                field = model._meta.get_field(name)
                with self.subTest(field=f'{model.__name__}.{name}'):
                    # The raw value, as declared; NOT_PROVIDED when absent.
                    self.assertEqual(field.db_default, '')
                    self.assertFalse(field.null)

    def test_the_customer_cheque_bank_field_is_unchanged(self):
        field = PaymentMethodEntry._meta.get_field('bank_name')
        self.assertEqual(field.max_length, 120)

    def test_upi_is_still_not_depositable(self):
        self.assertNotIn('UPI', PaymentMethodEntry.DEPOSITABLE_METHODS)
        self.assertEqual(PaymentMethodEntry.SAP_POSTABLE_DEPOSIT_METHODS,
                         ('CASH',))
        self.assertNotIn('UPI', dict(BankDeposit.DepositType.choices))
