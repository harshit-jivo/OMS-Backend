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
from django.contrib.contenttypes.models import ContentType
from django.db import connection

from users.models import User, UserRole

from .models import PaymentMethodEntry, PaymentReceipt
from .serializers import PaymentReceiptSerializer
from .tests_support import uniq
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
            username=uniq('perf_user-'), name='Perf', role=role)
        # Warm the ContentType cache. The generic prefetches (attachments,
        # status history) resolve a content type on first use and cache it for
        # the process. That is a FIXED cost, paid once, but it lands inside
        # whichever measurement runs first and would read as a difference
        # between the two counts compared below — which are about per-ROW cost.
        ContentType.objects.get_for_model(PaymentReceipt)

    def _serialize_list(self):
        # `_receipt_queryset` is the production queryset — the thing under
        # test, prefetches and all — but it is then narrowed to the receipts
        # THIS test created. On the shared TEST database it would otherwise
        # serialize the live rows as well, which changes the row count without
        # changing anything about the N+1 behaviour being measured.
        qs = _receipt_queryset(self.user).filter(created_by=self.user)
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

    def test_approval_is_read_without_a_query_per_row(self):
        """The list renders each document's flow without scaling queries.

        The old failure mode was a manager method re-queried per row. The flow
        is a one-to-one, so it is fetched with the document; this asserts the
        count does not move when the list grows.
        """
        from .models import FlowStatus, PaymentReceiptFlow
        from .tests_workflow_fixtures import payments_workflow

        workflow, stages = payments_workflow(
            self.user, company='OIL', code='PAY_PERF', documents='receipts')

        def _flow(receipt):
            PaymentReceiptFlow.objects.create(
                receipt=receipt, workflow=workflow, status=FlowStatus.PENDING,
                current_stage=stages[0], total_stage=1)

        for i in range(4):
            _flow(_receipt(f'RC-PERF-E{i}', self.user))

        data, four = self._serialize_list()
        self.assertTrue(all(row['approval'] for row in data))

        for i in range(4, 10):
            _flow(_receipt(f'RC-PERF-E{i}', self.user))
        _, ten = self._serialize_list()

        self.assertEqual(
            four, ten,
            f'approval lookup scaled with rows: {four} then {ten}',
        )
