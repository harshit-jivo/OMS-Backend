"""URL routes for device & version management.

Mounted at ``api/`` in the project URLconf, so the full paths are
``/api/devices/...`` and ``/api/app/version/``. Two nouns (devices, app) share
one module because they are one feature; the paths below are explicit.
"""
from django.urls import path

from .admin_views import (
    AdminDeviceAnalyticsView,
    AdminDeviceDetailView,
    AdminDeviceListView,
    AdminReleaseDetailView,
    AdminReleaseListCreateView,
)
from .views import (
    AppVersionView,
    CurrentDevicesView,
    DeviceRegisterView,
    DeviceUpdateView,
)

urlpatterns = [
    # --- client endpoints (Phase 2) -----------------------------------------
    path("devices/register/", DeviceRegisterView.as_view(), name="device-register"),
    path("devices/update/", DeviceUpdateView.as_view(), name="device-update"),
    path("devices/me/", CurrentDevicesView.as_view(), name="device-me"),
    path("app/version/", AppVersionView.as_view(), name="app-version"),

    # --- admin endpoints (Phase 5) ------------------------------------------
    # Note: these live under /api/admin/... and do not collide with Django's
    # own /admin/ site, which is mounted at the project root.
    path(
        "admin/devices/analytics/",
        AdminDeviceAnalyticsView.as_view(),
        name="admin-device-analytics",
    ),
    path("admin/devices/", AdminDeviceListView.as_view(), name="admin-device-list"),
    path(
        "admin/devices/<int:pk>/",
        AdminDeviceDetailView.as_view(),
        name="admin-device-detail",
    ),
    path(
        "admin/releases/",
        AdminReleaseListCreateView.as_view(),
        name="admin-release-list",
    ),
    path(
        "admin/releases/<int:pk>/",
        AdminReleaseDetailView.as_view(),
        name="admin-release-detail",
    ),
]
