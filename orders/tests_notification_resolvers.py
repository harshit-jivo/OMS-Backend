"""Phase 3.6 — tests for the Orders-side notification registration resolvers.

A NEW test file (the existing Orders notification suite,
``orders/tests_notifications.py``, is untouched). These verify the read-only
resolvers return only a user's OWN, ACTIVE registrations from the existing
``push_tokens`` / ``web_push_subscriptions`` tables.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from orders.models import PushToken, WebPushSubscription
from orders.notification_resolvers import (
    resolve_mobile_tokens,
    resolve_web_subscriptions,
)


class NotificationResolverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.u1 = User.objects.create(username="rsv_u1", name="U1")
        cls.u2 = User.objects.create(username="rsv_u2", name="U2")

        PushToken.objects.create(user=cls.u1, token="TOK-U1-ACTIVE", platform="android", is_active=True)
        PushToken.objects.create(user=cls.u1, token="TOK-U1-INACTIVE", platform="android", is_active=False)
        PushToken.objects.create(user=cls.u2, token="TOK-U2-ACTIVE", platform="ios", is_active=True)

        WebPushSubscription.objects.create(
            user=cls.u1, endpoint="https://ep/u1-active", p256dh="p1", auth="a1", is_active=True)
        WebPushSubscription.objects.create(
            user=cls.u1, endpoint="https://ep/u1-inactive", p256dh="p1", auth="a1", is_active=False)
        WebPushSubscription.objects.create(
            user=cls.u2, endpoint="https://ep/u2-active", p256dh="p2", auth="a2", is_active=True)

    # 1 + 2 mobile: active only
    def test_mobile_returns_active_only(self):
        self.assertEqual(resolve_mobile_tokens(self.u1), ["TOK-U1-ACTIVE"])

    # 3 mobile: user isolation
    def test_mobile_user_isolation(self):
        self.assertNotIn("TOK-U2-ACTIVE", resolve_mobile_tokens(self.u1))
        self.assertEqual(resolve_mobile_tokens(self.u2), ["TOK-U2-ACTIVE"])

    # 4 + 5 web: active only, correct shape
    def test_web_returns_active_only(self):
        subs = resolve_web_subscriptions(self.u1)
        self.assertEqual([s["endpoint"] for s in subs], ["https://ep/u1-active"])
        self.assertEqual(subs[0]["keys"], {"p256dh": "p1", "auth": "a1"})

    # 6 web: user isolation
    def test_web_user_isolation(self):
        endpoints = [s["endpoint"] for s in resolve_web_subscriptions(self.u1)]
        self.assertNotIn("https://ep/u2-active", endpoints)

    def test_user_with_no_registrations(self):
        User = get_user_model()
        lonely = User.objects.create(username="rsv_lonely", name="Lonely")
        self.assertEqual(resolve_mobile_tokens(lonely), [])
        self.assertEqual(resolve_web_subscriptions(lonely), [])
