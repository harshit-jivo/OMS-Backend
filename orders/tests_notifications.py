"""Tests for the Orders notification system (Phase 1 stabilisation).

Scope is deliberately the CURRENT behaviour, not a desired future one: these
tests exist to pin the contract so Phase 2's extraction can be verified as
behaviour-preserving. They therefore assert the exact payload keys, the exact
endpoint shapes and the exact status codes the mobile app and web client rely
on today.

Every outbound HTTP call is mocked. Nothing here contacts Expo or a browser
push service, so the suite is safe to run anywhere.

PATCHING RULE — use ``patch.object(module, "name")``, never
``patch("orders.webpush.name")``.

A dotted string target is resolved lazily, so it still "succeeds" against a
module that has become a re-export shim: the patch lands on a name nobody
calls, the real function runs, and the test passes while asserting nothing.
``patch.object`` resolves the attribute at decoration time and raises
AttributeError the moment it moves — a loud failure instead of a false green.
This matters because the notification transport is scheduled to move into a
dedicated app, and these tests are the parity harness for that move.
"""
from unittest.mock import patch

import requests
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User, UserRole

from . import notifications as notif
from . import views
# Recipient resolution moved to `orders.views.notifications` when views.py was
# split (plan item 3.1). Patch against THAT module, not the `orders.views`
# package: the package re-exports these names, so `patch.object(views, ...)`
# finds an attribute, succeeds, and stubs a binding `_resolve_notification_
# recipients` never reads — which is precisely the false green the module
# docstring above warns about. It caught three tests during the split.
from .views import notifications as views_notifications
from . import webpush as webpush_module
from .models import Notification, Order, OrderStatus, PushToken, WebPushSubscription
from .serializers import NotificationSerializer
from .views import (
    NotificationHistoryView,
    NotificationListView,
    PushTokenView,
    WebPushSubscriptionView,
)


class _Base(TestCase):
    """Two users, one order — the minimum to prove per-user isolation."""

    def setUp(self):
        role = UserRole.objects.create(name="billing", display_name="Billing")
        self.user = User.objects.create_user(
            username="notif-owner", password="pw", name="Owner", role=role
        )
        self.other = User.objects.create_user(
            username="notif-stranger", password="pw", name="Stranger", role=role
        )
        self.status = OrderStatus.objects.create(code="BILLING", name="Billing")
        self.order = Order.objects.create(
            order_number="ORD-NOTIF-0001",
            card_code="C001",
            card_name="Test Party",
            status=self.status,
            created_by=self.user,
        )

    def _notify(self, user=None, message="Test message", is_read=False):
        return Notification.objects.create(
            user=user or self.user, order=self.order,
            message=message, is_read=is_read,
        )

    def _get(self, view, url, user=None, **kwargs):
        request = APIRequestFactory().get(url, **kwargs)
        force_authenticate(request, user=user or self.user)
        return view.as_view()(request)


# ---------------------------------------------------------------------------
# Creation and persistence
# ---------------------------------------------------------------------------

class NotificationCreationTests(_Base):
    def test_save_notification_persists_the_record(self):
        created = notif.save_notification(self.user, self.order, "Hello")

        self.assertIsNotNone(created)
        self.assertEqual(created.user, self.user)
        self.assertEqual(created.order, self.order)
        self.assertFalse(created.is_read)

    def test_save_notification_refuses_incomplete_input(self):
        """A missing recipient must not create a row addressed to nobody."""
        self.assertIsNone(notif.save_notification(None, self.order, "Hello"))
        self.assertIsNone(notif.save_notification(self.user, None, "Hello"))
        self.assertIsNone(notif.save_notification(self.user, self.order, ""))
        self.assertEqual(Notification.objects.count(), 0)

    @patch.object(notif, "send_push_notification")
    def test_fan_out_excludes_the_actor(self, _push):
        """The person who caused the event is never notified about it."""
        notif.deliver_notification_to_many(
            [self.user, self.other], self.order, "Changed", exclude_user=self.user
        )

        recipients = list(Notification.objects.values_list("user_id", flat=True))
        self.assertEqual(recipients, [self.other.id])

    @patch.object(notif, "send_push_notification")
    def test_fan_out_continues_after_one_recipient_fails(self, _push):
        """One bad recipient must not cost the others their notification."""
        real_save = notif.save_notification

        def explode_for_first(user, order, message):
            if user.id == self.user.id:
                raise RuntimeError("simulated database failure")
            return real_save(user, order, message)

        with patch.object(notif, "save_notification", side_effect=explode_for_first):
            notif.deliver_notification_to_many(
                [self.user, self.other], self.order, "Changed"
            )

        self.assertEqual(
            list(Notification.objects.values_list("user_id", flat=True)),
            [self.other.id],
        )


