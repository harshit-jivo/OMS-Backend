"""Tests for device tracking.

Views are exercised directly via APIRequestFactory + force_authenticate, so the
tests cover the view/serializer/service stack without depending on middleware or
the live database (the test runner uses a throwaway DB).
"""
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import UserDevice
from .services import register_device, touch_last_active
from .utils import is_valid_device_id, parse_browser
from .views import (
    CurrentDevicesView,
    DeviceRegisterView,
    DeviceUpdateView,
)

User = get_user_model()

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEVICE_ID = "11111111-2222-4333-8444-555555555555"


class UtilTests(TestCase):
    def test_parse_browser_chrome(self):
        self.assertEqual(parse_browser(CHROME_UA), ("Chrome", "120.0.0.0"))

    def test_parse_browser_edge_not_chrome(self):
        edge = CHROME_UA + " Edg/120.0.0.0"
        self.assertEqual(parse_browser(edge)[0], "Edge")

    def test_parse_browser_unknown_is_blank(self):
        self.assertEqual(parse_browser("something-weird"), ("", ""))

    def test_device_id_validation(self):
        self.assertTrue(is_valid_device_id(DEVICE_ID))
        self.assertFalse(is_valid_device_id("bad id!"))
        self.assertFalse(is_valid_device_id("short"))


class DeviceApiTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(username="alice", password="x")
        self.other = User.objects.create_user(username="bob", password="x")

    def _register_payload(self, **overrides):
        payload = {
            "device_id": DEVICE_ID,
            "platform": "WEB",
            "app_type": "WEB",
            "app_version": "1.0.2",
            "build_number": 42,
            "timezone": "Asia/Kolkata",
            "language": "en-IN",
        }
        payload.update(overrides)
        return payload

    def test_register_creates_then_upserts(self):
        req = self.factory.post("/api/devices/register/", self._register_payload(), format="json", HTTP_USER_AGENT=CHROME_UA)
        force_authenticate(req, user=self.user)
        resp = DeviceRegisterView.as_view()(req)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["success"])
        # Browser derived server-side from UA (web platform).
        self.assertEqual(resp.data["data"]["browser_name"], "Chrome")
        self.assertEqual(UserDevice.objects.filter(user=self.user).count(), 1)

        # Second call = same row, 200, build bumped.
        req2 = self.factory.post("/api/devices/register/", self._register_payload(build_number=43), format="json", HTTP_USER_AGENT=CHROME_UA)
        force_authenticate(req2, user=self.user)
        resp2 = DeviceRegisterView.as_view()(req2)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(UserDevice.objects.filter(user=self.user).count(), 1)
        self.assertEqual(UserDevice.objects.get(user=self.user).build_number, 43)

    def test_register_rejects_bad_device_id(self):
        req = self.factory.post("/api/devices/register/", self._register_payload(device_id="bad!"), format="json")
        force_authenticate(req, user=self.user)
        resp = DeviceRegisterView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("device_id", resp.data["errors"])

    def test_register_rejects_bad_platform(self):
        req = self.factory.post("/api/devices/register/", self._register_payload(platform="SYMBIAN"), format="json")
        force_authenticate(req, user=self.user)
        resp = DeviceRegisterView.as_view()(req)
        self.assertEqual(resp.status_code, 400)

    def test_register_requires_auth(self):
        req = self.factory.post("/api/devices/register/", self._register_payload(), format="json")
        resp = DeviceRegisterView.as_view()(req)
        self.assertEqual(resp.status_code, 401)

    def test_same_device_id_different_users_are_two_rows(self):
        for u in (self.user, self.other):
            data = self._register_payload()
            data.update(browser_name="", browser_version="")
            register_device(u, data)
        self.assertEqual(UserDevice.objects.filter(device_id=DEVICE_ID).count(), 2)

    def test_update_existing_and_404(self):
        register_device(self.user, self._register_payload() | {"browser_name": "", "browser_version": ""})
        req = self.factory.put("/api/devices/update/", {"device_id": DEVICE_ID, "app_version": "1.1.0", "build_number": 50}, format="json")
        force_authenticate(req, user=self.user)
        resp = DeviceUpdateView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(UserDevice.objects.get(user=self.user).build_number, 50)

        # Unknown device -> 404, never creates.
        req404 = self.factory.put("/api/devices/update/", {"device_id": "99999999-2222-4333-8444-555555555555", "build_number": 9}, format="json")
        force_authenticate(req404, user=self.user)
        self.assertEqual(DeviceUpdateView.as_view()(req404).status_code, 404)

    def test_me_lists_only_callers_devices(self):
        register_device(self.user, self._register_payload() | {"browser_name": "", "browser_version": ""})
        register_device(self.other, self._register_payload() | {"browser_name": "", "browser_version": ""})
        req = self.factory.get("/api/devices/me/")
        force_authenticate(req, user=self.user)
        resp = CurrentDevicesView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["device_id"], DEVICE_ID)

    def test_last_active_throttled(self):
        device, _ = register_device(self.user, self._register_payload() | {"browser_name": "", "browser_version": ""})
        # register just primed the throttle, so an immediate touch is suppressed.
        self.assertFalse(touch_last_active(device))


