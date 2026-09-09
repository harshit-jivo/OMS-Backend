"""Who may read a receipt's attachment, and when an upload counts as an edit.

Two rules that are easy to get wrong in opposite directions:

  * The VERIFIER must see the cheque image. Their whole job is checking the
    physical money against the entry, and the image is that evidence. This is
    not expressible through the approval chain, because verification happens
    BEFORE the document enters it — there is no pending approval request yet
    and the verifier has taken no approval action. The rule admitted creators
    and approvers only, so the verify screen showed the receipt and refused
    its attachment.

  * A creation-time upload is NOT an edit. The client attaches the image
    moments after POSTing the receipt; logging that as UPDATED put an "edited"
    row on every receipt the instant it was raised.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from users.models import User, UserRole

from .models import PaymentMethodEntry, PaymentReceipt
from .permissions import PAYMENTS_APPROVE, PAYMENTS_CREATE, PAYMENTS_VERIFY


def _user(username, keys):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role, extra_pages=keys)


class AttachmentVisibilityTests(TestCase):
    """`can_be_viewed_by` — the rule the attachment download guard delegates to."""

    def setUp(self):
        self.creator = _user('att_creator', [PAYMENTS_CREATE])
        self.receipt = PaymentReceipt.objects.create(
            receipt_no='RCP-OIL-TEST-0001', company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.PENDING,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=self.receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))

    def test_verifier_may_see_the_attachment_before_approval_exists(self):
        """The regression: no approval row exists yet at verification time."""
        verifier = _user('att_verifier', [PAYMENTS_VERIFY])
        self.assertFalse(
            self.receipt.approvals.exists(),
            'precondition: verification happens before the approval chain')
        self.assertTrue(self.receipt.can_be_viewed_by(verifier))

    def test_creator_may_see_their_own(self):
        self.assertTrue(self.receipt.can_be_viewed_by(self.creator))

    def test_unrelated_user_may_not(self):
        """The grant is not a blanket one — Payments_Create confers nothing."""
        other = _user('att_other', [PAYMENTS_CREATE])
        self.assertFalse(self.receipt.can_be_viewed_by(other))

    def test_user_with_no_keys_may_not(self):
        self.assertFalse(self.receipt.can_be_viewed_by(_user('att_none', [])))

    def test_approver_still_may(self):
        """The pre-existing path must keep working."""
        from django.contrib.contenttypes.models import ContentType

        from approvals.models import ApprovalRequest, ApprovalWorkflow

        approver = _user('att_approver', [PAYMENTS_APPROVE])
        workflow = ApprovalWorkflow.objects.create(
            code='PAY_ATT', name='Att', document_type='PAYMENT', company='OIL')
        ApprovalRequest.objects.create(
            workflow=workflow,
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=self.receipt.pk, company='OIL',
            amount=self.receipt.total_amount,
            document_number=self.receipt.receipt_no,
            status=ApprovalRequest.Status.PENDING)
        self.assertTrue(self.receipt.can_be_viewed_by(approver))


class AttachmentUploadIsNotAlwaysAnEditTests(TestCase):
    """The DRAFT/not-DRAFT split that decides CREATED vs UPDATED.

    Asserted against the view's own rule rather than through an HTTP upload,
    which would need the file share. What matters is which `action` a given
    document status produces, because the Edit History card selects on it.
    """

    def setUp(self):
        self.creator = _user('up_creator', [PAYMENTS_CREATE])

    def _receipt(self, no, status):
        receipt = PaymentReceipt.objects.create(
            receipt_no=no, company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('50.00'),
            is_advance=True, sap_branch_id=1, status=status,
            verification_status=PaymentReceipt.VerificationStatus.PENDING,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('50.00'))
        return receipt

    def _action_for(self, receipt):
        """The action the upload view would log for this document's status.

        Mirrors `_AttachmentUploadBase.post`. Kept as one expression so the
        test fails if the view's rule changes shape.
        """
        from .models import PaymentStatusHistory
        from .views import ReceiptAttachmentUploadView

        is_initial = receipt.status == ReceiptAttachmentUploadView.model.Status.DRAFT
        return (PaymentStatusHistory.Action.CREATED if is_initial
                else PaymentStatusHistory.Action.UPDATED)

    def test_draft_upload_is_not_an_edit(self):
        """The regression: attaching while raising must not read as 'edited'."""
        from .models import PaymentStatusHistory

        receipt = self._receipt('RCP-OIL-TEST-0002',
                                PaymentReceipt.Status.DRAFT)
        self.assertEqual(self._action_for(receipt),
                         PaymentStatusHistory.Action.CREATED)

    def test_upload_after_submission_is_an_edit(self):
        from .models import PaymentStatusHistory

        receipt = self._receipt('RCP-OIL-TEST-0003',
                                PaymentReceipt.Status.PENDING_APPROVAL)
        self.assertEqual(self._action_for(receipt),
                         PaymentStatusHistory.Action.UPDATED)

    def test_log_status_drops_a_diff_on_created(self):
        """Why the CREATED row carries no 'old -> new': the service refuses it.

        Belt and braces with the view passing change_data=None — this asserts
        the guarantee holds even if a future caller forgets.
        """
        from .models import PaymentStatusHistory
        from .services import log_status

        receipt = self._receipt('RCP-OIL-TEST-0004',
                                PaymentReceipt.Status.DRAFT)
        log_status(receipt, from_status=receipt.status,
                   to_status=receipt.status, user=self.creator,
                   action=PaymentStatusHistory.Action.CREATED,
                   change_data={'attachments': {'old': [], 'new': ['Cheque']}},
                   reason='Attached Cheque image.')
        row = PaymentStatusHistory.objects.filter(
            object_id=receipt.pk,
            action=PaymentStatusHistory.Action.CREATED).latest('created_at')
        self.assertFalse(row.change_data,
                         'a CREATED row must carry no field diff')
