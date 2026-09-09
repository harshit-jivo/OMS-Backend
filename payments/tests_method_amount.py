"""A payment method amount must be strictly positive, rejected at the API.

Regression cover for a real defect: PATCHing a method amount to 0.00 passed
every serializer rule and was refused by the database CHECK constraint
`payment_method_amount_positive`, surfacing as an unhandled IntegrityError —
HTTP 500 — instead of a validation error.

The constraint is the right final boundary and stays. These tests assert the
application refuses the value first, so the constraint is never the thing the
user meets.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from .models import PaymentMethodEntry, PaymentReceipt
from .permissions import PAYMENTS_CREATE


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role,
        extra_pages=list(keys))


class MethodAmountValidationTests(TestCase):
    def setUp(self):
        self.creator = _user('ma_creator', [PAYMENTS_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.creator)

        self.receipt = PaymentReceipt.objects.create(
            receipt_no='RC-MA-1', company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1, created_by=self.creator)
        self.entry = PaymentMethodEntry.objects.create(
            receipt=self.receipt, method=PaymentMethodEntry.Method.UPI,
            amount=Decimal('100.00'), upi_reference='UTR123')

    def _patch(self, methods):
        return self.client.patch(
            reverse('payment-receipt-detail', args=[self.receipt.pk]),
            {'methods': methods}, format='json')

    def _assert_rejected_and_unchanged(self, resp):
        """400, a field error naming the amount, and nothing written."""
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn('methods', resp.data['errors'])
        # The stored row is untouched — the whole point of failing before the
        # write rather than after a partial one. `update()` deletes the old
        # rows before recreating them, so a late failure could otherwise leave
        # the receipt with NO methods at all.
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.amount, Decimal('100.00'))
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.total_amount, Decimal('100.00'))
        self.assertEqual(self.receipt.methods.count(), 1)

    # CASE A — the reported bug.
    def test_patch_zero_amount_is_400_not_500(self):
        resp = self._patch([
            {'method': 'UPI', 'amount': '0.00', 'upi_reference': 'UTR123'}])
        self._assert_rejected_and_unchanged(resp)

    # CASE B
    def test_patch_negative_amount_is_400(self):
        resp = self._patch([
            {'method': 'UPI', 'amount': '-10.00', 'upi_reference': 'UTR123'}])
        self._assert_rejected_and_unchanged(resp)

    def test_patch_small_negative_amount_is_400(self):
        """-0.01 rounds to a stored value the CHECK would also refuse."""
        resp = self._patch([
            {'method': 'UPI', 'amount': '-0.01', 'upi_reference': 'UTR123'}])
        self._assert_rejected_and_unchanged(resp)

    # CASE C
    def test_patch_smallest_positive_amount_succeeds(self):
        resp = self._patch([
            {'method': 'UPI', 'amount': '0.01', 'upi_reference': 'UTR123'}])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.total_amount, Decimal('0.01'))

    def test_patch_valid_amount_still_updates(self):
        resp = self._patch([
            {'method': 'UPI', 'amount': '250.00', 'upi_reference': 'UTR999'}])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.total_amount, Decimal('250.00'))
        self.assertEqual(self.receipt.methods.first().amount,
                         Decimal('250.00'))

    # CASE D
    def test_patch_unrelated_field_is_unaffected(self):
        resp = self.client.patch(
            reverse('payment-receipt-detail', args=[self.receipt.pk]),
            {'remarks': 'Note only.'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.remarks, 'Note only.')
        self.assertEqual(self.receipt.total_amount, Decimal('100.00'))

    # CASE E — one of several lines zeroed. The one-method rule refuses the
    # multi-line payload first, so this asserts a 400 rather than which rule
    # caught it; either way the zero never reaches the database.
    def test_patch_one_of_several_methods_to_zero_is_400(self):
        resp = self._patch([
            {'method': 'UPI', 'amount': '100.00', 'upi_reference': 'UTR1'},
            {'method': 'CHEQUE', 'amount': '0.00', 'cheque_number': 'C1',
             'bank_name': 'HDFC', 'cheque_date': str(date.today())},
        ])
        self._assert_rejected_and_unchanged(resp)

    # CASE F — the amount/total relationship still holds after a valid edit.
    def test_total_still_follows_the_method_amount(self):
        resp = self._patch([
            {'method': 'UPI', 'amount': '75.50', 'upi_reference': 'UTR2'}])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.total_amount, Decimal('75.50'))

    # CREATE and PATCH must share one rule (task section 8).
    def test_create_rejects_zero_amount_the_same_way(self):
        resp = self.client.post(
            reverse('payment-receipt-list'),
            {'company': 'OIL', 'card_code': 'CUST2',
             'payment_date': str(date.today()), 'is_advance': True,
             'methods': [{'method': 'UPI', 'amount': '0.00',
                          'upi_reference': 'UTR5'}]},
            format='json')
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn('methods', resp.data['errors'])
        self.assertFalse(
            PaymentReceipt.objects.filter(card_code='CUST2').exists())

    def test_create_rejects_negative_amount(self):
        resp = self.client.post(
            reverse('payment-receipt-list'),
            {'company': 'OIL', 'card_code': 'CUST3',
             'payment_date': str(date.today()), 'is_advance': True,
             'methods': [{'method': 'UPI', 'amount': '-5.00',
                          'upi_reference': 'UTR6'}]},
            format='json')
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(
            PaymentReceipt.objects.filter(card_code='CUST3').exists())

    def test_the_database_constraint_is_still_in_place(self):
        """The fix must not have replaced the final integrity boundary."""
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentMethodEntry.objects.create(
                    receipt=self.receipt,
                    method=PaymentMethodEntry.Method.UPI,
                    amount=Decimal('0.00'))
