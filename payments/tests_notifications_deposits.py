"""Phase 3.7 — Bank Deposit integration with the reusable notification framework.

Deposits (``payments.BankDeposit``) are the SECOND consumer of the framework,
after payment receipts. The point of this suite is the ABSTRACTION TEST: a second
business object publishes generic notifications (entity = the BankDeposit, no
deposit FK in the framework) through the exact same ``notify()`` API, and the
framework required ZERO changes. External Expo / Web Push network calls are
mocked — no real push is sent.
"""

import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from notifications.models import Notification
from notifications.providers.base import ProviderResult
from payments.tests_support import notifications_of
from payments.models import BankDeposit
from payments.notification_events import (
    DEPOSIT_APPROVED,
    DEPOSIT_REJECTED,
    publish_deposit_decision,
)


class DepositsNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name="Dep Co A")
        cls.company_b = Company.objects.create(name="Dep Co B")
        cls.submitter = User.objects.create(username="d_sub", name="Submitter", company=cls.company_a)
        cls.approver = User.objects.create(
            username="d_appr", name="Approver", company=cls.company_a,
            # Being the stage's user is not enough: approving also
            # requires the action key (payments.permissions.may_act_on).
            extra_pages=["Deposit_Approve"])
        cls.other_company_user = User.objects.create(username="d_other", name="Other", company=cls.company_b)

    def _deposit(self, no="DEP-N-1"):
        return BankDeposit.objects.create(
            deposit_no=no, company="OIL",
            deposit_date=date.today(),
            collected_amount=100, deposit_amount=100,
        )

    # 1 + 3 (approved creates notification, correct recipient)
    def test_approved_event_creates_notification(self):
        deposit = self._deposit()
        created = publish_deposit_decision(deposit, self.submitter, approved=True)
        self.assertEqual(len(created), 1)
        n = created[0]
        self.assertEqual(n.event_type, DEPOSIT_APPROVED)
        self.assertEqual(n.title, "Deposit approved")
        self.assertIn(deposit.deposit_no, n.message)
        self.assertFalse(n.is_read)
        self.assertEqual(n.user_id, self.submitter.id)

    # 2 (rejected creates notification, reason threaded)
    def test_rejected_event(self):
        deposit = self._deposit()
        n = publish_deposit_decision(
            deposit, self.submitter, approved=False, reason="Slip mismatch"
        )[0]
        self.assertEqual(n.event_type, DEPOSIT_REJECTED)
        self.assertEqual(n.title, "Deposit rejected")
        self.assertIn("Slip mismatch", n.message)

    # 5 (company isolation — company derived from submitter, not the deposit's
    # SAP category string)
    def test_company_scoping_from_submitter(self):
        deposit = self._deposit()
        n = publish_deposit_decision(deposit, self.submitter, approved=True)[0]
        self.assertEqual(n.company_id, self.company_a.id)

    # 4 (wrong recipient is not notified) + user isolation
    def test_other_company_user_not_notified(self):
        deposit = self._deposit()
        publish_deposit_decision(deposit, self.submitter, approved=True)
        self.assertEqual(
            Notification.objects.filter(user=self.other_company_user).count(), 0
        )
        self.assertEqual(Notification.objects.filter(user=self.submitter).count(), 1)

    # 6 + 7 + 8 (GFK resolves to the deposit; canonical payload carries the
    # deposit entity_type + entity_id)
    def test_deposit_gfk_and_payload(self):
        from notifications.services.payloads import build_payload

        deposit = self._deposit()
        n = publish_deposit_decision(deposit, self.submitter, approved=True)[0]
        n.refresh_from_db()
        self.assertEqual(n.content_type, ContentType.objects.get_for_model(BankDeposit))
        self.assertEqual(n.object_id, deposit.id)
        self.assertEqual(n.entity, deposit)
        payload = build_payload(n)
        self.assertEqual(payload["entity_type"], "bankdeposit")
        self.assertEqual(payload["entity_id"], deposit.id)
        self.assertEqual(payload["event_type"], DEPOSIT_APPROVED)
        self.assertNotIn("deposit_id", payload)  # generic, never a deposit-specific id

    def test_no_submitter_no_notification(self):
        deposit = self._deposit()
        created = publish_deposit_decision(deposit, None, approved=True)
        self.assertEqual(created, [])
        # Scoped to THIS document: the shared TEST database already holds
        # notifications for real ones.
        self.assertEqual(notifications_of(deposit).count(), 0)

    # 9 (mobile provider receives the canonical deposit payload)
    def test_mobile_provider_receives_deposit_payload(self):
        from notifications.providers import mobile

        deposit = self._deposit()
        with mock.patch.object(mobile.MobileProvider, "get_tokens",
                               return_value=["ExponentPushToken[abc]"]), \
                mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                publish_deposit_decision(deposit, self.submitter, approved=True)
        self.assertTrue(m_requests.post.called)
        messages = m_requests.post.call_args.kwargs["json"]
        data = messages[0]["data"]
        self.assertEqual(data["entity_type"], "bankdeposit")
        self.assertEqual(data["entity_id"], deposit.id)
        self.assertEqual(data["event_type"], DEPOSIT_APPROVED)
        self.assertEqual(messages[0]["title"], "Deposit approved")

    # 10 (web provider receives the canonical deposit payload)
    def test_web_provider_receives_deposit_payload(self):
        from notifications.providers import web

        deposit = self._deposit()
        sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}}
        with mock.patch.object(web.WebProvider, "get_subscriptions", return_value=[sub]), \
                mock.patch("pywebpush.webpush") as m_webpush:
            with self.captureOnCommitCallbacks(execute=True):
                publish_deposit_decision(deposit, self.submitter, approved=True)
        self.assertTrue(m_webpush.called)
        data = json.loads(m_webpush.call_args.kwargs["data"])
        self.assertEqual(data["entity_type"], "bankdeposit")
        self.assertEqual(data["entity_id"], deposit.id)

    # 11 (provider failure is isolated — the notification is still created)
    def test_provider_failure_does_not_break_operation(self):
        from notifications.providers import mobile

        deposit = self._deposit()
        with mock.patch.object(mobile.MobileProvider, "get_tokens", return_value=["tok"]), \
                mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.side_effect = RuntimeError("network down")
            with self.captureOnCommitCallbacks(execute=True):
                created = publish_deposit_decision(deposit, self.submitter, approved=True)
        self.assertEqual(len(created), 1)

    # 16 + the real approval path (exactly one record, no duplicate)
    #
    # Was `payments.hooks._on_deposit_approved` with a mocked ApprovalRequest;
    # see tests_notifications.py for why that was replaced by the real engine
    # path rather than re-mocked. The no-duplicate guarantee is what matters
    # here and it is asserted against a genuine approval.
    def test_approval_notifies_the_submitter_exactly_once(self):
        from payments import workflow_flow
        from payments.tests_workflow_fixtures import payments_workflow

        payments_workflow(self.approver, documents='deposits')
        deposit = self._deposit('DEP-N-APPROVE')
        deposit.created_by = self.submitter
        # See tests_notifications.py: kept clear of the company live workflow
        # configuration targets.
        deposit.company = 'MART'
        deposit.save(update_fields=['created_by', 'company'])
        flow = workflow_flow.start(deposit, user=self.submitter)

        # on_commit (the SAP post) is NOT executed here → no SAP call, no push.
        workflow_flow.approve(flow, user=self.approver)

        qs = Notification.objects.filter(
            user=self.submitter, event_type=DEPOSIT_APPROVED,
            object_id=deposit.id)
        self.assertEqual(qs.count(), 1)  # exactly one — no duplicate

    # 12 + 13 (rollback → no notification record, no delivery)
    def test_rollback_no_records_no_delivery(self):
        from django.db import transaction

        deposit = self._deposit()
        before = Notification.objects.count()
        spy = mock.Mock()
        spy.channel = "mobile_push"
        spy.send.return_value = ProviderResult("mobile_push", delivered=1)
        with mock.patch("notifications.services.dispatcher.default_providers",
                        return_value=[spy]):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    publish_deposit_decision(deposit, self.submitter, approved=True)
                    raise RuntimeError("rollback")
        self.assertEqual(Notification.objects.count(), before)
        self.assertFalse(spy.send.called)

    # 14 (uses the new generic framework table, not the old Orders one)
    def test_uses_new_generic_table(self):
        self.assertIn("notifications_notification", Notification._meta.db_table)
        self.assertNotEqual(Notification._meta.db_table, "notifications")

    # 15 (no Orders notification row is created by a deposit decision)
    def test_no_orders_notification_created(self):
        from orders.models import Notification as OrdersNotification

        before = OrdersNotification.objects.count()
        deposit = self._deposit()
        publish_deposit_decision(deposit, self.submitter, approved=True)
        self.assertEqual(OrdersNotification.objects.count(), before)

    # 17 (event names are owned by the deposits/payments module, not the framework)
    def test_event_names_owned_by_module(self):
        import notifications.constants as constants
        from payments import notification_events

        # The names live in the business module's own events module...
        self.assertEqual(notification_events.DEPOSIT_APPROVED, "DEPOSIT_APPROVED")
        self.assertEqual(notification_events.DEPOSIT_REJECTED, "DEPOSIT_REJECTED")
        # ...and were NOT added to the framework's shipped event set. The
        # framework validates NAME FORMAT only (is_valid_event_name), so a module
        # can own new event names without any change to notifications/constants.
        self.assertNotIn("DEPOSIT_APPROVED", constants.EVENT_NAMES)
        self.assertNotIn("DEPOSIT_REJECTED", constants.EVENT_NAMES)
        self.assertTrue(constants.is_valid_event_name("DEPOSIT_APPROVED"))
