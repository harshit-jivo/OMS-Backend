"""Only the stage that now holds a payment is told about it.

Drives the REAL path — `payments.workflow_flow.approve/reject` over the
Workflow Engine — so the sequencing proved here is the sequencing production
runs. Each advance notifies exactly one person: the stage's EFFECTIVE user,
resolved at send time, never a copy stored when the document was submitted.

Rewritten from the old three-LEVEL version. The engine has one user per stage
and no quorum, so "several approvers on one rung, any of whom may clear it" no
longer exists; what replaced it — a temporary replacement standing in for the
configured user — is tested instead.

PostgreSQL only: the engine's schema and query validation need it.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from notifications.models import Notification
from payments import workflow_flow
from payments.models import PaymentReceipt
from payments.notification_events import (PAYMENT_APPROVED, PAYMENT_REJECTED,
                                          PAYMENT_SUBMITTED)
from payments.tests_workflow_fixtures import payments_workflow


class PaymentStageSequencingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company = Company.objects.create(name='Lvl Co')
        cls.creator = User.objects.create(
            username='lv_cre', name='Creator', company=cls.company)
        # Being the stage's user is necessary but NOT sufficient: the approve
        # key is checked too (payments.permissions.may_act_on).
        grant = ['Payments_Approve', 'Deposit_Approve']
        cls.a1 = User.objects.create(username='lv_a1', name='Appr S1',
                                     company=cls.company, extra_pages=grant)
        cls.a2 = User.objects.create(username='lv_a2', name='Appr S2',
                                     company=cls.company, extra_pages=grant)
        cls.a3 = User.objects.create(username='lv_a3', name='Appr S3',
                                     company=cls.company, extra_pages=grant)

    def _receipt(self, no='RC-LVL'):
        return PaymentReceipt.objects.create(
            receipt_no=no, company='OIL', card_code='C1',
            payment_date=date.today(), total_amount=Decimal('100'),
            created_by=self.creator)

    def _submit(self, no='RC-LVL'):
        """A receipt routed through the engine and waiting at stage 1."""
        receipt = self._receipt(no)
        flow = workflow_flow.start(receipt, user=self.creator)
        return receipt, flow

    def _submitted_ids(self, entity_id):
        return set(Notification.objects
                   .filter(event_type=PAYMENT_SUBMITTED, object_id=entity_id)
                   .values_list('user_id', flat=True))

    def _recips(self, event_type, entity_id):
        return set(Notification.objects
                   .filter(event_type=event_type, object_id=entity_id)
                   .values_list('user_id', flat=True))

    def test_full_three_stage_sequence(self):
        payments_workflow(self.a1, self.a2, self.a3, documents='receipts')
        receipt, flow = self._submit()

        # 1) Submit -> ONLY stage 1. The others and the creator get nothing.
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id})

        # 2) Stage 1 approves -> stage 2 is told, and only stage 2.
        workflow_flow.approve(flow, user=self.a1)
        self.assertEqual(self._submitted_ids(receipt.id),
                         {self.a1.id, self.a2.id})
        self.assertNotIn(self.a3.id, self._submitted_ids(receipt.id))

        # 3) Stage 2 approves -> stage 3 is told.
        flow.refresh_from_db()
        workflow_flow.approve(flow, user=self.a2)
        self.assertEqual(self._submitted_ids(receipt.id),
                         {self.a1.id, self.a2.id, self.a3.id})
        # Nothing final yet.
        self.assertEqual(self._recips(PAYMENT_APPROVED, receipt.id), set())

        # 4) The last stage completes it -> ONLY the creator is told.
        flow.refresh_from_db()
        workflow_flow.approve(flow, user=self.a3)
        self.assertEqual(self._recips(PAYMENT_APPROVED, receipt.id),
                         {self.creator.id})

        # The creator never got an "approval required" ping…
        self.assertNotIn(self.creator.id, self._submitted_ids(receipt.id))
        # …and nobody was pinged twice.
        sent = list(Notification.objects
                    .filter(event_type=PAYMENT_SUBMITTED, object_id=receipt.id)
                    .values_list('user_id', flat=True))
        self.assertEqual(len(sent), len(set(sent)))

    def test_reject_at_stage_1_notifies_the_creator_and_nobody_downstream(self):
        payments_workflow(self.a1, self.a2, self.a3, documents='receipts')
        receipt, flow = self._submit('RC-REJ1')

        workflow_flow.reject(flow, user=self.a1, remarks='No good')

        self.assertEqual(self._recips(PAYMENT_REJECTED, receipt.id),
                         {self.creator.id})
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id})

    def test_reject_at_stage_2_does_not_reach_stage_3(self):
        payments_workflow(self.a1, self.a2, self.a3, documents='receipts')
        receipt, flow = self._submit('RC-REJ2')

        workflow_flow.approve(flow, user=self.a1)
        flow.refresh_from_db()
        workflow_flow.reject(flow, user=self.a2, remarks='Stop')

        self.assertEqual(self._recips(PAYMENT_REJECTED, receipt.id),
                         {self.creator.id})
        self.assertNotIn(self.a3.id, self._submitted_ids(receipt.id))

    def test_a_replacement_receives_the_ping_instead_of_the_stage_user(self):
        """What replaced "several approvers on one rung".

        The stage keeps its configured user; only the effective actor changes,
        and the notification follows the effective one.
        """
        from workflow.models import WorkflowUserReplacement

        payments_workflow(self.a1, self.a2, documents='receipts')
        today = date.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.a1, new_user=self.a3,
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=1))

        receipt, _flow = self._submit('RC-REPL')

        self.assertEqual(self._submitted_ids(receipt.id), {self.a3.id})
        self.assertNotIn(self.a1.id, self._submitted_ids(receipt.id))

    def test_delivery_only_after_commit(self):
        """The provider fan-out runs on transaction.on_commit, not inline."""
        from notifications.providers import mobile

        payments_workflow(self.a1, self.a2, documents='receipts')
        with mock.patch.object(mobile.MobileProvider, 'get_tokens',
                               return_value=['ExponentPushToken[x]']), \
                mock.patch.object(mobile, 'requests') as m_req:
            m_req.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self._submit('RC-COMMIT')
                # Records exist, but delivery has NOT run yet.
                self.assertFalse(m_req.post.called)
            for callback in callbacks:
                callback()
            self.assertTrue(m_req.post.called)
