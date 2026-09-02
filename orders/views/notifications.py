"""Notification views and recipient resolution.

The first domain lifted out of `orders/views.py` (plan item 3.1). It was chosen
first because it DEPENDED on the rest rather than the reverse: resolving who to
notify pulled in the user-scoping helpers now in `._shared`, while the order
flow needs only `send_order_notifications` back from here. That direction is
what makes the cut possible without a circular import.

Note for tests: `mock.patch` replaces a name in the module that LOOKS IT UP, so
patch targets here are `orders.views.notifications.<name>`, NOT
`orders.views.<name>`. The package re-exports these names, but patching the
re-exported copy would not affect the callers in this module — the test would
pass while stubbing nothing.
"""
from urllib import request
from orders.serializers import NotificationSerializer
from orders.models import Notification, PushToken, WebPushSubscription
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from sap_sync.models import Party as SapParty
from users.models import User
from django.db import transaction
import logging
from orders.notifications import NotificationEvents, NotificationPlan, NotificationTemplates, NotificationTypes, deactivate_push_token, deliver_notification_to_many
from orders.webpush import get_vapid_public_key
from core.deprecation import deprecated

from ._shared import (
    _assigned_rate_approvers_for_order,
    _build_iexact_filter,
    _get_user_category_names,
    _get_user_main_group_names,
)

logger = logging.getLogger(__name__)




def _billing_users_for_order(order, exclude_user=None):
    users = User.objects.filter(role__name__iexact='billing', is_active=True)
    if exclude_user:
        users = users.exclude(id=exclude_user.id)

    order_categories = {
        str(category or '').strip().upper()
        for category in order.items.exclude(category__isnull=True)
        .exclude(category='')
        .values_list('category', flat=True)
    }

    matching_users = []
    for user in users:
        user_categories = _get_user_category_names(user)
        if user_categories and not set(user_categories).intersection(order_categories):
            continue

        user_main_groups = _get_user_main_group_names(user)
        if user_main_groups:
            party_match = SapParty.objects.filter(
                card_code=order.card_code,
            )
            if user_categories:
                party_match = party_match.filter(category__in=user_categories)
            party_match = party_match.filter(
                _build_iexact_filter('main_group', user_main_groups)
            )
            if not party_match.exists():
                continue

        matching_users.append(user)

    return matching_users
    
def _display_user_name(user):
    if not user:
        return "Unknown"
    return getattr(user, "name", None) or getattr(user, "username", "Unknown")

def _order_rate_approval_payload(order):
    return [
        {
            'id': approval.id,
            'approver': approval.approver_id,
            'approver_name': _display_user_name(approval.approver),
            'status': approval.status,
            'remarks': approval.remarks or '',
            'approved_at': approval.approved_at,
            'created_at': approval.created_at,
        }
        for approval in order.rate_approvals.select_related('approver').all()
    ]

def _active_users_with_role(role_name, exclude_user=None):
    """Return active users holding ``role_name`` (case-insensitive).

    Used to resolve role-scoped recipients (e.g. auditors) for the next
    workflow action. This targets a specific role -- it is never a broadcast
    to all users.
    """
    users = User.objects.filter(role__name__iexact=role_name, is_active=True)
    if exclude_user:
        users = users.exclude(id=exclude_user.id)
    return list(users)