# ---------------------------------------------------------------------------
# Payload contract — what old mobile builds depend on
# ---------------------------------------------------------------------------

class PayloadContractTests(_Base):
    REQUIRED_KEYS = {
        "notification_id", "order_id", "screen", "notification_type",
        "event_type", "title", "message", "timestamp",
    }

    def test_expo_data_block_keys_are_unchanged(self):
        record = self._notify()

        data = notif._build_push_data(
            record, event_type="ORDER_APPROVED",
            notification_type="approval", title="Order approved",
        )

        # Exactly these keys: an old app build reads notification_id/order_id/
        # screen and ignores the rest, so removing one breaks deep linking on
        # devices that cannot be force-updated.
        self.assertEqual(set(data), self.REQUIRED_KEYS)
        self.assertEqual(data["notification_id"], record.id)
        self.assertEqual(data["order_id"], self.order.id)
        self.assertEqual(data["screen"], "notifications")

    def test_web_payload_adds_only_order_number_and_body(self):
        record = self._notify()

        web = notif.build_notification_payload(
            record, "ORDER_APPROVED", "approval", "Order approved",
            order_number=self.order.order_number,
        )

        self.assertEqual(set(web) - self.REQUIRED_KEYS, {"order_number", "body"})
        self.assertEqual(web["body"], web["message"])

    def test_title_falls_back_to_the_default_heading(self):
        data = notif._build_push_data(self._notify())
        self.assertEqual(data["title"], notif.DEFAULT_PUSH_TITLE)


# ---------------------------------------------------------------------------
# Delivery + failure handling
# ---------------------------------------------------------------------------

class DeliveryTests(_Base):
    def setUp(self):
        super().setUp()
        PushToken.objects.create(
            user=self.user, token="ExponentPushToken[aaa]",
            platform="android", is_active=True,
        )

    def _expo_ok(self, ticket_id="ticket-1"):
        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"status": "ok", "id": ticket_id}]}

        return _Response()

    @patch.object(notif.threading, "Thread")
    @patch.object(notif.requests, "post")
    def test_expo_send_targets_active_tokens(self, post, _thread):
        post.return_value = self._expo_ok()

        notif.send_push_notification(self.user, self._notify())

        sent = post.call_args.kwargs["json"]
        self.assertEqual([m["to"] for m in sent], ["ExponentPushToken[aaa]"])
        self.assertEqual(sent[0]["channelId"], notif.ANDROID_CHANNEL_ID)

    @patch.object(notif.requests, "post")
    def test_inactive_tokens_are_never_targeted(self, post):
        PushToken.objects.update(is_active=False)

        notif.send_push_notification(self.user, self._notify())

        post.assert_not_called()

    @patch.object(notif.requests, "post")
    def test_network_failure_does_not_raise(self, post):
        """A dead push service must not turn an approval into a 500."""
        post.side_effect = requests.RequestException("connection reset")

        notif.send_push_notification(self.user, self._notify())  # must not raise

    @patch.object(notif.requests, "post")
    def test_malformed_response_does_not_raise(self, post):
        """Previously only RequestException was caught, so this escaped."""
        post.side_effect = ValueError("unexpected shape")

        notif.send_push_notification(self.user, self._notify())  # must not raise

    @patch.object(notif.requests, "post")
    def test_device_not_registered_deactivates_the_token(self, post):
        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{
                    "status": "error",
                    "details": {"error": "DeviceNotRegistered"},
                }]}

        post.return_value = _Response()

        notif.send_push_notification(self.user, self._notify())

        self.assertFalse(PushToken.objects.get(token="ExponentPushToken[aaa]").is_active)

    @patch.object(webpush_module, "send_web_push_to_user")
    @patch.object(notif, "send_push_notification")
    def test_record_is_saved_even_when_both_channels_fail(self, push, web):
        """The in-app record is the durable channel; transports are best-effort."""
        push.side_effect = RuntimeError("expo exploded")
        web.side_effect = RuntimeError("web push exploded")

        created = notif.deliver_notification(self.user, self.order, "Still saved")

        self.assertIsNotNone(created)
        self.assertTrue(Notification.objects.filter(pk=created.pk).exists())

    @patch.object(webpush_module, "send_web_push_to_user")
    @patch.object(notif, "send_push_notification")
    def test_mobile_failure_does_not_block_web_push(self, push, web):
        push.side_effect = RuntimeError("expo exploded")

        notif.deliver_notification(self.user, self.order, "Message")

        web.assert_called_once()


