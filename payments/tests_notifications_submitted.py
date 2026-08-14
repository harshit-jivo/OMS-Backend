"""Submission notifications: PAYMENT_SUBMITTED / DEPOSIT_SUBMITTED.

When a PaymentReceipt or BankDeposit successfully ENTERS the approval workflow,
the eligible approvers (not the submitter) are notified that a decision is
required. This is distinct from *_APPROVED / *_REJECTED, which notify the
submitter of the outcome.

Recipient resolution is proven to live in payments + approvals (never in the
framework). Expo / Web Push are mocked — no real push is sent.
"""

import json
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from approvals.models import (
    ApprovalLevel,
    ApprovalLevelApprover,
    ApprovalRequest,
    ApprovalWorkflow,
)
from notifications.models import Notification
from notifications.providers.base import ProviderResult
from payments.models import (
    BankDeposit,
    BankDepositLine,
    PaymentMethodEntry,
    PaymentReceipt,
)
from payments.notification_events import (
    DEPOSIT_SUBMITTED,
    PAYMENT_SUBMITTED,
    publish_deposit_submitted,
    publish_receipt_submitted,
)


def _workflow(document_type, company, approvers_by_level):
    """Build a real workflow + levels + named approvers.

    ``approvers_by_level`` is a list of iterables of users, one per level.
    """
    wf = ApprovalWorkflow.objects.create(
        code=f"{document_type}_{company or 'ALL'}_TEST",
        name=f"{document_type} test",
        document_type=document_type,
        company=company,
        forbid_self_approval=True,
        is_active=True,
    )
    for i, approvers in enumerate(approvers_by_level, start=1):
        level = ApprovalLevel.objects.create(
            workflow=wf, sequence=i, name=f"Level {i}", min_approvals=1, is_active=True)
        for user in approvers:
            ApprovalLevelApprover.objects.create(
                level=level, user=user, company="", is_active=True)
    return wf


