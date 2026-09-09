"""URLs for the reusable notification framework's read API.

Mounted at /api/notifications/ (see OMS/urls.py).
"""

from django.urls import path

from .views import (
    FrameworkNotificationListView,
    FrameworkNotificationUnreadCountView,
)

urlpatterns = [
    path("", FrameworkNotificationListView.as_view(), name="framework-notifications"),
    path("unread-count/", FrameworkNotificationUnreadCountView.as_view(),
         name="framework-notifications-unread-count"),
    path("<int:pk>/", FrameworkNotificationListView.as_view(),
         name="framework-notification-detail"),
]