def _resolve_notification_recipients(order, status_name, actor, previous_status):
    """Determine *who* should be notified for a status transition and *what*
    message they should receive.

    Returns a :class:`NotificationPlan` (recipients, message, title,
    event_type, notification_type). ``recipients`` is a list of ``User``
    objects -- always the exact user(s) responsible for the next action, never
    a broadcast to all users. An empty plan (no recipients) means "notify no
    one".

    Business rule (Task 3): if a required recipient is missing due to
    configuration, we log the issue and return no recipients rather than
    falling back to notifying everyone -- this avoids leaking order details to
    unrelated users.
    """
    creator = order.created_by
    creator_name = _display_user_name(creator)
    actor_name = _display_user_name(actor)
    normalized_status = (status_name or "").strip().lower()
    previous_name = (getattr(previous_status, "name", "") or "").strip().lower()

    _empty = NotificationPlan([], "", "", None, None)

    # --- Forward transitions: notify the users owning the NEXT action --------
    if normalized_status in {"rate approval", "need approval"}:
        approvers = _assigned_rate_approvers_for_order(
            order, status_filter="PENDING", exclude_user=actor
        )
        if not approvers:
            logger.warning(
                "Notification skipped: order %s reached '%s' with no assigned rate "
                "approver. Not broadcasting to all approvers.",
                order.order_number,
                status_name,
            )
            return _empty
        return NotificationPlan(
            approvers,
            NotificationTemplates.rate_approval_needed(order.order_number, creator_name),
            NotificationTemplates.TITLE_RATE_APPROVAL_REQUIRED,
            NotificationEvents.RATE_APPROVAL_REQUESTED,
            NotificationTypes.APPROVAL,
        )

    if normalized_status in {"billing", "billing pending", "billing approval"}:
        billing_users = _billing_users_for_order(order, exclude_user=actor)
        if not billing_users:
            logger.warning(
                "Notification skipped: no eligible billing user resolved for order %s.",
                order.order_number,
            )
            return _empty
        return NotificationPlan(
            billing_users,
            NotificationTemplates.billing_ready(order.order_number, creator_name),
            NotificationTemplates.TITLE_BILLING_REQUIRED,
            NotificationEvents.BILLING_REQUESTED,
            NotificationTypes.BILLING,
        )

    if normalized_status == "auditor approval":
        auditors = _active_users_with_role("auditor", exclude_user=actor)
        if not auditors:
            logger.warning(
                "Notification skipped: no active auditor configured for order %s.",
                order.order_number,
            )
            return _empty
        return NotificationPlan(
            auditors,
            NotificationTemplates.auditor_review(order.order_number, creator_name),
            NotificationTemplates.TITLE_AUDITOR_REVIEW_REQUIRED,
            NotificationEvents.AUDITOR_REVIEW_REQUESTED,
            NotificationTypes.APPROVAL,
        )

    # --- Terminal transitions: notify the order creator ----------------------
    if normalized_status in {"billing rejected", "rejected", "completed", "approved"}:
        if not creator:
            logger.warning(
                "Notification skipped: order %s has no creator to notify for status '%s'.",
                order.order_number,
                status_name,
            )
            return _empty

        if normalized_status == "billing rejected":
            return NotificationPlan(
                [creator],
                NotificationTemplates.billing_rejected(order.order_number, actor_name),
                NotificationTemplates.TITLE_ORDER_REJECTED,
                NotificationEvents.ORDER_REJECTED,
                NotificationTypes.REJECTION,
            )
        if normalized_status == "rejected":
            source = "auditor" if "auditor" in previous_name else "approver"
            return NotificationPlan(
                [creator],
                NotificationTemplates.order_rejected(order.order_number, source, actor_name),
                NotificationTemplates.TITLE_ORDER_REJECTED,
                NotificationEvents.ORDER_REJECTED,
                NotificationTypes.REJECTION,
            )
        if normalized_status == "completed":
            return NotificationPlan(
                [creator],
                NotificationTemplates.order_completed(order.order_number, actor_name),
                NotificationTemplates.TITLE_ORDER_COMPLETED,
                NotificationEvents.ORDER_COMPLETED,
                NotificationTypes.COMPLETION,
            )
        # approved
        return NotificationPlan(
            [creator],
            NotificationTemplates.order_approved(order.order_number, actor_name),
            NotificationTemplates.TITLE_ORDER_APPROVED,
            NotificationEvents.ORDER_APPROVED,
            NotificationTypes.APPROVAL,
        )

    return _empty


def send_order_notifications(order, status_name, actor=None, previous_status=None):
    """Route an order status transition to the exact user(s) responsible for
    the next action.

    Responsibilities are separated:
      * recipient + message resolution -> ``_resolve_notification_recipients``
      * persistence + Expo push        -> ``notifications.deliver_notification``

    Recipients and message text are unchanged from Phase 1; the resolver now
    additionally supplies structured push metadata (title/event_type/
    notification_type) so mobile can deep-link from the payload (Task 3).
    """
    plan = _resolve_notification_recipients(
        order, status_name, actor, previous_status
    )
    if not plan.recipients or not plan.message:
        return

    # Deferred to after the transaction commits. Two reasons, and the first is
    # what made `UpdateOrderStatusView` unsafe to lock at all:
    #
    # 1. `deliver_notification_to_many` ends in `requests.post(..., timeout=15)`
    #    to Expo plus a web-push call — per recipient. Running that inside a
    #    transaction that holds `select_for_update()` on the order would park a
    #    row lock behind outbound HTTP to a third party, so one slow push would
    #    block every other approver on that order. The race fix is only safe
    #    because the network calls are out here.
    #
    # 2. A notification sent inside a transaction that then rolls back is a
    #    message about something that never happened. This view rolls its own
    #    status changes back on several paths.
    #
    # `on_commit` runs the callback immediately when no transaction is active,
    # so callers outside an atomic block are unaffected. Under `TestCase`
    # (which never commits) it does NOT run — tests asserting delivery need
    # `self.captureOnCommitCallbacks(execute=True)`.
    transaction.on_commit(lambda: deliver_notification_to_many(
        plan.recipients,
        order,
        plan.message,
        event_type=plan.event_type,
        notification_type=plan.notification_type,
        title=plan.title,
    ))

