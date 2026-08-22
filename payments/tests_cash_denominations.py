"""The cash note breakdown is required, and must equal the cash amount.

NO DATABASE — `PaymentMethodEntrySerializer.validate()` is a pure function of
its attrs, so these drive it directly. (This project has no test database and
savepoint wrappers have been proved to leak fixtures into live data.)

The gap these pin: the breakdown block used to be guarded by `if rows:`, so an
EMPTY breakdown skipped validation entirely and a cash receipt could be created
for money nobody had counted.
"""
from decimal import Decimal

from django.test import SimpleTestCase
from rest_framework import serializers as drf

from payments.serializers import PaymentMethodEntrySerializer


def _validate(**attrs):
    return PaymentMethodEntrySerializer().validate(dict(attrs))


class CashDenominationRequiredTests(SimpleTestCase):

    def test_cash_without_denominations_is_rejected(self):
        """The bug: this used to pass silently."""
        with self.assertRaises(drf.ValidationError) as ctx:
            _validate(method='CASH', amount=Decimal('42000'))
        self.assertIn('denominations', ctx.exception.detail)
        self.assertIn('required', str(ctx.exception.detail['denominations']))

    def test_cash_with_empty_denomination_list_is_rejected(self):
        with self.assertRaises(drf.ValidationError):
            _validate(method='CASH', amount=Decimal('42000'), denominations=[])

    def test_cash_with_matching_denominations_passes(self):
        """80x500 + 10x200 = 42,000 — the real receipt's breakdown."""
        attrs = _validate(
            method='CASH', amount=Decimal('42000'),
            denominations=[
                {'denomination': Decimal('500'), 'quantity': 80},
                {'denomination': Decimal('200'), 'quantity': 10},
            ])
        self.assertEqual(attrs['amount'], Decimal('42000'))

    def test_short_denominations_are_rejected(self):
        with self.assertRaises(drf.ValidationError) as ctx:
            _validate(method='CASH', amount=Decimal('42000'),
                      denominations=[{'denomination': Decimal('500'),
                                      'quantity': 10}])
        self.assertIn('5000', str(ctx.exception.detail['denominations']))

    def test_over_counted_denominations_are_rejected(self):
        with self.assertRaises(drf.ValidationError):
            _validate(method='CASH', amount=Decimal('1000'),
                      denominations=[{'denomination': Decimal('500'),
                                      'quantity': 4}])

    # -- other methods must not be dragged into the new rule ---------------
    def test_upi_without_denominations_still_passes(self):
        attrs = _validate(method='UPI', amount=Decimal('42000'),
                          upi_reference='UTR123456')
        self.assertEqual(attrs['amount'], Decimal('42000'))

    def test_non_cash_may_not_carry_denominations(self):
        with self.assertRaises(drf.ValidationError) as ctx:
            _validate(method='UPI', amount=Decimal('500'),
                      upi_reference='UTR1',
                      denominations=[{'denomination': Decimal('500'),
                                      'quantity': 1}])
        self.assertIn('Only a cash entry',
                      str(ctx.exception.detail['denominations']))

    def test_zero_amount_cash_is_not_forced_to_have_a_breakdown(self):
        """Nothing typed yet — the amount rule reports that, not this one."""
        attrs = _validate(method='CASH', amount=Decimal('0'))
        self.assertEqual(attrs['amount'], Decimal('0'))
