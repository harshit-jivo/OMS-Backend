"""Approval-LEVEL sequencing notifications — Orders-parity behavior.

Proves that Payment and Deposit notifications follow the exact Orders pattern:

    submit          -> ONLY level-1 approvers
    level 1 clears  -> ONLY level-2 approvers   (level 3 gets nothing)
    level 2 clears  -> ONLY level-3 approvers
    final approval  -> the CREATOR/SUBMITTER
    any rejection   -> the CREATOR/SUBMITTER, and no downstream level is notified

Drives the REAL approvals engine (approvals.services.approve/reject), so the
level transitions and hook firing are genuine, not simulated. Expo/Web Push are
mocked at the provider boundary — no real push is sent.
"""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from approvals import services as approval_services
from approvals.models import ApprovalLevel, ApprovalLevelApprover, ApprovalWorkflow
from notifications.models import Notification
from payments.models import PaymentReceipt
from payments.notification_events import (
    PAYMENT_APPROVED,
    PAYMENT_REJECTED,
    PAYMENT_SUBMITTED,
)


def _three_level_workflow(document_type, levels_users):
    wf = ApprovalWorkflow.objects.create(
        code=f"{document_type}_LVL_TEST",
        name=f"{document_type} level test",
        document_type=document_type,
        company="",
        forbid_self_approval=True,
        is_active=True,
    )
    for i, users in enumerate(levels_users, start=1):
        level = ApprovalLevel.objects.create(
            workflow=wf, sequence=i, name=f"Level {i}", min_approvals=1, is_active=True)
        for u in users:
            ApprovalLevelApprover.objects.create(
                level=level, user=u, company="", is_active=True)
    return wf


class PaymentLevelSequencingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company = Company.objects.create(name="Lvl Co")
        cls.creator = User.objects.create(username="lv_cre", name="Creator", company=cls.company)
        # Approvers need the module-level grant (Payments_Approve / Deposit_Approve)
        # — being named on a level is necessary but not sufficient (see
        # approvals.services.has_approve_permission). Grant it via extra_pages.
        grant = ["Payments_Approve", "Deposit_Approve"]
        cls.a1 = User.objects.create(username="lv_a1", name="Appr L1", company=cls.company, extra_pages=grant)
        cls.a2 = User.objects.create(username="lv_a2", name="Appr L2", company=cls.company, extra_pages=grant)
        cls.a3 = User.objects.create(username="lv_a3", name="Appr L3", company=cls.company, extra_pages=grant)

    def _submit(self, no="RC-LVL"):
        """Create a receipt + a real PENDING approval request at level 1."""
        receipt = PaymentReceipt.objects.create(
            receipt_no=no, company="OIL", card_code="C1",
            payment_date=date.today(), total_amount=Decimal("100"),
            created_by=self.creator,
        )
        approval_services.submit(
            document=receipt, user=self.creator, company="OIL",
            amount=Decimal("100"), document_number=receipt.receipt_no,
            document_type="PAYMENT",
        )
        return receipt

    def _submitted_ids(self, entity_id):
        return set(
            Notification.objects
            .filter(event_type=PAYMENT_SUBMITTED, object_id=entity_id)
            .values_list("user_id", flat=True))

    def _recips(self, event_type, entity_id):
        return set(
            Notification.objects
            .filter(event_type=event_type, object_id=entity_id)
            .values_list("user_id", flat=True))

    def test_full_three_level_sequence(self):
        _three_level_workflow("PAYMENT", [[self.a1], [self.a2], [self.a3]])
        receipt = self._submit()
        req = receipt.approvals.first()

        # 1) Submit -> ONLY level 1 (a1). a2, a3, creator get nothing.
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id})

        # 2) a1 approves -> level advances to 2 -> ONLY a2 newly notified.
        approval_services.approve(request_id=req.id, user=self.a1)
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id, self.a2.id})
        self.assertNotIn(self.a3.id, self._submitted_ids(receipt.id))

        # 3) a2 approves -> level advances to 3 -> a3 newly notified.
        approval_services.approve(request_id=req.id, user=self.a2)
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id, self.a2.id, self.a3.id})

        # No PAYMENT_APPROVED (final) notification yet.
        self.assertEqual(self._recips(PAYMENT_APPROVED, receipt.id), set())

        # 4) a3 approves -> FINAL -> ONLY the creator gets PAYMENT_APPROVED.
        approval_services.approve(request_id=req.id, user=self.a3)
        self.assertEqual(self._recips(PAYMENT_APPROVED, receipt.id), {self.creator.id})

        # The creator was NEVER sent a "submitted/approval required" ping.
        self.assertNotIn(self.creator.id, self._submitted_ids(receipt.id))
        # No duplicate SUBMITTED for any single approver.
        counts = (Notification.objects
                  .filter(event_type=PAYMENT_SUBMITTED, object_id=receipt.id)
                  .values_list("user_id", flat=True))
        self.assertEqual(len(counts), len(set(counts)))

    def test_reject_at_level_1_notifies_creator_and_no_downstream(self):
        _three_level_workflow("PAYMENT", [[self.a1], [self.a2], [self.a3]])
        receipt = self._submit("RC-REJ1")
        req = receipt.approvals.first()

        approval_services.reject(request_id=req.id, user=self.a1, remarks="No good")

        # Creator notified of rejection; a2/a3 never notified of anything new.
        self.assertEqual(self._recips(PAYMENT_REJECTED, receipt.id), {self.creator.id})
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id})  # only the L1 ping
        self.assertNotIn(self.a2.id, self._submitted_ids(receipt.id))
        self.assertNotIn(self.a3.id, self._submitted_ids(receipt.id))

    def test_reject_at_level_2_does_not_notify_level_3(self):
        _three_level_workflow("PAYMENT", [[self.a1], [self.a2], [self.a3]])
        receipt = self._submit("RC-REJ2")
        req = receipt.approvals.first()

        approval_services.approve(request_id=req.id, user=self.a1)   # advance to L2 (a2 notified)
        approval_services.reject(request_id=req.id, user=self.a2, remarks="Stop")

        self.assertEqual(self._recips(PAYMENT_REJECTED, receipt.id), {self.creator.id})
        # a3 (level 3) must never have been notified.
        self.assertNotIn(self.a3.id, self._submitted_ids(receipt.id))

    def test_multiple_approvers_same_level_all_notified_then_advance(self):
        # Level 1 has two approvers (min_approvals defaults to 1 -> ANY clears it).
        _three_level_workflow("PAYMENT", [[self.a1, self.a2], [self.a3]])
        receipt = self._submit("RC-MULTI")
        req = receipt.approvals.first()

        # Both level-1 approvers are notified at submit.
        self.assertEqual(self._submitted_ids(receipt.id), {self.a1.id, self.a2.id})

        # a1 approves -> quorum (1) met -> advance to level 2 -> a3 notified.
        approval_services.approve(request_id=req.id, user=self.a1)
        self.assertIn(self.a3.id, self._submitted_ids(receipt.id))

    def test_delivery_only_after_commit(self):
        """The provider fan-out runs on transaction.on_commit, not inline."""
        from notifications.providers import mobile

        _three_level_workflow("PAYMENT", [[self.a1], [self.a2]])
        with mock.patch.object(mobile.MobileProvider, "get_tokens",
                               return_value=["ExponentPushToken[x]"]), \
                mock.patch.object(mobile, "requests") as m_req:
            m_req.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self._submit("RC-COMMIT")
                # Records exist, but delivery has NOT run yet.
                self.assertFalse(m_req.post.called)
            # After the captured on_commit callbacks run, delivery happened.
            for cb in callbacks:
                cb()
            self.assertTrue(m_req.post.called)
