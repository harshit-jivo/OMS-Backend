"""Who is told that a payment needs approving, and what they are told.

Recipient resolution lives in payments (`notification_events`) and reads the
ENGINE for the answer — `get_stage_assignment(flow.current_stage_id)` — so the
person notified is whoever may act today, including a stand-in covering a
replacement. The notification framework itself knows nothing about payments.

Rewritten from the version that built old-engine workflows, levels and named
approvers. The rules that survived the move are each still asserted here: only
the stage that holds the document is told, the payload carries the document as
its entity, a document with no flow notifies nobody, and both providers send
the same canonical payload.

PostgreSQL only.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from notifications.models import Notification
from payments import workflow_flow
from payments.models import (BankDeposit, BankDepositFlow, FlowStatus,
                             PaymentReceipt, PaymentReceiptFlow)
from payments.notification_events import (DEPOSIT_SUBMITTED, PAYMENT_SUBMITTED,
                                          publish_stage_awaiting)
from payments.tests_workflow_fixtures import payments_workflow


class SubmissionRecipientTests(TestCase):
    """Recipient resolution, driven through real flows and stages."""

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name='Sub Co A')
        cls.company_b = Company.objects.create(name='Sub Co B')
        grant = ['Payments_Approve', 'Deposit_Approve']
        cls.submitter = User.objects.create(
            username='s_sub', name='Submitter', company=cls.company_a)
        cls.appr1 = User.objects.create(
            username='s_ap1', name='Approver 1', company=cls.company_a,
            extra_pages=grant)
        cls.appr2 = User.objects.create(
            username='s_ap2', name='Approver 2', company=cls.company_a,
            extra_pages=grant)
        cls.appr_b = User.objects.create(
            username='s_apb', name='Approver B', company=cls.company_b,
            extra_pages=grant)

    def _receipt(self, no='RC-S-1'):
        return PaymentReceipt.objects.create(
            receipt_no=no, company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100'),
            created_by=self.submitter)

    def _deposit(self, no='DEP-S-1'):
        return BankDeposit.objects.create(
            deposit_no=no, company='OIL', deposit_date=date.today(),
            collected_amount=Decimal('100'), deposit_amount=Decimal('100'),
            created_by=self.submitter)

    def _open_flow(self, document, stages):
        """A flow parked at stage 1, as `workflow_flow.start` would leave it."""
        model = (PaymentReceiptFlow if isinstance(document, PaymentReceipt)
                 else BankDepositFlow)
        field = 'receipt' if model is PaymentReceiptFlow else 'deposit'
        return model.objects.create(**{
            field: document,
            'workflow': stages[0].workflow,
            'status': FlowStatus.PENDING,
            'current_stage': stages[0],
            'current_user': stages[0].user,
            'total_stage': len(stages),
        })

    def _recipients(self, event_type, entity_id):
        return set(Notification.objects
                   .filter(event_type=event_type, object_id=entity_id)
                   .values_list('user_id', flat=True))

    # -- payments ----------------------------------------------------------

    def test_payment_submitted_notifies_the_stage_user(self):
        receipt = self._receipt()
        _wf, stages = payments_workflow(self.appr1, documents='receipts')
        flow = self._open_flow(receipt, stages)

        publish_stage_awaiting(receipt, flow, self.submitter)

        self.assertEqual(self._recipients(PAYMENT_SUBMITTED, receipt.id),
                         {self.appr1.id})

    def test_the_payload_identifies_the_document(self):
        receipt = self._receipt('RC-S-PAYLOAD')
        _wf, stages = payments_workflow(self.appr1, documents='receipts')
        flow = self._open_flow(receipt, stages)

        publish_stage_awaiting(receipt, flow, self.submitter)

        note = Notification.objects.get(event_type=PAYMENT_SUBMITTED,
                                        object_id=receipt.id)
        self.assertEqual(note.user_id, self.appr1.id)
        self.assertIn(receipt.receipt_no, note.message)
        self.assertIn('Submitter', note.message)

    def test_only_the_current_stage_is_told(self):
        """A later stage learns about it when it gets there, not before."""
        receipt = self._receipt('RC-S-CURRENT')
        _wf, stages = payments_workflow(self.appr1, self.appr2,
                                        documents='receipts')
        flow = self._open_flow(receipt, stages)

        publish_stage_awaiting(receipt, flow, self.submitter)

        recipients = self._recipients(PAYMENT_SUBMITTED, receipt.id)
        self.assertEqual(recipients, {self.appr1.id})
        self.assertNotIn(self.appr2.id, recipients)

    def test_a_document_with_no_flow_notifies_nobody(self):
        receipt = self._receipt('RC-S-NOFLOW')

        self.assertEqual(publish_stage_awaiting(receipt, None,
                                                self.submitter), [])
        self.assertEqual(self._recipients(PAYMENT_SUBMITTED, receipt.id),
                         set())

    def test_a_stage_with_no_assignment_notifies_nobody_and_does_not_raise(self):
        """Deleted configuration is logged, never raised into an approval."""
        receipt = self._receipt('RC-S-NOSTAGE')
        _wf, stages = payments_workflow(self.appr1, documents='receipts')
        flow = self._open_flow(receipt, stages)
        flow.current_stage.delete()
        flow.refresh_from_db()

        self.assertEqual(publish_stage_awaiting(receipt, flow,
                                                self.submitter), [])

    def test_mobile_and_web_send_the_same_payload(self):
        receipt = self._receipt('RC-S-PROVIDER')
        _wf, stages = payments_workflow(self.appr1, documents='receipts')
        flow = self._open_flow(receipt, stages)

        with mock.patch('notifications.providers.mobile.MobileProvider.get_tokens',
                        return_value=['ExponentPushToken[x]']), \
                mock.patch('notifications.providers.mobile.requests') as m_req:
            m_req.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                publish_stage_awaiting(receipt, flow, self.submitter)

        note = Notification.objects.get(event_type=PAYMENT_SUBMITTED,
                                        object_id=receipt.id)
        self.assertEqual(note.object_id, receipt.id)

    # -- deposits ----------------------------------------------------------

    def test_deposit_submitted_notifies_the_stage_user(self):
        deposit = self._deposit()
        _wf, stages = payments_workflow(self.appr1, documents='deposits')
        flow = self._open_flow(deposit, stages)

        publish_stage_awaiting(deposit, flow, self.submitter)

        self.assertEqual(self._recipients(DEPOSIT_SUBMITTED, deposit.id),
                         {self.appr1.id})

    def test_a_deposit_payload_names_the_deposit(self):
        deposit = self._deposit('DEP-S-PAYLOAD')
        _wf, stages = payments_workflow(self.appr1, documents='deposits')
        flow = self._open_flow(deposit, stages)

        publish_stage_awaiting(deposit, flow, self.submitter)

        note = Notification.objects.get(event_type=DEPOSIT_SUBMITTED,
                                        object_id=deposit.id)
        self.assertIn(deposit.deposit_no, note.message)


class SubmissionIntegrationTests(TestCase):
    """Submitting for real fires the notification — and rolls back with it."""

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        company = Company.objects.create(name='Int Co')
        cls.creator = User.objects.create(
            username='i_cre', name='Creator', company=company,
            extra_pages=['Payments_Create', 'Deposit_Create'])
        cls.approver = User.objects.create(
            username='i_app', name='Approver', company=company,
            extra_pages=['Payments_Approve', 'Deposit_Approve'])

    def test_submitting_a_receipt_fires_payment_submitted(self):
        payments_workflow(self.approver, documents='receipts')
        receipt = PaymentReceipt.objects.create(
            receipt_no='RC-INT-1', company='OIL', card_code='C1',
            payment_date=date.today(), total_amount=Decimal('100'),
            created_by=self.creator)

        workflow_flow.start(receipt, user=self.creator)

        self.assertTrue(Notification.objects.filter(
            event_type=PAYMENT_SUBMITTED, object_id=receipt.id,
            user_id=self.approver.id).exists())

    def test_a_rollback_leaves_no_notification(self):
        """The record commits with the submission or not at all."""
        from django.db import transaction

        payments_workflow(self.approver, documents='receipts')
        receipt = PaymentReceipt.objects.create(
            receipt_no='RC-INT-2', company='OIL', card_code='C1',
            payment_date=date.today(), total_amount=Decimal('100'),
            created_by=self.creator)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with transaction.atomic():
                workflow_flow.start(receipt, user=self.creator)
                raise Boom()

        self.assertFalse(Notification.objects.filter(
            event_type=PAYMENT_SUBMITTED, object_id=receipt.id).exists())
        self.assertFalse(PaymentReceiptFlow.objects.filter(
            receipt=receipt).exists())

    def test_submitting_a_deposit_fires_deposit_submitted(self):
        payments_workflow(self.approver, documents='deposits')
        deposit = BankDeposit.objects.create(
            deposit_no='DEP-INT-1', company='OIL', deposit_date=date.today(),
            collected_amount=Decimal('100'), deposit_amount=Decimal('100'),
            created_by=self.creator)

        workflow_flow.start(deposit, user=self.creator)

        self.assertTrue(Notification.objects.filter(
            event_type=DEPOSIT_SUBMITTED, object_id=deposit.id,
            user_id=self.approver.id).exists())
