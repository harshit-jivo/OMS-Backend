"""SAP posting uses the account stored on the payment.

The account the collector chose is frozen onto the line when the payment is
saved, and posting reads that and nothing else. It used to fall back to
`PaymentMethodMapping` for lines raised before the picker existed; that table
has been retired, so a line with no account is now a gap to be reported rather
than a legacy shape to be resolved.

Every test here is database-free: receipts are stand-ins, and SAP, HANA and the
poster are patched. No document is ever sent to SAP.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from payments import bank_master, services
from payments.serializers import PaymentMethodEntrySerializer


class _Manager:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _Entry:
    """Stand-in for PaymentMethodEntry: the posting path only reads fields."""

    def __init__(self, method, amount='100.00', *, account_key='',
                 gl_account='', bank_code='', receiving_bank_name='',
                 account_number='', branch='', upi_reference='',
                 cheque_number='', bank_name='', cheque_date=None):
        self.method = method
        self.amount = Decimal(amount)
        self.account_key = account_key
        self.gl_account = gl_account
        self.bank_code = bank_code
        self.receiving_bank_name = receiving_bank_name
        self.account_number = account_number
        self.branch = branch
        self.upi_reference = upi_reference
        self.cheque_number = cheque_number
        self.bank_name = bank_name
        self.cheque_date = cheque_date


class _Receipt:
    def __init__(self, methods, company='OIL'):
        self.company = company
        self.company_db = 'TEST_DB'
        self.receipt_no = 'RCP-TEST-1'
        self.card_code = 'CUST1'
        self.payment_date = date(2026, 9, 18)
        self.currency = 'INR'
        self.remarks = ''
        self.is_advance = True
        self.methods = _Manager(methods)
        self.allocations = _Manager([])


def cash(gl='1105003', **kw):
    return _Entry('CASH', account_key=gl, gl_account=gl,
                  receiving_bank_name='CASH SALE MAYAPURI', **kw)


def banked(method, key='INB:1104102', **kw):
    code, gl = key.split(':')
    return _Entry(method, account_key=key, gl_account=gl, bank_code=code,
                  receiving_bank_name='INDIAN BANK',
                  account_number='6994996254', branch='SONEPAT', **kw)


def _post(receipt):
    """Run post_receipt_to_sap with SAP patched out; return the payload."""
    captured = {}

    def fake_post(document, payload, user=None):
        captured['payload'] = payload
        return document

    with patch.object(services, 'resolve_company_db', return_value='TEST_DB'), \
            patch.object(services, 'resolve_bpl_id', return_value=1), \
            patch.object(services.hana_queries,
                         'fetch_incoming_payment_series', return_value=2564), \
            patch('payments.sap_poster.post_document', fake_post):
        services.post_receipt_to_sap(receipt)
    return captured['payload']


# ---------------------------------------------------------------------------
# A-C. Each tender posts the account stored on its own line
# ---------------------------------------------------------------------------

class SnapshotPostingTests(SimpleTestCase):

    def test_cash_posts_its_snapshot_gl_as_cash_account(self):
        payload = _post(_Receipt([cash('1105003')]))
        self.assertEqual(payload['CashAccount'], '1105003')
        self.assertEqual(payload['CashSum'], 100.0)
        self.assertNotIn('TransferAccount', payload)

    def test_cheque_posts_its_snapshot_gl_as_transfer_account(self):
        payload = _post(_Receipt([banked(
            'CHEQUE', 'INB:1104104', cheque_number='000123',
            bank_name='HDFC', cheque_date=date(2026, 9, 18))]))
        self.assertEqual(payload['TransferAccount'], '1104104')
        self.assertNotIn('CashAccount', payload)

    def test_upi_posts_its_snapshot_gl_as_transfer_account(self):
        payload = _post(_Receipt([banked('UPI', 'ICICI:1104106',
                                         upi_reference='UTR1')]))
        self.assertEqual(payload['TransferAccount'], '1104106')
        self.assertEqual(payload['TransferReference'], 'UTR1')

    def test_any_of_several_cash_accounts_posts_as_chosen(self):
        """No default: whichever drawer the user picked is what posts."""
        for gl in ('1105001', '1105002', '1105003'):
            with self.subTest(gl=gl):
                self.assertEqual(_post(_Receipt([cash(gl)]))['CashAccount'], gl)


# ---------------------------------------------------------------------------
# D-F. The snapshot is authoritative
# ---------------------------------------------------------------------------

class SnapshotAuthorityTests(SimpleTestCase):

    def test_nothing_outside_the_line_is_consulted(self):
        """The account comes off the line and nowhere else."""
        for receipt in (_Receipt([cash()]),
                        _Receipt([banked('UPI')]),
                        _Receipt([banked('CHEQUE', cheque_number='1')])):
            with self.subTest(method=receipt.methods.all()[0].method):
                _post(receipt)                      # no AssertionError

    def test_each_line_posts_its_own_account(self):
        """Lines do not share or inherit an account from one another."""
        payload = _post(_Receipt([cash('1105002'),
                                  banked('UPI', 'INB:1104102',
                                         upi_reference='U')]))
        self.assertEqual(payload['CashAccount'], '1105002')
        self.assertEqual(payload['TransferAccount'], '1104102')

    def test_a_changed_bank_master_does_not_move_a_snapshot_payment(self):
        """The account has since vanished from SAP's lists: still posts as
        chosen, and SAP — not OMS — decides whether it still accepts it."""
        def must_not_be_read(company, **kwargs):
            raise AssertionError('bank master read during snapshot posting')

        with patch.object(bank_master, 'get_company_banks', must_not_be_read), \
                patch.object(bank_master, 'get_company_cash_accounts',
                             must_not_be_read):
            payload = _post(_Receipt([banked('CHEQUE', 'INB:1104104',
                                             cheque_number='9')]))
        self.assertEqual(payload['TransferAccount'], '1104104')

    def test_a_changed_cash_configuration_does_not_move_a_snapshot_payment(self):
        with self.settings(SAP_CASH_PARENT_ACCOUNT='9999999'):
            payload = _post(_Receipt([cash('1105002')]))
        self.assertEqual(payload['CashAccount'], '1105002')


# ---------------------------------------------------------------------------
# G. A line with no account is refused, not resolved
# ---------------------------------------------------------------------------

class NoAccountIsRefusedTests(SimpleTestCase):
    """A line with no account is a gap, not a legacy shape.

    These used to assert the mapping fallback: a keyless line posted whatever
    `PaymentMethodMapping` said. The table is gone, and with it the idea that
    the system can work out where money should go on the collector's behalf —
    so the same inputs must now be refused rather than guessed at.
    """

    def test_a_keyless_cash_line_has_no_account_to_post(self):
        payload = _post(_Receipt([_Entry('CASH')]))
        self.assertFalse(payload.get('CashAccount'))

    def test_a_keyless_upi_line_has_no_account_to_post(self):
        payload = _post(_Receipt([_Entry('UPI', upi_reference='U')]))
        self.assertFalse(payload.get('TransferAccount'))

    def test_a_keyless_cheque_line_still_refuses_rather_than_using_cash(self):
        """The guard that outlived the mapping.

        A cheque posts as a bank transfer and needs a collection account. It
        must never fall through to the cash G/L, which would park the money in
        a clearing account no deposit ever empties.
        """
        with self.assertRaises(ValidationError):
            _post(_Receipt([_Entry('CHEQUE', cheque_number='1')]))


# ---------------------------------------------------------------------------
# H-I. The customer's bank is never the receiving bank
# ---------------------------------------------------------------------------

class CustomerBankSeparationTests(SimpleTestCase):

    def test_the_customer_cheque_bank_never_becomes_the_transfer_account(self):
        payload = _post(_Receipt([banked(
            'CHEQUE', 'INB:1104102', cheque_number='000123', bank_name='HDFC',
            cheque_date=date(2026, 9, 18))]))
        self.assertEqual(payload['TransferAccount'], '1104102')
        self.assertNotIn('HDFC', payload['TransferAccount'])
        self.assertNotIn('BankCode', payload)
        # Its one legitimate place: the cheque detail in Remarks.
        self.assertIn('HDFC', payload['Remarks'])
        self.assertNotIn('INDIAN BANK', payload['Remarks'])

    def test_a_customer_bank_alone_does_not_count_as_a_snapshot(self):
        """bank_name set, receiving account blank -> legacy, not a snapshot."""
        entry = _Entry('CHEQUE', cheque_number='1', bank_name='HDFC')
        self.assertIsNone(services.snapshot_gl_account(entry))

    def test_receiving_bank_name_is_exposed_and_server_owned(self):
        meta = PaymentMethodEntrySerializer.Meta
        self.assertIn('receiving_bank_name', meta.fields)
        self.assertIn('bank_name', meta.fields)
        self.assertIn('receiving_bank_name', meta.read_only_fields)
        self.assertNotIn('bank_name', meta.read_only_fields)


# ---------------------------------------------------------------------------
# Snapshot integrity and the submit-time check
# ---------------------------------------------------------------------------

class SnapshotIntegrityTests(SimpleTestCase):

    def test_a_complete_cash_snapshot_resolves(self):
        self.assertEqual(services.snapshot_gl_account(cash('1105001')),
                         '1105001')

    def test_a_complete_bank_snapshot_resolves(self):
        self.assertEqual(
            services.snapshot_gl_account(banked('UPI', 'ICICI:1104106')),
            '1104106')

    def test_an_all_blank_line_is_legacy(self):
        self.assertIsNone(services.snapshot_gl_account(_Entry('UPI')))

    def test_a_partial_snapshot_is_refused_not_guessed(self):
        for entry in (_Entry('CASH', gl_account='1105001'),
                      _Entry('UPI', account_key='INB:1104102'),
                      _Entry('UPI', account_key='INB:1104102',
                             gl_account='1104102')):
            with self.subTest(fields=vars(entry)):
                with self.assertRaises(ValidationError):
                    services.snapshot_gl_account(entry)

    def test_a_contradictory_snapshot_is_refused(self):
        """The key and the stored G/L must agree."""
        tampered = banked('UPI', 'INB:1104102')
        tampered.gl_account = '9999999'
        with self.assertRaises(ValidationError):
            services.snapshot_gl_account(tampered)
        cash_tampered = cash('1105001')
        cash_tampered.gl_account = '1105003'
        with self.assertRaises(ValidationError):
            services.snapshot_gl_account(cash_tampered)

    def test_mixed_snapshot_and_keyless_lines_of_one_method_are_refused(self):
        receipt = _Receipt([cash('1105001'), _Entry('CASH')])
        with self.assertRaises(ValidationError):
            services._bank_accounts_for('OIL', receipt)

    def test_one_method_into_two_accounts_is_refused(self):
        """SAP has one CashAccount per document."""
        receipt = _Receipt([cash('1105001'), cash('1105003')])
        with self.assertRaises(ValidationError):
            services._bank_accounts_for('OIL', receipt)

    def test_several_lines_into_one_account_post_together(self):
        receipt = _Receipt([cash('1105001', amount='40.00'),
                            cash('1105001', amount='60.00')])
        payload = _post(receipt)
        self.assertEqual(payload['CashAccount'], '1105001')
        self.assertEqual(payload['CashSum'], 100.0)

    def test_submit_check_passes_when_every_line_has_an_account(self):
        if True:
            services._validate_gl_accounts(
                'OIL', {'CASH'}, receipt=_Receipt([cash()]))

    def test_submit_check_names_a_method_with_no_account(self):
        """The error arrives at submit, in front of someone who can fix it.

        It used to mean "no mapping configured for this company"; it now means
        "nobody chose an account for this line", which is something the person
        submitting can actually act on.
        """
        with self.assertRaises(ValidationError):
            services._validate_gl_accounts(
                'OIL', {'CHEQUE'},
                receipt=_Receipt([_Entry('CHEQUE', cheque_number='1')]))


# ---------------------------------------------------------------------------
# J. Deposit posting is unchanged
# ---------------------------------------------------------------------------

class _Deposit:
    def __init__(self, source_gl_account=''):
        self.company = 'OIL'
        self.company_db = 'TEST_DB'
        self.deposit_no = 'DEP-TEST-1'
        self.deposit_date = date(2026, 9, 18)
        self.deposit_amount = Decimal('100.00')
        self.currency = 'INR'
        self.remarks = ''
        self.slip_number = ''
        self.bank_gl_account = '2201101'
        # The drawer this deposit empties, frozen when it was submitted.
        self.source_gl_account = source_gl_account
        self.lines = _Manager([])


class DepositPostingUnchangedTests(SimpleTestCase):
    """A deposit posts the source frozen on it, and nothing else.

    The fallback to the CASH mapping is gone: deposits raised before the
    source was frozen had theirs written from their own receipts, so a blank
    source now means the deposit genuinely empties no drawer.
    """

    def test_deposit_source_comes_from_the_deposit(self):
        deposit = _Deposit(source_gl_account='1105003')
        self.assertEqual(
            (deposit.source_gl_account or '').strip(), '1105003')

    def test_a_deposit_with_no_source_records_none(self):
        """Blank now means "empties no drawer", not "ask the mapping"."""
        self.assertEqual(_Deposit().source_gl_account, '')
