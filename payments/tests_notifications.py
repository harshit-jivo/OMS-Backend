"""Phase 3.5 — Payments integration with the reusable notification framework.

Payments is the first real consumer. These tests prove Payments publishes
generic notifications (entity = the PaymentReceipt, no order/payment FK in the
framework) and that delivery goes through the generic providers. External Expo /
Web Push network calls are mocked — no real push is sent.
"""

import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from notifications.models import Notification
from notifications.providers.base import ProviderResult
from payments.models import PaymentReceipt
from payments.notification_events import (
    PAYMENT_APPROVED,
    PAYMENT_REJECTED,
    publish_receipt_decision,
)


class PaymentsNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name="Pay Co A")
        cls.company_b = Company.objects.create(name="Pay Co B")
        cls.submitter = User.objects.create(username="p_sub", name="Submitter", company=cls.company_a)
        cls.approver = User.objects.create(username="p_appr", name="Approver", company=cls.company_a)
        cls.other_company_user = User.objects.create(username="p_other", name="Other", company=cls.company_b)

    def _receipt(self, no="RC-N-1"):
        return PaymentReceipt.objects.create(
            receipt_no=no, company="OIL", card_code="CUST1",
            payment_date=date.today(), total_amount=100,
        )

    # 1 + 2 + 3 + 4
    def test_approved_event_creates_notification(self):
        receipt = self._receipt()
        created = publish_receipt_decision(receipt, self.submitter, approved=True)
        self.assertEqual(len(created), 1)
        n = created[0]
        self.assertEqual(n.event_type, PAYMENT_APPROVED)
        self.assertEqual(n.title, "Payment approved")
        self.assertIn(receipt.receipt_no, n.message)
        self.assertFalse(n.is_read)

    # 5 + 11 + 12 (GFK + payload entity)
    def test_payment_entity_gfk_and_payload(self):
        from notifications.services.payloads import build_payload

        receipt = self._receipt()
        n = publish_receipt_decision(receipt, self.submitter, approved=True)[0]
        n.refresh_from_db()
        self.assertEqual(n.content_type, ContentType.objects.get_for_model(PaymentReceipt))
        self.assertEqual(n.object_id, receipt.id)
        self.assertEqual(n.entity, receipt)
        payload = build_payload(n)
        self.assertEqual(payload["entity_type"], "paymentreceipt")
        self.assertEqual(payload["entity_id"], receipt.id)
        self.assertEqual(payload["event_type"], PAYMENT_APPROVED)
        self.assertNotIn("order_id", payload)  # generic, never payment/order id

    # 6 + 7 (company from submitter, recipients)
    def test_company_and_recipient_scoping(self):
        receipt = self._receipt()
        n = publish_receipt_decision(receipt, self.submitter, approved=True)[0]
        self.assertEqual(n.user_id, self.submitter.id)
        self.assertEqual(n.company_id, self.company_a.id)  # submitter's own company

    # 8 + 9 (wrong-company user never notified; user isolation)
    def test_other_company_user_not_notified(self):
        receipt = self._receipt()
        publish_receipt_decision(receipt, self.submitter, approved=True)
        self.assertEqual(
            Notification.objects.filter(user=self.other_company_user).count(), 0
        )
        self.assertEqual(Notification.objects.filter(user=self.submitter).count(), 1)

    def test_rejected_event(self):
        receipt = self._receipt()
        n = publish_receipt_decision(
            receipt, self.submitter, approved=False, reason="Missing invoice"
        )[0]
        self.assertEqual(n.event_type, PAYMENT_REJECTED)
        self.assertEqual(n.title, "Payment rejected")
        self.assertIn("Missing invoice", n.message)

    def test_no_submitter_no_notification(self):
        receipt = self._receipt()
        created = publish_receipt_decision(receipt, None, approved=True)
        self.assertEqual(created, [])
        self.assertEqual(Notification.objects.count(), 0)

    # 13 (mobile provider gets correct payload)
    def test_mobile_provider_receives_payment_payload(self):
        from notifications.providers import mobile

        receipt = self._receipt()
        with mock.patch.object(mobile.MobileProvider, "get_tokens",
                               return_value=["ExponentPushToken[abc]"]), \
                mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                publish_receipt_decision(receipt, self.submitter, approved=True)
        self.assertTrue(m_requests.post.called)
        messages = m_requests.post.call_args.kwargs["json"]
        data = messages[0]["data"]
        self.assertEqual(data["entity_type"], "paymentreceipt")
        self.assertEqual(data["entity_id"], receipt.id)
        self.assertEqual(data["event_type"], PAYMENT_APPROVED)
        self.assertEqual(messages[0]["title"], "Payment approved")

    # 14 (web provider gets correct payload)
    def test_web_provider_receives_payment_payload(self):
        from notifications.providers import web

        receipt = self._receipt()
        sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}}
        with mock.patch.object(web.WebProvider, "get_subscriptions", return_value=[sub]), \
                mock.patch("pywebpush.webpush") as m_webpush:
            with self.captureOnCommitCallbacks(execute=True):
                publish_receipt_decision(receipt, self.submitter, approved=True)
        self.assertTrue(m_webpush.called)
        data = json.loads(m_webpush.call_args.kwargs["data"])
        self.assertEqual(data["entity_type"], "paymentreceipt")
        self.assertEqual(data["entity_id"], receipt.id)

    # 15 (provider failure does not break the payment operation)
    def test_provider_failure_does_not_break_operation(self):
        from notifications.providers import mobile

        receipt = self._receipt()
        with mock.patch.object(mobile.MobileProvider, "get_tokens", return_value=["tok"]), \
                mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.side_effect = RuntimeError("network down")
            with self.captureOnCommitCallbacks(execute=True):
                created = publish_receipt_decision(receipt, self.submitter, approved=True)
        self.assertEqual(len(created), 1)  # notification still created; no raise

    # 16 (successful hook path creates a notification)
    def test_approval_hook_creates_notification(self):
        from payments.hooks import _on_receipt_approved

        receipt = self._receipt()
        approval_request = mock.Mock()
        approval_request.document = receipt
        approval_request.submitted_by = self.submitter
        approval_request._acting_user = self.approver
        approval_request.pk = 1
        # on_commit (SAP post) is NOT executed here → no SAP call, no push.
        _on_receipt_approved(approval_request)
        n = Notification.objects.filter(
            user=self.submitter, event_type=PAYMENT_APPROVED
        ).first()
        self.assertIsNotNone(n)
        self.assertEqual(n.object_id, receipt.id)

    # 17 (rollback → no records, no delivery)
    def test_rollback_no_records_no_delivery(self):
        from django.db import transaction

        receipt = self._receipt()
        before = Notification.objects.count()
        spy = mock.Mock()
        spy.channel = "mobile_push"
        spy.send.return_value = ProviderResult("mobile_push", delivered=1)
        with mock.patch("notifications.services.dispatcher.default_providers",
                        return_value=[spy]):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    publish_receipt_decision(receipt, self.submitter, approved=True)
                    raise RuntimeError("rollback")
        self.assertEqual(Notification.objects.count(), before)
        self.assertFalse(spy.send.called)