# ---------------------------------------------------------------------------
# API endpoints — shape and permissions
# ---------------------------------------------------------------------------

class NotificationEndpointTests(_Base):
    def test_list_returns_only_the_callers_notifications(self):
        mine = self._notify(message="Mine")
        self._notify(user=self.other, message="Theirs")

        response = self._get(NotificationListView, "/orders/notifications/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([n["id"] for n in response.data], [mine.id])

    def test_list_payload_fields_are_unchanged(self):
        self._notify()

        response = self._get(NotificationListView, "/orders/notifications/")

        self.assertEqual(
            set(response.data[0]),
            {"id", "message", "is_read", "created_at", "order_id"},
        )

    def test_history_reports_totals_and_unread_count(self):
        self._notify(message="a")
        self._notify(message="b", is_read=True)
        self._notify(user=self.other, message="not mine")

        response = self._get(
            NotificationHistoryView, "/orders/notifications/history/?limit=20"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["unread_count"], 1)

    def test_history_unread_filter(self):
        unread = self._notify(message="unread")
        self._notify(message="read", is_read=True)

        response = self._get(
            NotificationHistoryView, "/orders/notifications/history/?filter=unread"
        )

        self.assertEqual([n["id"] for n in response.data["results"]], [unread.id])

    def test_mark_all_read_affects_only_the_caller(self):
        mine = self._notify()
        theirs = self._notify(user=self.other)

        request = APIRequestFactory().post("/orders/notifications/", {})
        force_authenticate(request, user=self.user)
        response = NotificationListView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Notification.objects.get(pk=mine.pk).is_read)
        self.assertFalse(Notification.objects.get(pk=theirs.pk).is_read)

    def test_cannot_mark_another_users_notification_read(self):
        theirs = self._notify(user=self.other)

        request = APIRequestFactory().patch(f"/orders/notifications/{theirs.pk}/")
        force_authenticate(request, user=self.user)
        response = NotificationListView.as_view()(request, pk=theirs.pk)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Notification.objects.get(pk=theirs.pk).is_read)

    def test_anonymous_access_is_rejected(self):
        request = APIRequestFactory().get("/orders/notifications/")
        self.assertIn(NotificationListView.as_view()(request).status_code, (401, 403))


class PushTokenEndpointTests(_Base):
    def _post(self, payload, user=None):
        request = APIRequestFactory().post("/orders/push-token/", payload)
        force_authenticate(request, user=user or self.user)
        return PushTokenView.as_view()(request)

    def test_registration_stores_the_token_for_the_caller(self):
        response = self._post({"token": "ExponentPushToken[xyz]", "platform": "android"})

        self.assertEqual(response.status_code, 200)
        token = PushToken.objects.get(token="ExponentPushToken[xyz]")
        self.assertEqual(token.user, self.user)
        self.assertTrue(token.is_active)

    def test_token_is_required(self):
        self.assertEqual(self._post({"platform": "android"}).status_code, 400)

    def test_re_registering_moves_the_token_to_the_current_user(self):
        """A shared device must not keep pushing to whoever logged in first."""
        self._post({"token": "ExponentPushToken[shared]", "platform": "android"})
        self._post({"token": "ExponentPushToken[shared]", "platform": "android"},
                   user=self.other)

        token = PushToken.objects.get(token="ExponentPushToken[shared]")
        self.assertEqual(token.user, self.other)
        self.assertEqual(PushToken.objects.filter(token="ExponentPushToken[shared]").count(), 1)

    def test_logout_deactivates_only_the_callers_token(self):
        PushToken.objects.create(user=self.other, token="ExponentPushToken[other]",
                                 platform="ios", is_active=True)
        self._post({"token": "ExponentPushToken[mine]", "platform": "android"})

        request = APIRequestFactory().delete(
            "/orders/push-token/", {"token": "ExponentPushToken[other]"}, format="json"
        )
        force_authenticate(request, user=self.user)
        PushTokenView.as_view()(request)

        # Scoped to request.user, so another user's token survives.
        self.assertTrue(PushToken.objects.get(token="ExponentPushToken[other]").is_active)