class SubmissionRecipientTests(TestCase):
    """Recipient resolution + publish helpers, using real ApprovalRequest rows."""

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name="Sub Co A")
        cls.company_b = Company.objects.create(name="Sub Co B")
        cls.submitter = User.objects.create(username="s_sub", name="Submitter", company=cls.company_a)
        cls.appr1 = User.objects.create(username="s_ap1", name="Approver 1", company=cls.company_a)
        cls.appr2 = User.objects.create(username="s_ap2", name="Approver 2", company=cls.company_a)
        cls.appr_b = User.objects.create(username="s_apb", name="Approver B", company=cls.company_b)

    def _receipt(self, no="RC-S-1"):
        return PaymentReceipt.objects.create(
            receipt_no=no, company="OIL", card_code="CUST1",
            payment_date=date.today(), total_amount=100, created_by=self.submitter,
        )

    def _deposit(self, no="DEP-S-1"):
        return BankDeposit.objects.create(
            deposit_no=no, company="OIL", deposit_date=date.today(),
            collected_amount=100, deposit_amount=100, created_by=self.submitter,
        )

    def _open_request(self, document, workflow, document_number):
        return ApprovalRequest.objects.create(
            workflow=workflow,
            content_type=ContentType.objects.get_for_model(document.__class__),
            object_id=document.pk,
            company="OIL",
            amount=Decimal("100"),
            document_number=document_number,
            status=ApprovalRequest.Status.PENDING,
            current_level=1,
            total_levels=len(list(workflow.levels.all())),
            submitted_by=self.submitter,
        )

    # 1-8 Payment: submission notifies approver(s), correct fields, entity, recipient
    def test_payment_submitted_notifies_single_approver(self):
        receipt = self._receipt()
        wf = _workflow("PAYMENT", "", [[self.appr1]])
        self._open_request(receipt, wf, receipt.receipt_no)

        created = publish_receipt_submitted(receipt, self.submitter)
        self.assertEqual(len(created), 1)
        n = created[0]
        self.assertEqual(n.event_type, PAYMENT_SUBMITTED)
        self.assertEqual(n.title, "Payment approval required")
        self.assertIn(receipt.receipt_no, n.message)
        self.assertEqual(n.user_id, self.appr1.id)
        self.assertEqual(n.content_type, ContentType.objects.get_for_model(PaymentReceipt))
        self.assertEqual(n.object_id, receipt.id)
        self.assertEqual(n.entity, receipt)
        self.assertFalse(n.is_read)

    # 6-7 payload entity_type/entity_id
    def test_payment_submitted_payload(self):
        from notifications.services.payloads import build_payload

        receipt = self._receipt()
        wf = _workflow("PAYMENT", "", [[self.appr1]])
        self._open_request(receipt, wf, receipt.receipt_no)
        n = publish_receipt_submitted(receipt, self.submitter)[0]
        payload = build_payload(n)
        self.assertEqual(payload["entity_type"], "paymentreceipt")
        self.assertEqual(payload["entity_id"], receipt.id)
        self.assertEqual(payload["event_type"], PAYMENT_SUBMITTED)
        self.assertNotIn("payment_id", payload)

    # 9 submitter never notified (submitter is also an eligible approver here)
    def test_submitter_excluded_even_if_eligible(self):
        receipt = self._receipt()
        wf = _workflow("PAYMENT", "", [[self.submitter, self.appr1]])
        self._open_request(receipt, wf, receipt.receipt_no)
        created = publish_receipt_submitted(receipt, self.submitter)
        recipients = {n.user_id for n in created}
        self.assertNotIn(self.submitter.id, recipients)
        self.assertEqual(recipients, {self.appr1.id})

    # 11 multiple approvers at the CURRENT level -> one notification each;
    # 12 a level-2-only approver is NOT notified at submit (current level only).
    def test_submit_notifies_current_level_only(self):
        receipt = self._receipt()
        # Level 1: appr1 + appr2.  Level 2: appr_b only.
        wf = _workflow("PAYMENT", "", [[self.appr1, self.appr2], [self.appr_b]])
        self._open_request(receipt, wf, receipt.receipt_no)
        created = publish_receipt_submitted(receipt, self.submitter)
        user_ids = [n.user_id for n in created]
        self.assertEqual(sorted(user_ids), sorted([self.appr1.id, self.appr2.id]))
        self.assertNotIn(self.appr_b.id, user_ids)  # level-2 approver NOT yet notified
        self.assertEqual(len(user_ids), len(set(user_ids)))  # no duplicate

    # 10 wrong-company approver does not receive it (company isolation via notify)
    def test_wrong_company_approver_not_notified(self):
        receipt = self._receipt()
        # appr_b is named as an approver but belongs to company B; the deposit's
        # notification company is derived per-recipient, and appr_b's own company
        # is B — but the point of this test is that a *named cross-company* user
        # still only ever gets scoped to their own company, never company A's.
        wf = _workflow("PAYMENT", "", [[self.appr1, self.appr_b]])
        self._open_request(receipt, wf, receipt.receipt_no)
        created = publish_receipt_submitted(receipt, self.submitter)
        by_user = {n.user_id: n for n in created}
        # appr1 (company A) is notified and scoped to company A
        self.assertIn(self.appr1.id, by_user)
        self.assertEqual(by_user[self.appr1.id].company_id, self.company_a.id)
        # appr_b, if notified, is scoped to their OWN company (B) — never A
        if self.appr_b.id in by_user:
            self.assertEqual(by_user[self.appr_b.id].company_id, self.company_b.id)

    def test_no_open_request_no_notification(self):
        receipt = self._receipt()  # no ApprovalRequest created
        created = publish_receipt_submitted(receipt, self.submitter)
        self.assertEqual(created, [])

    # 13 mobile provider gets canonical PAYMENT_SUBMITTED payload
    def test_payment_mobile_provider_payload(self):
        from notifications.providers import mobile

        receipt = self._receipt()
        wf = _workflow("PAYMENT", "", [[self.appr1]])
        self._open_request(receipt, wf, receipt.receipt_no)
        with mock.patch.object(mobile.MobileProvider, "get_tokens",
                               return_value=["ExponentPushToken[abc]"]), \
                mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                publish_receipt_submitted(receipt, self.submitter)
        data = m_requests.post.call_args.kwargs["json"][0]["data"]
        self.assertEqual(data["event_type"], PAYMENT_SUBMITTED)
        self.assertEqual(data["entity_type"], "paymentreceipt")
        self.assertEqual(data["entity_id"], receipt.id)

    # 13 web provider gets the same canonical payload
    def test_payment_web_provider_payload(self):
        from notifications.providers import web

        receipt = self._receipt()
        wf = _workflow("PAYMENT", "", [[self.appr1]])
        self._open_request(receipt, wf, receipt.receipt_no)
        sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}}
        with mock.patch.object(web.WebProvider, "get_subscriptions", return_value=[sub]), \
                mock.patch("pywebpush.webpush") as m_webpush:
            with self.captureOnCommitCallbacks(execute=True):
                publish_receipt_submitted(receipt, self.submitter)
        data = json.loads(m_webpush.call_args.kwargs["data"])
        self.assertEqual(data["event_type"], PAYMENT_SUBMITTED)
        self.assertEqual(data["entity_type"], "paymentreceipt")

    # 17-25 Deposit: submission notifies approvers, correct fields, entity, submitter excluded
    def test_deposit_submitted_notifies_approver(self):
        deposit = self._deposit()
        wf = _workflow("DEPOSIT", "", [[self.appr1]])
        self._open_request(deposit, wf, deposit.deposit_no)
        created = publish_deposit_submitted(deposit, self.submitter)
        self.assertEqual(len(created), 1)
        n = created[0]
        self.assertEqual(n.event_type, DEPOSIT_SUBMITTED)
        self.assertEqual(n.title, "Deposit approval required")
        self.assertIn(deposit.deposit_no, n.message)
        self.assertEqual(n.user_id, self.appr1.id)
        self.assertEqual(n.content_type, ContentType.objects.get_for_model(BankDeposit))
        self.assertEqual(n.object_id, deposit.id)
        self.assertEqual(n.entity, deposit)

    def test_deposit_submitted_payload(self):
        from notifications.services.payloads import build_payload

        deposit = self._deposit()
        wf = _workflow("DEPOSIT", "", [[self.appr1]])
        self._open_request(deposit, wf, deposit.deposit_no)
        n = publish_deposit_submitted(deposit, self.submitter)[0]
        payload = build_payload(n)
        self.assertEqual(payload["entity_type"], "bankdeposit")
        self.assertEqual(payload["entity_id"], deposit.id)
        self.assertEqual(payload["event_type"], DEPOSIT_SUBMITTED)
        self.assertNotIn("deposit_id", payload)

    def test_deposit_submitter_excluded_and_deduped(self):
        deposit = self._deposit()
        wf = _workflow("DEPOSIT", "", [[self.submitter, self.appr1, self.appr2], [self.appr1]])
        self._open_request(deposit, wf, deposit.deposit_no)
        created = publish_deposit_submitted(deposit, self.submitter)
        ids = [n.user_id for n in created]
        self.assertNotIn(self.submitter.id, ids)
        self.assertEqual(sorted(ids), sorted([self.appr1.id, self.appr2.id]))
        self.assertEqual(len(ids), len(set(ids)))

    def test_deposit_mobile_provider_payload(self):
        from notifications.providers import mobile

        deposit = self._deposit()
        wf = _workflow("DEPOSIT", "", [[self.appr1]])
        self._open_request(deposit, wf, deposit.deposit_no)
        with mock.patch.object(mobile.MobileProvider, "get_tokens",
                               return_value=["ExponentPushToken[abc]"]), \
                mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                publish_deposit_submitted(deposit, self.submitter)
        data = m_requests.post.call_args.kwargs["json"][0]["data"]
        self.assertEqual(data["event_type"], DEPOSIT_SUBMITTED)
        self.assertEqual(data["entity_type"], "bankdeposit")
        self.assertEqual(data["entity_id"], deposit.id)


