"""The receipt list must not scale its query count with its row count.

Written after the home dashboard was measured at 58 SECONDS and 212 queries
for 49 receipts. Three fields were resolving per row:

  * `sap_branch`      — a HANA round trip per receipt
  * `deposit_account` — the method->account mapping per TENDER LINE
  * `approval`        — `.order_by()` on a prefetched manager, which clones the
                        queryset and silently discards the prefetch

All three are detail-only concerns. These tests assert the list stays flat and
that the detail view still resolves them, so a future "tidy-up" cannot quietly
put the 58 seconds back.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection

from users.models import User, UserRole

from .models import PaymentMethodEntry, PaymentReceipt
from .serializers import PaymentReceiptSerializer
from .views import _receipt_queryset


def _receipt(no, creator):
    receipt = PaymentReceipt.objects.create(
        receipt_no=no, company='OIL', card_code='CUST1',
        payment_date=date.today(), total_amount=Decimal('100.00'),
        is_advance=True, sap_branch_id=1, created_by=creator)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.CASH,
        amount=Decimal('100.00'))
    return receipt


class ReceiptListQueryCountTests(TestCase):
    def setUp(self):
        role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        self.user = User.objects.create(
            username='perf_user', name='Perf', role=role)

    def _serialize_list(self):
        qs = _receipt_queryset(self.user)
        with CaptureQueriesContext(connection) as ctx:
            data = PaymentReceiptSerializer(qs, many=True).data
        return data, len(ctx)

    def test_query_count_does_not_grow_with_rows(self):
        """The N+1 signature: 3 rows and 9 rows must cost the same."""
        for i in range(3):
            _receipt(f'RC-PERF-A{i}', self.user)
        _, three = self._serialize_list()

        for i in range(6):
            _receipt(f'RC-PERF-B{i}', self.user)
        data, nine = self._serialize_list()

        self.assertEqual(len(data), 9)
        self.assertEqual(
            three, nine,
            f'query count scaled with rows: {three} for 3, {nine} for 9',
        )

    def test_list_skips_the_sap_branch_lookup(self):
        """It costs one HANA round trip per row and no list renders it."""
        _receipt('RC-PERF-C1', self.user)
        with patch('payments.hana_queries.fetch_invoice_branches') as hana:
            data, _ = self._serialize_list()
        hana.assert_not_called()
        self.assertIsNone(data[0]['sap_branch'])

    def test_list_skips_the_deposit_account_lookup(self):
        _receipt('RC-PERF-C2', self.user)
        data, _ = self._serialize_list()
        self.assertIsNone(data[0]['methods'][0]['deposit_account'])

    def test_detail_still_resolves_both(self):
        """The fields are skipped for LISTS only — a detail view needs them."""
        receipt = _receipt('RC-PERF-D1', self.user)
        with patch(
            'payments.hana_queries.fetch_invoice_branches', return_value=[],
        ):
            data = PaymentReceiptSerializer(receipt).data
        # `sap_branch` resolves to a dict (source "none" for an advance with no
        # allocations) rather than the None a list returns.
        self.assertIsNotNone(data['sap_branch'])

    def test_approval_is_read_from_the_prefetch(self):
        """`.order_by()` on a prefetched manager re-queries; `.all()` does not."""
        from django.contrib.contenttypes.models import ContentType

        from approvals.models import ApprovalRequest, ApprovalWorkflow

        ct = ContentType.objects.get_for_model(PaymentReceipt)
        workflow = ApprovalWorkflow.objects.create(
            code='PAY_PERF', name='Perf', document_type='PAYMENT',
            company='OIL')
        for i in range(4):
            receipt = _receipt(f'RC-PERF-E{i}', self.user)
            ApprovalRequest.objects.create(
                workflow=workflow, content_type=ct, object_id=receipt.pk,
                company='OIL', amount=receipt.total_amount,
                document_number=receipt.receipt_no,
                status=ApprovalRequest.Status.PENDING)

        data, four = self._serialize_list()
        self.assertTrue(all(row['approval'] for row in data))

        for i in range(4, 10):
            receipt = _receipt(f'RC-PERF-E{i}', self.user)
            ApprovalRequest.objects.create(
                workflow=workflow, content_type=ct, object_id=receipt.pk,
                company='OIL', amount=receipt.total_amount,
                document_number=receipt.receipt_no,
                status=ApprovalRequest.Status.PENDING)
        _, ten = self._serialize_list()

        self.assertEqual(
            four, ten,
            f'approval lookup scaled with rows: {four} then {ten}',
        )
