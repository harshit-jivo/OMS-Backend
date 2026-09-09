"""CASH is configured in the method-mapping table, like every other tender.

The cash G/L used to live on a per-company row (`SapCompanyMap`), so this
serializer refused CASH outright — a drawer is not a house bank, and there was
nothing here for it to point at. Migration 0033 moved the G/L into this table
and 0034 dropped the old one, which inverts that reasoning: CASH now belongs
here, and rejecting it left three migrated rows that no API could read or edit.
"""
from django.test import TestCase

from .models import PaymentMethodMapping
from .serializers import PaymentMethodMappingSerializer as Serializer


class CashMappingTests(TestCase):
    """CASH carries a G/L and no bank."""

    def test_cash_can_be_created_with_a_gl_account(self):
        s = Serializer(data={'company': 'MART', 'payment_method': 'CASH',
                             'gl_account': '1105003'})
        self.assertTrue(s.is_valid(), s.errors)

    def test_cash_without_a_gl_account_is_refused(self):
        """It has no house bank to resolve an account from."""
        s = Serializer(data={'company': 'MART', 'payment_method': 'CASH'})
        self.assertFalse(s.is_valid())
        self.assertIn('gl_account', s.errors)

    def test_cash_with_a_bank_key_is_refused(self):
        """Both set would leave the posting account ambiguous."""
        s = Serializer(data={'company': 'MART', 'payment_method': 'CASH',
                             'gl_account': '1105003', 'bank_key': 'INB:2201101'})
        self.assertFalse(s.is_valid())
        self.assertIn('bank_key', s.errors)

    def test_an_existing_cash_row_can_be_deactivated(self):
        """The migrated rows were previously frozen — no PATCH could pass."""
        row = PaymentMethodMapping.objects.create(
            company='OIL', payment_method='CASH', bank_key='',
            gl_account='1105001', is_active=True)
        s = Serializer(row, data={'is_active': False}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)

    def test_gl_account_is_readable(self):
        """The column existed but was absent from the serialized shape."""
        row = PaymentMethodMapping.objects.create(
            company='OIL', payment_method='CASH', bank_key='',
            gl_account='1105001', is_active=True)
        self.assertEqual(Serializer(row).data['gl_account'], '1105001')

    def test_a_second_active_cash_row_is_refused(self):
        """Uniqueness still belongs to the backend, not the form."""
        PaymentMethodMapping.objects.create(
            company='OIL', payment_method='CASH', bank_key='',
            gl_account='1105001', is_active=True)
        s = Serializer(data={'company': 'OIL', 'payment_method': 'CASH',
                             'gl_account': '1105001'})
        self.assertFalse(s.is_valid())
        self.assertIn('payment_method', s.errors)


class BankedMethodTests(TestCase):
    """A banked tender is the mirror image: a bank, and no G/L of its own."""

    def test_a_banked_method_without_a_bank_key_is_refused(self):
        s = Serializer(data={'company': 'OIL', 'payment_method': 'UPI'})
        self.assertFalse(s.is_valid())
        self.assertIn('bank_key', s.errors)

    def test_a_banked_method_may_not_carry_its_own_gl_account(self):
        """Its G/L comes from the house bank in SAP; a copy could disagree."""
        s = Serializer(data={'company': 'OIL', 'payment_method': 'UPI',
                             'bank_key': 'INB:2201101',
                             'gl_account': '9999999'})
        self.assertFalse(s.is_valid())
        self.assertIn('gl_account', s.errors)
