"""Which cash drawer a deposit empties, and the one-source rule.

The source is derived from the receipts being banked: each cash line names the
account it was received into, and a deposit may only empty ONE of them, because
SAP takes a single source account per deposit document.

Lines carried no account before the picker existed and resolved through
`PaymentMethodMapping` instead. That table is retired, so a line with no
account now contributes no source — and a deposit made only of such receipts
is refused with "no cash G/L", exactly as it was when the mapping had no
answer.

Database-free: receipts and deposits are stand-ins.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from payments import bank_master, services
from payments.models import BankDeposit


class _Manager:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def select_related(self, *args, **kwargs):
        return list(self._items)


class _Entry:
    def __init__(self, method, amount='100.00', *, gl=''):
        self.method = method
        self.amount = Decimal(amount)
        # A snapshot is complete or entirely absent — the two states posting
        # distinguishes. For cash the key IS the G/L.
        self.account_key = gl
        self.gl_account = gl
        self.bank_code = ''
        self.receiving_bank_name = 'CASH SALE' if gl else ''
        self.account_number = ''
        self.branch = ''
        self.upi_reference = ''
        self.cheque_number = ''
        self.bank_name = ''
        self.cheque_date = None


class _Receipt:
    def __init__(self, entries, total='100.00'):
        self.receipt_no = 'RCP-1'
        self.total_amount = Decimal(total)
        self.methods = _Manager(entries)


class _Line:
    def __init__(self, receipt):
        self.receipt = receipt
        self.amount = receipt.total_amount


class _Deposit:
    """Stand-in for BankDeposit; records what submit would have saved."""

    def __init__(self, receipts, *, company='OIL', collected=None,
                 source_gl_account='', status=None):
        self.company = company
        self.company_db = 'TEST_DB'
        self.deposit_no = 'DEP-1'
        self.deposit_date = date(2026, 9, 18)
        self.currency = 'INR'
        self.remarks = ''
        self.slip_number = ''
        self.bank_key = 'INB:2201101'
        self.bank_code = 'INB'
        self.bank_gl_account = '2201101'
        self.bank_display_name = 'INDIAN BANK'
        self.source_gl_account = source_gl_account
        self.shortfall_reason = ''
        self.status = status or BankDeposit.Status.DRAFT
        self.sap_doc_entry = None
        self.lines = _Manager([_Line(r) for r in receipts])
        # CASH ONLY, matching services.cash_total_for_lines. Summing
        # `total_amount` would pull in cheques, which a deposit does not
        # collect — each one was banked by its own receipt — and every
        # cheque-bearing fixture would then fail validation for a mismatch
        # it is not there to test.
        total = sum(
            (entry.amount
             for receipt in receipts
             for entry in receipt.methods.all()
             if entry.method == 'CASH'),
            Decimal('0'))
        self.collected_amount = collected if collected is not None else total
        self.deposit_amount = self.collected_amount
        # The real model computes this; the SAP remarks composer reads it to
        # decide whether a shortfall reason needs explaining to SAP. These
        # fixtures bank in full, so there is nothing to explain.
        self.shortfall = self.collected_amount - self.deposit_amount
        self.saved_fields = []

    def save(self, update_fields=None):
        self.saved_fields.append(tuple(update_fields or ()))

    def get_status_display(self):
        return str(self.status).title()


def cash(gl='', amount='100.00'):
    return _Receipt([_Entry('CASH', amount, gl=gl)], total=amount)


def cheque(amount='100.00'):
    return _Receipt([_Entry('CHEQUE', amount)], total=amount)


def mixed(gl, amount='100.00'):
    """One receipt carrying both a cash and a cheque line."""
    return _Receipt([_Entry('CASH', amount, gl=gl), _Entry('CHEQUE', amount)],
                    total=str(Decimal(amount) * 2))


class CashSourceDerivationTests(SimpleTestCase):
    """Which drawer(s) a set of receipts would empty."""

    def _sources(self, receipts):
        return services.cash_sources_for_receipts('OIL', receipts)

    def test_one_account_across_several_receipts(self):
        self.assertEqual(self._sources([cash('1105001'), cash('1105001')]),
                         ['1105001'])

    def test_a_second_drawer_is_reported_separately(self):
        self.assertEqual(self._sources([cash('1105003'), cash('1105003')]),
                         ['1105003'])

    def test_two_drawers_are_both_reported(self):
        self.assertEqual(self._sources([cash('1105001'), cash('1105003')]),
                         ['1105001', '1105003'])

    def test_a_line_with_no_account_contributes_no_source(self):
        """It used to fall back to the CASH mapping, which is now retired.

        A deposit over such a receipt reports "no cash G/L" rather than
        crediting a drawer nobody said the money came from — the same message
        it gave when the mapping had no answer.
        """
        self.assertEqual(self._sources([cash()]), [])

    def test_a_cheque_contributes_no_source(self):
        """It reached the bank when its own receipt posted."""
        self.assertEqual(self._sources([cheque()]), [])

    def test_a_mixed_receipt_contributes_only_its_cash(self):
        self.assertEqual(self._sources([mixed('1105002')]), ['1105002'])

    def test_several_accountless_lines_still_yield_nothing(self):
        self.assertEqual(self._sources([cash(), cash()]), [])


class OneCashSourceRuleTests(SimpleTestCase):
    """The rule itself, and the message the user is given."""

    def test_one_source_is_returned(self):
        self.assertEqual(services.check_one_cash_source(['1105001']),
                         '1105001')

    def test_no_cash_is_blank_not_an_error(self):
        self.assertEqual(services.check_one_cash_source([]), '')

    def test_two_sources_are_refused_and_both_named(self):
        with self.assertRaises(ValidationError) as caught:
            services.check_one_cash_source(['1105001', '1105003'])
        message = str(caught.exception)
        self.assertIn('different CASH SALE accounts', message)
        self.assertIn('1105001', message)
        self.assertIn('1105003', message)
        self.assertIn('separate deposits', message)

    def test_three_sources_are_refused(self):
        with self.assertRaises(ValidationError):
            services.check_one_cash_source(['1105001', '1105002', '1105003'])


class DepositValidationTests(SimpleTestCase):
    """validate_deposit — the last gate before the approval chain."""

    def _validate(self, receipts, **kw):
        deposit = _Deposit(receipts, **kw)
        services.validate_deposit(deposit)
        return deposit

    def _rejected(self, receipts):
        with self.assertRaises(ValidationError) as caught:
            self._validate(receipts)
        return str(caught.exception)

    # Cases A-C: one drawer, whichever it is.
    def test_same_account_twice_is_accepted(self):
        self._validate([cash('1105001'), cash('1105001')])

    def test_the_mayapuri_drawer_alone_is_accepted(self):
        """Option A: a second drawer banks directly, with no sweep."""
        self._validate([cash('1105003'), cash('1105003')])

    def test_the_third_drawer_alone_is_accepted(self):
        self._validate([cash('1105002'), cash('1105002')])

    # Cases D-E: more than one drawer.
    def test_two_different_accounts_are_rejected(self):
        message = self._rejected([cash('1105001'), cash('1105003')])
        self.assertIn('1105001', message)
        self.assertIn('1105003', message)

    def test_another_pair_of_different_accounts_is_rejected(self):
        self._rejected([cash('1105002'), cash('1105003')])

    def test_three_different_accounts_are_rejected(self):
        self._rejected([cash('1105001'), cash('1105002'), cash('1105003')])

    # Cases F-G: legacy beside snapshot.
    def test_an_accountless_line_beside_one_with_an_account_is_accepted(self):
        """The accountless line contributes nothing, so one drawer is named.

        It used to inherit the CASH mapping and could therefore CONTRADICT the
        line beside it. With the mapping gone it simply adds no source, and the
        deposit is judged on the accounts that were actually chosen.
        """
        self._validate([cash(), cash('1105001')])

    def test_two_named_drawers_are_still_rejected(self):
        message = self._rejected([cash('1105001'), cash('1105003')])
        self.assertIn('1105001', message)
        self.assertIn('1105003', message)

    # Cheque and UPI.
    def test_a_cheque_only_deposit_is_accepted(self):
        self._validate([cheque(), cheque()])

    def test_cash_and_cheque_with_one_drawer_is_accepted(self):
        self._validate([cash('1105001'), cheque()])

    def test_upi_is_still_refused(self):
        receipt = _Receipt([_Entry('UPI')])
        with self.assertRaises(ValidationError) as caught:
            self._validate([receipt])
        self.assertIn('cash and cheque', str(caught.exception).lower())


class SubmitFreezesTheSourceTests(SimpleTestCase):
    """At submit the drawer is frozen onto the deposit.

    Exercises `freeze_deposit_source`, which is the part of submit that decides
    the account. Submit itself runs inside a database transaction, so the write
    around it is covered by the database-backed tests.
    """

    def _freeze(self, receipts):
        deposit = _Deposit(receipts)
        services.freeze_deposit_source(deposit)
        return deposit

    def test_the_chosen_drawer_is_stored(self):
        self.assertEqual(self._freeze([cash('1105003')]).source_gl_account,
                         '1105003')

    def test_a_deposit_of_accountless_receipts_stores_nothing(self):
        """Nothing to inherit: the mapping that used to answer is gone."""
        self.assertEqual(self._freeze([cash()]).source_gl_account, '')

    def test_a_cheque_only_deposit_stores_nothing(self):
        self.assertEqual(self._freeze([cheque()]).source_gl_account, '')

    def test_two_drawers_are_refused_before_anything_is_stored(self):
        deposit = _Deposit([cash('1105001'), cash('1105003')])
        with self.assertRaises(ValidationError):
            services.freeze_deposit_source(deposit)
        self.assertEqual(deposit.source_gl_account, '')

    def test_submit_saves_the_source_beside_the_bank_snapshot(self):
        """The columns submit writes — read off the function itself."""
        import inspect
        source = inspect.getsource(services.submit_deposit)
        self.assertIn('freeze_deposit_source(deposit)', source)
        self.assertIn("'source_gl_account'", source)

    def test_the_destination_bank_is_untouched_by_this(self):
        deposit = self._freeze([cash('1105003')])
        self.assertEqual(deposit.bank_gl_account, '2201101')
        self.assertNotEqual(deposit.bank_gl_account, deposit.source_gl_account)


class PostingUsesTheStoredSourceTests(SimpleTestCase):
    """Posting reads the frozen value, not the mapping."""

    def _post(self, deposit):
        captured = {}

        def fake_post(document, payload, user=None):
            captured['payload'] = payload
            return document

        with patch.object(services, 'sap_postable_amount',
                             return_value=Decimal('100.00')), \
                patch.object(services, 'resolve_bpl_id', return_value=1), \
                patch.object(services.hana_queries,
                             'fetch_incoming_payment_series',
                             return_value=2564), \
                patch('payments.sap_poster.post_document', fake_post):
            services.post_deposit_to_sap(deposit)
        return captured.get('payload')

    def test_the_stored_source_is_what_posts(self):
        payload = self._post(_Deposit([cash('1105003')],
                                      source_gl_account='1105003'))
        self.assertEqual(payload['PaymentAccounts'][0]['AccountCode'],
                         '1105003')

    def test_the_source_is_read_off_the_deposit_and_nowhere_else(self):
        """Even when its receipts name a different drawer.

        The frozen value is the authority: it records what was true when the
        deposit was submitted, and a later change to the receipts must not move
        money out of a drawer that has already been emptied.
        """
        deposit = _Deposit([cash('1105003')], source_gl_account='1105001')
        payload = self._post(deposit)
        self.assertEqual(payload['PaymentAccounts'][0]['AccountCode'],
                         '1105001')

    def test_exactly_one_sap_account_line_is_sent(self):
        payload = self._post(_Deposit([cash('1105001')],
                                      source_gl_account='1105001'))
        self.assertEqual(len(payload['PaymentAccounts']), 1)

    def test_the_destination_bank_is_still_the_transfer_account(self):
        payload = self._post(_Deposit([cash('1105001')],
                                      source_gl_account='1105001'))
        self.assertEqual(payload['TransferAccount'], '2201101')