@deprecated(
    successor='/api/v1/notifications/',
    note=(
        'Superseded by the reusable `notifications` app. This view reads '
        '`orders.Notification` (table `notifications`), which plan item 3.5 '
        'retires in favour of `notifications.Notification` (table '
        '`notifications_notification`). No sunset date is set: the mobile '
        'client still calls this, and the date belongs to whoever owns that '
        'migration. The header and the usage log exist so the decision can be '
        'made from evidence rather than from a guess.'
    ),
)
class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = (
            Notification.objects
            .filter(user=request.user)
            .select_related('order')
            .order_by('-created_at')[:50]
        )
        serializer = NotificationSerializer(notifications, many=True)
        return Response(serializer.data)

    def post(self, request):
        # Mark all as read
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"message": "All notifications marked as read"})

    def patch(self, request, pk=None):
        # Mark single as read
        if pk:
            try:
                notification = Notification.objects.get(id=pk, user=request.user)
                notification.is_read = True
                notification.save()
                return Response({"message": "Notification marked as read"})
            except Notification.DoesNotExist:
                return Response({"error": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"error": "No ID provided"}, status=status.HTTP_400_BAD_REQUEST)

class PushTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = (request.data.get('token') or request.data.get('push_token') or '').strip()
        platform = (request.data.get('platform') or '').strip()

        if not token:
            return Response({'error': 'Push token is required'}, status=status.HTTP_400_BAD_REQUEST)

        PushToken.objects.update_or_create(
            token=token,
            defaults={
                'user': request.user,
                'platform': platform,
                'is_active': True,
            },
        )
        return Response({'success': True, 'message': 'Push token registered'})

    def delete(self, request):
        """Deactivate the caller's push token (e.g. on logout).

        Optional endpoint -- older app versions that never call it are
        unaffected. Scoped to ``request.user`` so a client can only disable its
        own token, and the row is retained (is_active=False) rather than
        deleted so history/analytics stay intact.
        """
        token = (request.data.get('token') or request.data.get('push_token') or '').strip()
        if not token:
            return Response({'error': 'Push token is required'}, status=status.HTTP_400_BAD_REQUEST)

        deactivate_push_token(request.user, token)
        return Response({'success': True, 'message': 'Push token deactivated'})


class WebPushPublicKeyView(APIView):
    """Expose the VAPID public (application server) key the browser needs to
    create a Web Push subscription. The public key is not secret."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'public_key': get_vapid_public_key()})


class WebPushSubscriptionView(APIView):
    """Register or remove a browser Web Push subscription for the caller.

    Reuses the :class:`WebPushSubscription` model. Multiple browsers/devices per
    user are supported (one row per endpoint). Never touches mobile tokens.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = request.data.get('subscription') or request.data
        endpoint = (subscription.get('endpoint') or '').strip()
        keys = subscription.get('keys') or {}
        p256dh = (keys.get('p256dh') or '').strip()
        auth = (keys.get('auth') or '').strip()

        if not endpoint or not p256dh or not auth:
            return Response(
                {'error': 'endpoint, keys.p256dh and keys.auth are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:255]

        # update_or_create on the unique endpoint prevents duplicates and
        # re-homes an endpoint to the current user if it moved.
        WebPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                'user': request.user,
                'p256dh': p256dh,
                'auth': auth,
                'user_agent': user_agent,
                'is_active': True,
            },
        )
        return Response({'success': True, 'message': 'Web push subscription saved'})

    def delete(self, request):
        subscription = request.data.get('subscription') or request.data
        endpoint = (subscription.get('endpoint') or '').strip()
        if not endpoint:
            return Response(
                {'error': 'endpoint is required'}, status=status.HTTP_400_BAD_REQUEST
            )

        WebPushSubscription.objects.filter(
            user=request.user, endpoint=endpoint
        ).update(is_active=False)
        return Response({'success': True, 'message': 'Web push subscription removed'})


@deprecated(
    successor='/api/v1/notifications/',
    note=(
        'Superseded by the reusable `notifications` app. This is the paginated '
        'history variant of the same legacy read path as `NotificationListView` '
        '-- it also reads `orders.Notification` (table `notifications`), which '
        'plan item 3.5 retires in favour of `notifications.Notification` (table '
        '`notifications_notification`). No sunset date is set: the web client '
        'still calls this, and the date belongs to whoever owns that migration. '
        'The header and the usage log exist so the decision can be made from '
        'evidence rather than from a guess.'
    ),
)
class NotificationHistoryView(APIView):
    """Paginated notification history (Phase 3, Task 9).

    Unlike the legacy list endpoint (latest 50, used by mobile), this returns
    the full history with limit/offset pagination and an unread count so the
    web UI can render Unread / Read / Today / Yesterday / Older groups and
    infinite scroll. Read-state grouping by date is done client-side from
    ``created_at``.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = int(request.query_params.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = int(request.query_params.get('offset', 0))
        except (TypeError, ValueError):
            offset = 0

        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        status_filter = (request.query_params.get('filter') or 'all').lower()

        base = Notification.objects.filter(user=request.user).select_related('order')
        if status_filter == 'unread':
            base = base.filter(is_read=False)
        elif status_filter == 'read':
            base = base.filter(is_read=True)

        total = base.count()
        unread_count = Notification.objects.filter(
            user=request.user, is_read=False
        ).count()

        page = base.order_by('-created_at')[offset:offset + limit]
        serializer = NotificationSerializer(page, many=True)

        next_offset = offset + limit if (offset + limit) < total else None
        return Response({
            'results': serializer.data,
            'count': total,
            'unread_count': unread_count,
            'next_offset': next_offset,
        })