class UrlTests(TestCase):
    """The client-facing device routes stay reachable at their published paths."""

    def test_url_reverse(self):
        self.assertTrue(reverse("device-register").endswith("/devices/register/"))
        self.assertTrue(reverse("device-update").endswith("/devices/update/"))
        self.assertTrue(reverse("device-me").endswith("/devices/me/"))

    def test_release_routes_are_gone(self):
        """Release management was removed; these names must not come back.

        Pins the deletion: the app is the source of truth for its own version,
        so there is no release policy to serve or curate.
        """
        for name in ("app-version", "admin-release-list", "admin-release-detail"):
            with self.assertRaises(NoReverseMatch):
                reverse(name)

    def test_apprelease_model_is_gone(self):
        with self.assertRaises(LookupError):
            apps.get_model("devices", "AppRelease")


class AdminApiTests(TestCase):
    """Admin device API."""

    def setUp(self):
        from users.models import UserRole

        from .admin_views import (
            AdminDeviceAnalyticsView,
            AdminDeviceDetailView,
            AdminDeviceListView,
        )

        self.views = {
            "list": AdminDeviceListView.as_view(),
            "detail": AdminDeviceDetailView.as_view(),
            "analytics": AdminDeviceAnalyticsView.as_view(),
        }
        self.factory = APIRequestFactory()

        admin_role = UserRole.objects.create(name="admin", display_name="Admin")
        staff_role = UserRole.objects.create(name="manager", display_name="Manager")
        self.admin = User.objects.create_user(
            username="boss", password="x", name="The Boss", role=admin_role
        )
        self.plain = User.objects.create_user(
            username="worker", password="x", name="A Worker", role=staff_role
        )

        now = timezone.now()
        UserDevice.objects.create(
            user=self.plain, device_id=DEVICE_ID, platform="ANDROID",
            app_type="MOBILE", app_version="1.0.0", build_number=40,
            os_name="Android", os_version="14", device_name="Pixel",
            first_login=now, last_login=now, last_active=now,
        )
        UserDevice.objects.create(
            user=self.admin, device_id="22222222-2222-4333-8444-555555555555",
            platform="WEB", app_type="WEB", app_version="1.2.0", build_number=58,
            browser_name="Chrome", os_name="Windows", os_version="10.0",
            first_login=now, last_login=now, last_active=now,
        )

    # --- permissions ------------------------------------------------------
    def test_non_admin_is_forbidden(self):
        for key in ("list", "analytics"):
            request = self.factory.get("/api/admin/x/")
            force_authenticate(request, user=self.plain)
            self.assertEqual(self.views[key](request).status_code, 403, key)

    def test_anonymous_is_rejected(self):
        request = self.factory.get("/api/admin/devices/")
        self.assertEqual(self.views["list"](request).status_code, 401)

    def test_admin_can_list(self):
        request = self.factory.get("/api/admin/devices/")
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["pagination"]["total"], 2)
        # The owning user is inlined, not duplicated onto the device table.
        self.assertIn("username", response.data["data"]["results"][0])

    # --- filtering / search / pagination ----------------------------------
    def test_filter_by_platform(self):
        request = self.factory.get("/api/admin/devices/", {"platform": "WEB"})
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)

    def test_search_by_user_name(self):
        request = self.factory.get("/api/admin/devices/", {"search": "Worker"})
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)

    def test_pagination_is_server_side(self):
        request = self.factory.get("/api/admin/devices/", {"page_size": 1, "page": 2})
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(len(response.data["data"]["results"]), 1)
        self.assertEqual(response.data["data"]["pagination"]["total_pages"], 2)

    def test_page_size_is_capped(self):
        request = self.factory.get("/api/admin/devices/", {"page_size": 9999})
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(response.data["data"]["pagination"]["page_size"], 100)

    def test_bad_ordering_falls_back(self):
        request = self.factory.get("/api/admin/devices/", {"ordering": "user__password"})
        force_authenticate(request, user=self.admin)
        self.assertEqual(self.views["list"](request).status_code, 200)

    # --- analytics --------------------------------------------------------
    def test_analytics_cards_and_charts(self):
        request = self.factory.get("/api/admin/devices/analytics/")
        force_authenticate(request, user=self.admin)
        response = self.views["analytics"](request)
        self.assertEqual(response.status_code, 200)
        cards = response.data["data"]["cards"]
        self.assertEqual(cards["total_devices"], 2)
        self.assertEqual(cards["android_devices"], 1)
        self.assertEqual(cards["web_devices"], 1)
        self.assertEqual(cards["desktop_browsers"], 1)
        # Nothing here compares against a "latest" — the cards only ever count
        # what the devices themselves reported.
        for gone in ("outdated_devices", "on_latest_devices", "latest_releases"):
            self.assertNotIn(gone, cards)
        charts = response.data["data"]["charts"]
        self.assertEqual(len(charts["devices_by_last_seen"]), 14)
        self.assertTrue(charts["version_distribution"])

    # --- device detail ----------------------------------------------------
    def test_device_detail_and_404(self):
        device = UserDevice.objects.first()
        request = self.factory.get("/api/admin/devices/x/")
        force_authenticate(request, user=self.admin)
        self.assertEqual(self.views["detail"](request, pk=device.pk).status_code, 200)
        self.assertEqual(self.views["detail"](request, pk=999999).status_code, 404)

    # --- derived activity status ------------------------------------------
    def _make_device(self, minutes_ago=None, days_ago=None):
        now = timezone.now()
        when = now - (
            timedelta(days=days_ago) if days_ago else timedelta(minutes=minutes_ago or 0)
        )
        return UserDevice.objects.create(
            user=self.plain,
            device_id=f"dev-{minutes_ago}-{days_ago}-{when.timestamp()}",
            platform="WEB", app_type="WEB", app_version="1.0.0", build_number=1,
            first_login=when, last_login=when, last_active=when,
        )

    def test_compute_status_buckets(self):
        from .status import (
            STATUS_IDLE,
            STATUS_INACTIVE,
            STATUS_OFFLINE,
            STATUS_ONLINE,
            compute_status,
        )

        now = timezone.now()
        self.assertEqual(compute_status(now - timedelta(minutes=1), now), STATUS_ONLINE)
        self.assertEqual(compute_status(now - timedelta(minutes=10), now), STATUS_IDLE)
        self.assertEqual(compute_status(now - timedelta(hours=2), now), STATUS_OFFLINE)
        self.assertEqual(compute_status(now - timedelta(days=45), now), STATUS_INACTIVE)
        # A device that somehow has no activity timestamp is inactive, not a crash.
        self.assertEqual(compute_status(None, now), STATUS_INACTIVE)

    def test_status_boundaries_are_exclusive(self):
        from .status import STATUS_IDLE, STATUS_ONLINE, compute_status

        now = timezone.now()
        # Exactly on the 5-minute edge counts as idle, not online.
        self.assertEqual(compute_status(now - timedelta(minutes=5, seconds=1), now), STATUS_IDLE)
        self.assertEqual(compute_status(now - timedelta(minutes=4, seconds=59), now), STATUS_ONLINE)

    def test_status_filter_and_counts_agree(self):
        UserDevice.objects.all().delete()
        self._make_device(minutes_ago=1)     # online
        self._make_device(minutes_ago=10)    # idle
        self._make_device(minutes_ago=120)   # offline
        self._make_device(days_ago=45)       # inactive

        for wanted in ("online", "idle", "offline", "inactive"):
            request = self.factory.get("/api/admin/devices/", {"status": wanted})
            force_authenticate(request, user=self.admin)
            response = self.views["list"](request)
            self.assertEqual(response.data["data"]["pagination"]["total"], 1, wanted)
            # The row's own badge value must match the bucket we filtered on.
            self.assertEqual(response.data["data"]["results"][0]["status"], wanted)

        request = self.factory.get("/api/admin/devices/analytics/")
        force_authenticate(request, user=self.admin)
        counts = self.views["analytics"](request).data["data"]["cards"]["status_counts"]
        self.assertEqual(counts, {"online": 1, "idle": 1, "offline": 1, "inactive": 1})
        # Buckets are exhaustive: they must account for every device.
        self.assertEqual(sum(counts.values()), UserDevice.objects.count())

    def test_unknown_status_is_ignored_not_an_error(self):
        request = self.factory.get("/api/admin/devices/", {"status": "banana"})
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["pagination"]["total"], 2)

    def test_sort_by_user_name_is_allowed(self):
        request = self.factory.get("/api/admin/devices/", {"ordering": "user__name"})
        force_authenticate(request, user=self.admin)
        response = self.views["list"](request)
        self.assertEqual(response.status_code, 200)
        names = [row["user_name"] for row in response.data["data"]["results"]]
        self.assertEqual(names, sorted(names))
