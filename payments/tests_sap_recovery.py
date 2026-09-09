"""The stranded-post sweeper only picks up documents SAP never saw.

Every test here is really the same question asked from a different angle:
could this sweep ever repost something that already exists in SAP? A duplicate
incoming payment is the worst outcome in this module — worse than the stranded
document it fixes — so the selection rule is tested far harder than the happy
path.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from approvals.models import ApprovalRequest, ApprovalWorkflow
from users.models import User, UserRole

from .models import PaymentMethodEntry, PaymentReceipt, SapCallLog
from .sap_recovery import find_stranded


class FindStrandedTests(TestCase):
    def setUp(self):
        role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        self.user = User.objects.create(
            username='rec_user', name='Rec', role=role)
        self.workflow = ApprovalWorkflow.objects.create(
            code='PAY_REC', name='Rec', document_type='PAYMENT', company='OIL')
        self.ct = ContentType.objects.get_for_model(PaymentReceipt)

    def _receipt(self, no, status=PaymentReceipt.Status.PENDING_APPROVAL,
                 doc_entry=None):
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
        return receipt

    def _approve(self, receipt, status=ApprovalRequest.Status.APPROVED):
        return ApprovalRequest.objects.create(
            workflow=self.workflow, content_type=self.ct,
            object_id=receipt.pk, company='OIL',
            amount=receipt.total_amount, document_number=receipt.receipt_no,
            status=status)

    def _call_log(self, receipt, status=SapCallLog.Status.FAILED):
        return SapCallLog.objects.create(
            content_type=self.ct, object_id=receipt.pk, company_db='DB',
            endpoint='/IncomingPayments', status=status)

    # -- the case this exists for ------------------------------------------

    def test_finds_an_approved_receipt_that_never_reached_sap(self):
        receipt = self._receipt('RC-REC-0001')
        self._approve(receipt)
        self.assertEqual(
            [r.pk for r in find_stranded(PaymentReceipt)], [receipt.pk])

    # -- everything that must NOT be swept ---------------------------------

    def test_ignores_a_receipt_with_a_sap_call_log(self):
        """The decisive guard: a log row proves a call was made."""
        receipt = self._receipt('RC-REC-0002')
        self._approve(receipt)
        self._call_log(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_receipt_still_awaiting_approval(self):
        self._receipt('RC-REC-0003')
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_receipt_whose_approval_is_still_pending(self):
        receipt = self._receipt('RC-REC-0004')
        self._approve(receipt, status=ApprovalRequest.Status.PENDING)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_posted_receipt(self):
        receipt = self._receipt('RC-REC-0005',
                                status=PaymentReceipt.Status.POSTED,
                                doc_entry=999)
        self._approve(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_receipt_with_a_doc_entry(self):
        """Belt and braces: a DocEntry means SAP has it, whatever the status."""
        receipt = self._receipt('RC-REC-0006', doc_entry=12345)
        self._approve(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_sap_unknown_receipt(self):
        """The dangerous one: SAP may hold it, so it must never be reposted."""
        receipt = self._receipt('RC-REC-0007',
                                status=PaymentReceipt.Status.SAP_UNKNOWN)
        self._approve(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_pending_error_receipt(self):
        """SAP said no; that flow reopens the approval and is not ours."""
        receipt = self._receipt('RC-REC-0008',
                                status=PaymentReceipt.Status.PENDING_ERROR)
        self._approve(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    def test_ignores_a_posting_in_flight(self):
        """POSTING_TO_SAP means a call may be mid-air and may yet commit."""
        receipt = self._receipt('RC-REC-0009',
                                status=PaymentReceipt.Status.POSTING_TO_SAP)
        self._approve(receipt)
        self.assertEqual(find_stranded(PaymentReceipt), [])

    # -- filters ------------------------------------------------------------

    def test_company_filter(self):
        receipt = self._receipt('RC-REC-0010')
        self._approve(receipt)
        self.assertEqual(find_stranded(PaymentReceipt, company='OIL'),
                         [receipt])
        self.assertEqual(find_stranded(PaymentReceipt, company='MART'), [])
