"""Read API for the reusable notification framework.

Mirrors the shape of ``orders.views.NotificationListView`` so the mobile app can
reuse the SAME notification page for framework (Payment/Deposit/…) notifications:

    GET    /api/notifications/          -> the caller's notifications, newest first
    GET    /api/notifications/unread-count/ -> {"unread": N}
    POST   /api/notifications/          -> mark ALL the caller's unread as read
    PATCH  /api/notifications/<pk>/     -> mark ONE as read

Permission model: every row is scoped to ``user=request.user``. A notification
only exists for the recipient it was created for (the current-level approver or
the submitter), so filtering by the caller IS the per-user/per-permission
scoping — there is no separate "who may see this" rule, and none is needed.
"""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer


class FrameworkNotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = (
            Notification.objects
            .filter(user=request.user)
            .select_related("content_type")
            .order_by("-created_at")[:50]
        )
        return Response(NotificationSerializer(notifications, many=True).data)

    def post(self, request):
        Notification.objects.filter(
            user=request.user, is_read=False
        ).update(is_read=True)
        return Response({"message": "All notifications marked as read"})

    def patch(self, request, pk=None):
        if not pk:
            return Response({"error": "No ID provided"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            notification = Notification.objects.get(id=pk, user=request.user)
        except Notification.DoesNotExist:
            return Response({"error": "Notification not found"},
                            status=status.HTTP_404_NOT_FOUND)
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])
        return Response({"message": "Notification marked as read"})


class FrameworkNotificationUnreadCountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        unread = Notification.objects.filter(
            user=request.user, is_read=False
        ).count()
        return Response({"unread": unread})