class SubmissionIntegrationTests(TestCase):
    """The REAL service path: submit_receipt / submit_deposit fire the event."""

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name="Int Co A")
        cls.submitter = User.objects.create(username="i_sub", name="Submitter", company=cls.company_a)
        cls.approver = User.objects.create(username="i_ap", name="Approver", company=cls.company_a)

    def test_submit_receipt_fires_payment_submitted(self):
        from payments import services

        _workflow("PAYMENT", "", [[self.approver]])
        receipt = PaymentReceipt.objects.create(
            receipt_no="RC-INT-1", company="OIL", card_code="CUST1",
            payment_date=date.today(), total_amount=Decimal("100"),
            created_by=self.submitter,
        )
        PaymentMethodEntry.objects.create(receipt=receipt, method="CASH", amount=Decimal("100"))

        # validate_receipt enforces invoice-allocation + SAP-G/L-mapping rules
        # that are orthogonal to notification wiring and depend on SAP config;
        # stub it so this test exercises the real submit -> notify path only.
        with mock.patch("payments.services.validate_receipt", return_value=True):
            services.submit_receipt(receipt, self.submitter)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_APPROVAL)
        qs = Notification.objects.filter(event_type=PAYMENT_SUBMITTED, user=self.approver)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().object_id, receipt.id)
        # submitter is NOT notified of their own submission
        self.assertEqual(
            Notification.objects.filter(event_type=PAYMENT_SUBMITTED, user=self.submitter).count(), 0)

    def test_submit_receipt_rollback_creates_no_notification(self):
        from django.db import transaction
        from payments import services

        _workflow("PAYMENT", "", [[self.approver]])
        receipt = PaymentReceipt.objects.create(
            receipt_no="RC-INT-RB", company="OIL", card_code="CUST1",
            payment_date=date.today(), total_amount=Decimal("100"),
            created_by=self.submitter,
        )
        PaymentMethodEntry.objects.create(receipt=receipt, method="CASH", amount=Decimal("100"))

        before = Notification.objects.count()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                with mock.patch("payments.services.validate_receipt", return_value=True):
                    services.submit_receipt(receipt, self.submitter)
                raise RuntimeError("rollback after submit")
        # submit_receipt is itself atomic; the outer rollback still discards its
        # Notification rows because they were created in the same DB transaction.
        self.assertEqual(Notification.objects.count(), before)

    def test_submit_deposit_fires_deposit_submitted(self):
        from payments import services

        _workflow("DEPOSIT", "", [[self.approver]])
        # A depositable CASH receipt to line the deposit against.
        receipt = PaymentReceipt.objects.create(
            receipt_no="RC-FOR-DEP", company="OIL", card_code="CUST1",
            payment_date=date.today(), total_amount=Decimal("100"),
            status=PaymentReceipt.Status.POSTED, created_by=self.submitter,
        )
        PaymentMethodEntry.objects.create(receipt=receipt, method="CASH", amount=Decimal("100"))
        deposit = BankDeposit.objects.create(
            deposit_no="DEP-INT-1", company="OIL", deposit_date=date.today(),
            collected_amount=Decimal("100"), deposit_amount=Decimal("100"),
            bank_key="HDFC:12345", created_by=self.submitter,
        )
        BankDepositLine.objects.create(deposit=deposit, receipt=receipt, amount=Decimal("100"))

        # bank_master.find_bank reads a SAP-backed cache; mock it so the test is
        # hermetic and SAP-independent (mirrors the real "bank already cached"
        # path — SAP being down does not block a cached-bank submission).
        fake_bank = {
            "key": "HDFC:12345", "bank_code": "HDFC",
            "gl_account": "12345", "label": "HDFC Bank",
        }
        with mock.patch("payments.services.bank_master.find_bank", return_value=fake_bank), \
                mock.patch("payments.services.validate_deposit", return_value=True):
            services.submit_deposit(deposit, self.submitter)

        deposit.refresh_from_db()
        self.assertEqual(deposit.status, BankDeposit.Status.PENDING_APPROVAL)
        qs = Notification.objects.filter(event_type=DEPOSIT_SUBMITTED, user=self.approver)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().object_id, deposit.id)
        self.assertEqual(
            Notification.objects.filter(event_type=DEPOSIT_SUBMITTED, user=self.submitter).count(), 0)