class WebPushSubscriptionTests(_Base):
    SUB = {
        "endpoint": "https://push.example.com/sub-1",
        "keys": {"p256dh": "key-p256dh", "auth": "key-auth"},
    }

    def _post(self, payload, user=None):
        request = APIRequestFactory().post(
            "/orders/web-push/subscribe/", {"subscription": payload}, format="json"
        )
        force_authenticate(request, user=user or self.user)
        return WebPushSubscriptionView.as_view()(request)

    def test_subscription_is_stored_for_the_caller(self):
        self.assertEqual(self._post(self.SUB).status_code, 200)

        sub = WebPushSubscription.objects.get(endpoint=self.SUB["endpoint"])
        self.assertEqual(sub.user, self.user)
        self.assertTrue(sub.is_active)

    def test_incomplete_subscription_is_rejected(self):
        response = self._post({"endpoint": "https://push.example.com/x", "keys": {}})
        self.assertEqual(response.status_code, 400)

    def test_resubscribing_rehomes_the_endpoint_without_duplicating(self):
        self._post(self.SUB)
        self._post(self.SUB, user=self.other)

        subs = WebPushSubscription.objects.filter(endpoint=self.SUB["endpoint"])
        self.assertEqual(subs.count(), 1)
        self.assertEqual(subs.first().user, self.other)


# ---------------------------------------------------------------------------
# Query cost — guards the N+1 fixed in Phase 1
# ---------------------------------------------------------------------------

class QueryCountTests(_Base):
    def test_serialising_a_page_costs_one_query(self):
        """`order_id` must read the local column, not traverse to Order.

        Sourcing it from `order.id` lazy-loaded the related Order per row, so a
        50-row page cost 51 queries whenever the caller forgot select_related.
        """
        for i in range(10):
            self._notify(message=f"n{i}")

        with self.assertNumQueries(1):
            data = NotificationSerializer(
                list(Notification.objects.filter(user=self.user)), many=True
            ).data

        self.assertEqual(len(data), 10)
        self.assertTrue(all(row["order_id"] == self.order.id for row in data))

    def test_list_endpoint_issues_a_single_query(self):
        for i in range(5):
            self._notify(message=f"n{i}")

        with self.assertNumQueries(1):
            self._get(NotificationListView, "/orders/notifications/")


# ---------------------------------------------------------------------------
# Recipient resolution — the routing rules
# ---------------------------------------------------------------------------

