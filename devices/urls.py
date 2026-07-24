"""URL routes for device tracking.

Mounted at ``api/`` in the project URLconf, so the full paths are
``/api/devices/...`` (client) and ``/api/admin/devices/...`` (admin).
"""
from django.urls import path

from .admin_views import (
    AdminDeviceAnalyticsView,
    AdminDeviceDetailView,
    AdminDeviceListView,
)
from .views import (
    CurrentDevicesView,
    DeviceRegisterView,
    DeviceUpdateView,
)

urlpatterns = [
    # --- client endpoints ----------------------------------------------------
    path("devices/register/", DeviceRegisterView.as_view(), name="device-register"),
    path("devices/update/", DeviceUpdateView.as_view(), name="device-update"),
    path("devices/me/", CurrentDevicesView.as_view(), name="device-me"),

    # --- admin endpoints -----------------------------------------------------
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
]
