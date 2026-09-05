"""An UPDATED history row says WHICH fields changed, and nothing else does.

`change_data` answers the one question the timeline could not: an edit row used
to say "Receipt edited" and leave the reader to guess what moved. These tests
pin the two halves of that promise — the diff is accurate and JSON-safe, and it
is absent everywhere an edit did not happen.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from . import services
from .models import (
    PaymentAllocation,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
)
from .permissions import PAYMENTS_CREATE
from .serializers import PaymentReceiptCreateSerializer

Action = PaymentStatusHistory.Action


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role,
        extra_pages=list(keys))


def _receipt(no, creator, **overrides):
    fields = dict(
        receipt_no=no, company='OIL', card_code='CUST1',
        payment_date=date(2026, 9, 1), total_amount=Decimal('1000.00'),
        is_advance=True, sap_branch_id=1,
        status=PaymentReceipt.Status.DRAFT, created_by=creator)
    fields.update(overrides)
    receipt = PaymentReceipt.objects.create(**fields)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.UPI,
        upi_reference='UTR-BASE', amount=fields['total_amount'])
    return receipt


class _EditMixin:
    """Drives a real PATCH through the serializer, as the API does."""

    def _edit(self, receipt, payload):
        request = type('R', (), {'user': self.user})()
        serializer = PaymentReceiptCreateSerializer(
            receipt, data=payload, partial=True,
            context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self._latest(receipt)

    @staticmethod
    def _latest(receipt):
        return PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=receipt.pk,
            action=Action.UPDATED).order_by('-id').first()


class ChangeDataContentTests(_EditMixin, TestCase):
    """The diff names the fields that moved, with their old and new values."""

    def setUp(self):
        self.user = _user('cd_editor', [PAYMENTS_CREATE])

    def test_amount_change_is_recorded_both_sides(self):
        receipt = _receipt('RC-CD-1', self.user)
        row = self._edit(receipt, {'methods': [
            {'method': 'UPI', 'amount': '1200.00',
             'upi_reference': 'UTR-BASE'}]})
        self.assertEqual(row.change_data['amount'],
                         {'old': '1000.00', 'new': '1200.00'})

    def test_money_is_a_string_not_a_float(self):
        """A float would render 1000.00 as 999.9999999999999 in an audit row."""
        receipt = _receipt('RC-CD-2', self.user)
        row = self._edit(receipt, {'methods': [
            {'method': 'UPI', 'amount': '1200.00',
             'upi_reference': 'UTR-BASE'}]})
        for side in ('old', 'new'):
            self.assertIsInstance(row.change_data['amount'][side], str)

    def test_unchanged_fields_are_absent(self):
        receipt = _receipt('RC-CD-3', self.user)
        row = self._edit(receipt, {'remarks': 'Corrected note'})
        self.assertEqual(set(row.change_data), {'remarks'})

    def test_payment_date_is_an_iso_string(self):
        receipt = _receipt('RC-CD-4', self.user)
        row = self._edit(receipt, {'payment_date': '2026-09-03'})
        self.assertEqual(row.change_data['payment_date'],
                         {'old': '2026-09-01', 'new': '2026-09-03'})

    def test_method_switch_records_tender_and_its_details(self):
        receipt = _receipt('RC-CD-5', self.user)
        row = self._edit(receipt, {'methods': [{
            'method': 'CHEQUE', 'amount': '1000.00',
            'cheque_number': '55512', 'bank_name': 'HDFC',
            'cheque_date': '2026-09-04'}]})
        self.assertEqual(row.change_data['payment_method'],
                         {'old': 'UPI', 'new': 'CHEQUE'})
        self.assertEqual(row.change_data['cheque_number'],
                         {'old': '', 'new': '55512'})
        self.assertEqual(row.change_data['cheque_bank']['new'], 'HDFC')
        self.assertEqual(row.change_data['cheque_date']['new'], '2026-09-04')
        # The amount did not move, so it must not appear.
        self.assertNotIn('amount', row.change_data)

    def test_upi_reference_change_is_recorded(self):
        receipt = _receipt('RC-CD-6', self.user)
        PaymentMethodEntry.objects.filter(receipt=receipt).update(
            upi_reference='UTR-OLD')
        row = self._edit(receipt, {'methods': [{
            'method': 'UPI', 'amount': '1000.00',
            'upi_reference': 'UTR-NEW'}]})
        self.assertEqual(row.change_data['upi_reference'],
                         {'old': 'UTR-OLD', 'new': 'UTR-NEW'})

    def test_allocation_change_is_recorded(self):
        receipt = _receipt('RC-CD-7', self.user, is_advance=False)
        PaymentAllocation.objects.create(
            receipt=receipt, sap_doc_entry=11, sap_doc_num=901,
            invoice_type=13, amount_applied=Decimal('400.00'))
        receipt.allocated_amount = Decimal('400.00')
        receipt.save(update_fields=['allocated_amount'])

        row = self._edit(receipt, {'allocations': [{
            'sap_doc_entry': 11, 'sap_doc_num': 901,
            'invoice_type': 13, 'amount_applied': '600.00'}]})
        change = row.change_data['allocations']
        self.assertEqual(change['old'], [{'invoice': 901, 'amount': '400.00'}])
        self.assertEqual(change['new'], [{'invoice': 901, 'amount': '600.00'}])

    def test_reordered_allocations_are_not_a_change(self):
        """Same rows in a different order is not an edit."""
        receipt = _receipt('RC-CD-8', self.user, is_advance=False)
        rows = [
            {'sap_doc_entry': 11, 'sap_doc_num': 901,
             'invoice_type': 13, 'amount_applied': '400.00'},
            {'sap_doc_entry': 12, 'sap_doc_num': 902,
             'invoice_type': 13, 'amount_applied': '600.00'},
        ]
        for row_data in rows:
            PaymentAllocation.objects.create(
                receipt=receipt,
                **{k: (Decimal(v) if k == 'amount_applied' else v)
                   for k, v in row_data.items()})
        receipt.allocated_amount = Decimal('1000.00')
        receipt.save(update_fields=['allocated_amount'])

        row = self._edit(receipt, {'allocations': list(reversed(rows))})
        self.assertIsNone(row.change_data)

    def test_multiple_fields_in_one_edit_all_appear(self):
        receipt = _receipt('RC-CD-9', self.user)
        row = self._edit(receipt, {
            'payment_date': '2026-09-03', 'remarks': 'Both moved',
            'methods': [{'method': 'UPI', 'amount': '1500.00',
                        'upi_reference': 'UTR-BASE'}]})
        self.assertEqual(set(row.change_data),
                         {'payment_date', 'remarks', 'amount'})

    def test_no_op_edit_records_null_not_empty(self):
        """`{}` would read as 'we did not look'; NULL says 'nothing moved'."""
        receipt = _receipt('RC-CD-10', self.user)
        row = self._edit(receipt, {'remarks': ''})
        self.assertIsNone(row.change_data)

    def test_verification_reset_edit_still_carries_the_diff(self):
        receipt = _receipt(
            'RC-CD-11', self.user,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED)
        row = self._edit(receipt, {'methods': [
            {'method': 'UPI', 'amount': '1200.00',
             'upi_reference': 'UTR-BASE'}]})
        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status,
                         PaymentReceipt.VerificationStatus.PENDING)
        self.assertEqual(row.change_data['amount']['new'], '1200.00')


class ChangeDataOnlyOnUpdatedTests(TestCase):
    """Every other action is a transition, so it carries no diff."""

    def setUp(self):
        self.user = _user('cd_actor', [PAYMENTS_CREATE])
        self.receipt = _receipt('RC-CD-20', self.user)

    def test_non_updated_actions_store_null(self):
        for action in (Action.CREATED, Action.PENDING_APPROVAL,
                       Action.APPROVED, Action.REJECTED, Action.VERIFIED,
                       Action.SAP_POSTED, Action.SAP_FAILED,
                       Action.STATUS_CHANGED):
            with self.subTest(action=action):
                row = services.log_status(
                    self.receipt, to_status='DRAFT', user=self.user,
                    action=action)
                self.assertIsNone(row.change_data)

    def test_a_diff_passed_with_a_non_updated_action_is_dropped(self):
        """The guard is in log_status, so no caller can create that row."""
        row = services.log_status(
            self.receipt, to_status='APPROVED', user=self.user,
            action=Action.APPROVED,
            change_data={'amount': {'old': '1.00', 'new': '2.00'}})
        row.refresh_from_db()
        self.assertIsNone(row.change_data)

    def test_updated_keeps_the_diff_it_is_given(self):
        row = services.log_status(
            self.receipt, to_status='DRAFT', user=self.user,
            action=Action.UPDATED,
            change_data={'amount': {'old': '1.00', 'new': '2.00'}})
        row.refresh_from_db()
        self.assertEqual(row.change_data['amount']['new'], '2.00')

    def test_default_is_null_for_callers_that_pass_nothing(self):
        row = services.log_status(
            self.receipt, to_status='DRAFT', user=self.user,
            action=Action.UPDATED)
        self.assertIsNone(row.change_data)


class ChangeDataApiTests(_EditMixin, TestCase):
    """The business timeline endpoint serves the diff."""

    def setUp(self):
        self.user = _user('cd_api', [PAYMENTS_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.receipt = _receipt('RC-CD-30', self.user)
        services.log_status(self.receipt, to_status='DRAFT', user=self.user,
                            action=Action.CREATED, reason='Receipt created.')

    def _history(self):
        resp = self.client.get(
            reverse('payment-receipt-history', args=[self.receipt.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp.data['data']

    def test_change_data_is_present_on_every_row(self):
        """Null on non-edits, so a client handles one shape, not two."""
        rows = self._history()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn('change_data', row)
            self.assertIsNone(row['change_data'])

    def test_edit_row_carries_the_diff_over_the_api(self):
        self._edit(self.receipt, {'remarks': 'Fixed the note'})
        edited = [r for r in self._history() if r['action'] == 'UPDATED']
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0]['change_data']['remarks']['new'],
                         'Fixed the note')

    def test_no_identifying_or_sap_detail_leaks_into_the_diff(self):
        self._edit(self.receipt, {'remarks': 'Fixed the note'})
        edited = [r for r in self._history() if r['action'] == 'UPDATED'][0]
        for banned in ('ip_address', 'sap_doc_entry', 'sap_response',
                       'created_at', 'updated_at', 'attachments'):
            self.assertNotIn(banned, edited['change_data'])