class RecipientResolutionTests(_Base):
    """Characterisation tests for ``views._resolve_notification_recipients``.

    This function decides WHO hears about a status change, and it had no test
    coverage at all. It is also where the single most consequential rule in the
    notification system lives: when a recipient cannot be resolved it notifies
    NOBODY rather than falling back to everyone in the role, because a
    broadcast would leak order details to unrelated users.

    These tests pin the current behaviour exactly. They are the parity harness
    for the planned extraction: the resolver is scheduled to move out of
    ``views.py`` into a registered hook, and its routing must not drift while
    it moves.

    The two category-scoped helpers (``_assigned_rate_approvers_for_order`` and
    ``_billing_users_for_order``) are patched rather than fixtured. They depend
    on item categories, party main-groups and SAP party rows; reconstructing
    all that would test THOSE helpers, not the branch selection under test.
    """

    def setUp(self):
        super().setUp()
        self.auditor_role = UserRole.objects.create(
            name="auditor", display_name="Auditor")
        self.auditor = User.objects.create_user(
            username="notif-auditor", password="pw", name="Aud", role=self.auditor_role)
        self.approver = User.objects.create_user(
            username="notif-approver", password="pw", name="App", role=self.auditor_role)

    def _resolve(self, status_name, actor=None, previous_status=None):
        return views._resolve_notification_recipients(
            self.order, status_name, actor, previous_status)

    # --- forward transitions: notify whoever owns the NEXT action ----------

    def test_rate_approval_notifies_assigned_approvers(self):
        with patch.object(views_notifications, "_assigned_rate_approvers_for_order",
                          return_value=[self.approver]):
            plan = self._resolve("Rate Approval")

        self.assertEqual(plan.recipients, [self.approver])
        self.assertEqual(plan.event_type, notif.NotificationEvents.RATE_APPROVAL_REQUESTED)
        self.assertEqual(plan.notification_type, notif.NotificationTypes.APPROVAL)
        self.assertIn(self.order.order_number, plan.message)

    def test_need_approval_is_an_alias_for_rate_approval(self):
        """Both status names must route identically — they are one branch."""
        with patch.object(views_notifications, "_assigned_rate_approvers_for_order",
                          return_value=[self.approver]):
            self.assertEqual(
                self._resolve("Need Approval").event_type,
                self._resolve("Rate Approval").event_type,
            )

    def test_billing_notifies_billing_users(self):
        with patch.object(views_notifications, "_billing_users_for_order",
                          return_value=[self.other]):
            plan = self._resolve("Billing")

        self.assertEqual(plan.recipients, [self.other])
        self.assertEqual(plan.event_type, notif.NotificationEvents.BILLING_REQUESTED)

    def test_auditor_approval_notifies_active_auditors(self):
        plan = self._resolve("Auditor Approval")

        self.assertIn(self.auditor, plan.recipients)
        self.assertEqual(plan.event_type, notif.NotificationEvents.AUDITOR_REVIEW_REQUESTED)

    # --- terminal transitions: notify the creator --------------------------

    def test_approved_notifies_the_creator(self):
        plan = self._resolve("Approved", actor=self.other)

        self.assertEqual(plan.recipients, [self.user])   # self.user created the order
        self.assertEqual(plan.event_type, notif.NotificationEvents.ORDER_APPROVED)

    def test_completed_notifies_the_creator(self):
        plan = self._resolve("Completed", actor=self.other)

        self.assertEqual(plan.recipients, [self.user])
        self.assertEqual(plan.event_type, notif.NotificationEvents.ORDER_COMPLETED)

    def test_rejected_notifies_the_creator(self):
        plan = self._resolve("Rejected", actor=self.other)

        self.assertEqual(plan.recipients, [self.user])
        self.assertEqual(plan.event_type, notif.NotificationEvents.ORDER_REJECTED)

    def test_billing_rejected_asks_the_creator_to_resubmit(self):
        plan = self._resolve("Billing Rejected", actor=self.other)

        self.assertEqual(plan.recipients, [self.user])
        self.assertEqual(plan.event_type, notif.NotificationEvents.ORDER_REJECTED)
        self.assertIn("resubmit", plan.message.lower())

    def test_rejection_source_comes_from_the_previous_status(self):
        """'rejected by auditor' vs 'by approver' is inferred, not stored."""
        auditor_stage = OrderStatus.objects.create(
            code="AUDITOR_APPROVAL", name="Auditor Approval")

        from_auditor = self._resolve(
            "Rejected", actor=self.other, previous_status=auditor_stage)
        from_elsewhere = self._resolve("Rejected", actor=self.other)

        self.assertIn("auditor", from_auditor.message.lower())
        self.assertIn("approver", from_elsewhere.message.lower())

    # --- the no-broadcast rule --------------------------------------------

    def test_no_assigned_approver_notifies_nobody(self):
        """MUST NOT fall back to every approver — that leaks order details."""
        with patch.object(views_notifications, "_assigned_rate_approvers_for_order",
                          return_value=[]):
            plan = self._resolve("Rate Approval")

        self.assertEqual(plan.recipients, [])
        self.assertEqual(plan.message, "")

    def test_no_billing_user_notifies_nobody(self):
        with patch.object(views_notifications, "_billing_users_for_order", return_value=[]):
            plan = self._resolve("Billing")

        self.assertEqual(plan.recipients, [])

    def test_no_active_auditor_notifies_nobody(self):
        User.objects.filter(role=self.auditor_role).update(is_active=False)

        self.assertEqual(self._resolve("Auditor Approval").recipients, [])

    def test_order_without_a_creator_notifies_nobody(self):
        self.order.created_by = None
        self.order.save(update_fields=["created_by"])

        self.assertEqual(self._resolve("Approved").recipients, [])

    def test_unknown_status_notifies_nobody(self):
        """An unmapped status is silence, never a broadcast."""
        self.assertEqual(self._resolve("Some Future Status").recipients, [])

    # --- input handling ----------------------------------------------------

    def test_status_matching_ignores_case_and_padding(self):
        plan = self._resolve("  APPROVED  ", actor=self.other)

        self.assertEqual(plan.event_type, notif.NotificationEvents.ORDER_APPROVED)

    def test_empty_status_notifies_nobody(self):
        self.assertEqual(self._resolve("").recipients, [])
        self.assertEqual(self._resolve(None).recipients, [])
