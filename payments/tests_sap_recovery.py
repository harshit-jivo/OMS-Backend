"""The stranded-post sweeper only picks up documents SAP never saw.

Every test here is really the same question asked from a different angle:
could this sweep ever repost something that already exists in SAP? A duplicate
incoming payment is the worst outcome in this module — worse than the stranded
document it fixes — so the selection rule is tested far harder than the happy
path.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from users.models import User, UserRole

from .models import (FlowStatus, PaymentMethodEntry, PaymentReceipt,
                     PaymentReceiptFlow, SapCallLog)
from .sap_recovery import find_stranded
from .tests_support import uniq
from .tests_workflow_fixtures import payments_workflow


class FindStrandedTests(TestCase):
    """What counts as "the SAP call was never made".

    The signature changed with the final-stage lifecycle. The flow no longer
    completes on approval, so "flow APPROVED, document not POSTED" cannot
    happen any more; what marks a document as owing a post is POSTING_TO_SAP,
    which the final approval commits before the call is made precisely so the
    intent outlives a lost `on_commit` callback.

    That also means the marker is set while the call is legitimately in
    flight, so age is now part of the test rather than an optimisation: a
    recent POSTING_TO_SAP is a posting in progress, an old one is a lost one.
    """

    def setUp(self):
        role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        self.user = User.objects.create(
            username=uniq('rec_user-'), name='Rec', role=role)
        self.approver = User.objects.create(
            username=uniq('rec_approver-'), name='Approver', role=role)
        self.workflow, self.stages = payments_workflow(
            self.approver, company='OIL', code='PAY_REC',
            documents='receipts')
        self.ct = ContentType.objects.get_for_model(PaymentReceipt)

    def _receipt(self, no, status=PaymentReceipt.Status.POSTING_TO_SAP,
                 doc_entry=None, age=timedelta(hours=2)):
        receipt = PaymentReceipt.objects.create(
            receipt_no=no, company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1, status=status,
            sap_doc_entry=doc_entry,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.user)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        self._age(receipt, age)
        return receipt

    def _age(self, receipt, age):
        """Backdate `updated_at`, which `auto_now` will not let us set."""
        PaymentReceipt.objects.filter(pk=receipt.pk).update(
            updated_at=timezone.now() - age)
        receipt.refresh_from_db()
        return receipt

    def _flow(self, receipt, status=FlowStatus.PENDING, at_final=True):
        """The receipt's flow, parked at its final stage by default."""
        return PaymentReceiptFlow.objects.create(
            receipt=receipt, workflow=self.workflow, status=status,
            current_stage=self.stages[-1] if at_final else None,
            total_stage=len(self.stages))

    def _call_log(self, receipt, status=SapCallLog.Status.FAILED):
        return SapCallLog.objects.create(
            content_type=self.ct, object_id=receipt.pk, company_db='DB',
            endpoint='/IncomingPayments', status=status)

    # -- the case this exists for ------------------------------------------

    def test_finds_a_receipt_owed_a_sap_post_that_never_happened(self):
        receipt = self._receipt('RC-REC-0001')
        self._flow(receipt)
        self.assertEqual(
            [r.pk for r in find_stranded(PaymentReceipt)], [receipt.pk])

    def test_the_flow_state_is_not_what_decides_it(self):
        """The document's own status is the marker, not the flow's.

        The flow is PENDING at the final stage for a stranded document — that
        is the whole point of the new lifecycle — so a sweep keyed on the flow
        would find nothing at all.
        """
        receipt = self._receipt('RC-REC-0001B')
        flow = self._flow(receipt)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertEqual(
            [r.pk for r in find_stranded(PaymentReceipt)], [receipt.pk])

    # -- everything that must NOT be swept ---------------------------------

    def test_ignores_a_posting_still_in_flight(self):
        """THE RACE THE AGE THRESHOLD EXISTS FOR.

        A posting that started seconds ago looks exactly like a lost one —
        POSTING_TO_SAP, no call log yet — because the log row is written after
        the status. Sweeping it would post the payment twice.
        """
        receipt = self._receipt('RC-REC-0009', age=timedelta(seconds=5))
        self._flow(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_receipt_with_a_sap_call_log(self):
        """The decisive guard: a log row proves a call was made."""
        receipt = self._receipt('RC-REC-0002')
        self._flow(receipt)
        self._call_log(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_receipt_still_awaiting_approval(self):
        """No final approval was given, so no post is owed."""
        receipt = self._receipt('RC-REC-0003',
                                status=PaymentReceipt.Status.PENDING_APPROVAL)
        self._flow(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_posted_receipt(self):
        receipt = self._receipt('RC-REC-0005',
                                status=PaymentReceipt.Status.POSTED,
                                doc_entry=999)
        self._flow(receipt, status=FlowStatus.APPROVED, at_final=False)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_receipt_with_a_doc_entry(self):
        """Belt and braces: a DocEntry means SAP has it, whatever the status."""
        receipt = self._receipt('RC-REC-0006', doc_entry=12345)
        self._flow(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_sap_unknown_receipt(self):
        """The dangerous one: SAP may hold it, so it must never be reposted."""
        receipt = self._receipt('RC-REC-0007',
                                status=PaymentReceipt.Status.SAP_UNKNOWN)
        self._flow(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_pending_error_receipt(self):
        """SAP said no. That document is with its approver, not with us."""
        receipt = self._receipt('RC-REC-0008',
                                status=PaymentReceipt.Status.PENDING_ERROR)
        self._flow(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    # -- filters ------------------------------------------------------------

    def test_company_filter(self):
        receipt = self._receipt('RC-REC-0010')
        self._flow(receipt)
        self.assertEqual(find_stranded(PaymentReceipt, company='OIL'),
                         [receipt])
        self.assertEqual(find_stranded(PaymentReceipt, company='MART'), [])

    def test_min_age_is_configurable(self):
        receipt = self._receipt('RC-REC-0011', age=timedelta(minutes=2))
        self._flow(receipt)

        # Membership, not list equality. These tests run against the SHARED
        # TEST database, so a genuinely stranded document created by someone
        # using the app is visible here too and would fail an exact-list
        # assertion — which is what happened when RCP-OIL-20260919-000003 was
        # stranded by a restart. The question this test asks is about THIS
        # receipt; what else the sweep finds is not its business.
        self.assertNotIn(receipt, find_stranded(PaymentReceipt))
        self.assertIn(
            receipt,
            find_stranded(PaymentReceipt, min_age=timedelta(minutes=1)))

