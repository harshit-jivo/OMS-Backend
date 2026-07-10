"""Notification domain module for the ``orders`` app.

This module centralises the *delivery* side of the order-notification pipeline
so that view code only has to worry about *who* should be notified and *why*.

Responsibilities are deliberately separated:

    * :class:`NotificationTemplates` -- generate the user-facing message text
      (Task 4: centralised, reusable message templates).
    * :func:`save_notification` -- persist a ``Notification`` record.
    * :func:`send_push_notification` -- deliver the existing Expo push message.
    * :func:`deliver_notification` -- convenience wrapper: save + push.
    * :func:`mark_order_notifications_read` -- mark an order's notifications read.

Recipient *resolution* stays in ``views.py`` (see ``send_order_notifications``)
because it depends on view-local business helpers. This module never imports
from ``views.py``, keeping the dependency graph acyclic.

NOTE (Task 7): the Expo push behaviour is intentionally preserved byte-for-byte
-- same endpoint, same payload shape, same headers, same timeout. Only the
``print`` calls were replaced with structured logging.
"""

import logging
from collections import namedtuple

import requests

from .models import Notification, PushToken

logger = logging.getLogger(__name__)

# Expo push service endpoint. Preserved exactly as the previous implementation.
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
# Screen the mobile app should deep-link to when a push is tapped.
NOTIFICATION_SCREEN = "notifications"
# Android channel id -- must match the channel the mobile app registers
# (see OMS-app/src/services/notification.service.ts). Kept as "default" so
# that already-installed clients continue to display pushes (Task 9).
ANDROID_CHANNEL_ID = "default"
# Fallback push heading if a caller does not supply an event-specific title.
DEFAULT_PUSH_TITLE = "Order update"


class NotificationEvents:
    """Stable, structured event identifiers sent in the push payload.

    The mobile app can branch on ``event_type`` (specific) or
    ``notification_type`` (coarse category) instead of parsing message text.
    These are additive metadata -- older clients simply ignore them.
    """

    RATE_APPROVAL_REQUESTED = "RATE_APPROVAL_REQUESTED"
    BILLING_REQUESTED = "BILLING_REQUESTED"
    AUDITOR_REVIEW_REQUESTED = "AUDITOR_REVIEW_REQUESTED"
    ORDER_APPROVED = "ORDER_APPROVED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_COMPLETED = "ORDER_COMPLETED"


# Coarse notification categories, useful for grouping / future routing.
class NotificationTypes:
    APPROVAL = "approval"
    BILLING = "billing"
    REJECTION = "rejection"
    COMPLETION = "completion"


# A resolved routing decision: who to notify, what to say, and the structured
# metadata that rides along in the push payload. Returned by the view-layer
# recipient resolver and consumed by :func:`deliver_notification_to_many`.
NotificationPlan = namedtuple(
    "NotificationPlan",
    ["recipients", "message", "title", "event_type", "notification_type"],
)


class NotificationTemplates:
    """Centralised, reusable notification message templates (Task 4).

    Keeping the copy in one place means future wording changes -- or eventual
    localisation -- happen here rather than being scattered through the views.

    The strings are intentionally identical to the previous inline messages so
    that existing notification behaviour is unchanged.
    """

    # --- Push headings (title). The body always stays the message below, so
    # navigation/logic never depends on the title text. ----------------------
    TITLE_RATE_APPROVAL_REQUIRED = "Rate approval required"
    TITLE_BILLING_REQUIRED = "Billing required"
    TITLE_AUDITOR_REVIEW_REQUIRED = "Auditor review required"
    TITLE_ORDER_APPROVED = "Order approved"
    TITLE_ORDER_REJECTED = "Order rejected"
    TITLE_ORDER_COMPLETED = "Order completed"

    @staticmethod
    def rate_approval_needed(order_number, creator_name):
        return f"Order {order_number} from {creator_name} needs your rate approval."

    @staticmethod
    def billing_ready(order_number, creator_name):
        return f"Order {order_number} from {creator_name} is ready for billing."

    @staticmethod
    def auditor_review(order_number, creator_name):
        return f"Order {order_number} from {creator_name} is ready for auditor review."

    @staticmethod
    def billing_rejected(order_number, actor_name):
        return (
            f"Order {order_number} was rejected by billing ({actor_name}). "
            "Please edit and resubmit."
        )

    @staticmethod
    def order_rejected(order_number, source, actor_name):
        return f"Order {order_number} was rejected by {source} ({actor_name})."

    @staticmethod
    def order_completed(order_number, actor_name):
        return f"Order {order_number} has been completed by auditor ({actor_name})."

    @staticmethod
    def order_approved(order_number, actor_name):
        return f"Order {order_number} has been approved by {actor_name}."


def mark_order_notifications_read(order, user):
    """Mark every unread notification for ``order``/``user`` as read."""
    if not order or not user:
        return
    Notification.objects.filter(order=order, user=user, is_read=False).update(is_read=True)


def save_notification(user, order, message):
    """Persist a single notification record.

    Returns the created :class:`Notification`, or ``None`` when any required
    field is missing (guards against notifying a "null" recipient).
    """
    if not user or not order or not message:
        return None

    notification = Notification.objects.create(user=user, order=order, message=message)
    logger.info(
        "Notification created id=%s user_id=%s order_id=%s",
        notification.id,
        user.id,
        order.id,
    )
    return notification


def _build_push_data(notification, event_type=None, notification_type=None, title=None):
    """Assemble the structured ``data`` block for a push message.

    Existing keys (``notification_id``, ``order_id``, ``screen``) are always
    present for backward compatibility (Task 9). New keys are additive and let
    the app navigate from structured fields rather than message text (Task 3).
    """
    created_at = getattr(notification, "created_at", None)
    return {
        # --- existing keys (do not remove -- older clients rely on these) ---
        "notification_id": notification.id,
        "order_id": notification.order_id,
        "screen": NOTIFICATION_SCREEN,
        # --- new, optional structured fields --------------------------------
        "notification_type": notification_type,
        "event_type": event_type,
        "title": title or DEFAULT_PUSH_TITLE,
        "message": notification.message,
        "timestamp": created_at.isoformat() if created_at else None,
    }


def send_push_notification(
    user, notification, event_type=None, notification_type=None, title=None
):
    """Send the Expo push notification for a saved ``notification``.

    The Expo transport (endpoint, sound, channel, priority, timeout) is
    preserved exactly (Task 7). ``title`` and the structured ``data`` fields
    are additive enhancements.
    """
    tokens = list(
        PushToken.objects.filter(user=user, is_active=True)
        .values_list("token", flat=True)
        .distinct()
    )
    if not tokens:
        logger.info(
            "Expo push skipped: no active tokens user_id=%s notification_id=%s",
            user.id,
            notification.id,
        )
        return

    data = _build_push_data(notification, event_type, notification_type, title)
    payload = [
        {
            "to": token,
            "title": title or DEFAULT_PUSH_TITLE,
            "body": notification.message,
            "sound": "default",
            "channelId": ANDROID_CHANNEL_ID,
            "priority": "high",
            "data": data,
        }
        for token in tokens
        if token
    ]
    if not payload:
        return

    try:
        response = requests.post(
            EXPO_PUSH_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Accept-encoding": "gzip, deflate",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        try:
            response_data = response.json()
        except ValueError:
            response_data = {"raw": response.text}

        if response.status_code >= 400:
            logger.error(
                "Expo push notification failed status=%s response=%s",
                response.status_code,
                response_data,
            )
            return

        ticket_errors = [
            ticket
            for ticket in response_data.get("data", [])
            if ticket.get("status") != "ok"
        ]
        if response_data.get("errors") or ticket_errors:
            logger.error(
                "Expo push notification ticket errors user_id=%s notification_id=%s errors=%s ticket_errors=%s",
                user.id,
                notification.id,
                response_data.get("errors", []),
                ticket_errors,
            )
            return

        logger.info(
            "Expo push notification accepted user_id=%s notification_id=%s token_count=%s",
            user.id,
            notification.id,
            len(tokens),
        )
    except requests.RequestException as error:
        logger.error("Expo push notification error: %s", error)


def build_notification_payload(
    notification, event_type=None, notification_type=None, title=None,
    order_number=None,
):
    """Flat payload shared by mobile (Expo ``data``) and browser Web Push.

    A single source of truth so both channels navigate from the same structured
    fields (order_id/event_type) rather than parsing message text.
    """
    created_at = getattr(notification, "created_at", None)
    return {
        "notification_id": notification.id,
        "order_id": notification.order_id,
        "order_number": order_number,
        "screen": NOTIFICATION_SCREEN,
        "notification_type": notification_type,
        "event_type": event_type,
        "title": title or DEFAULT_PUSH_TITLE,
        "message": notification.message,
        "body": notification.message,
        "timestamp": created_at.isoformat() if created_at else None,
    }


def deliver_notification(
    user, order, message, event_type=None, notification_type=None, title=None
):
    """Save a notification record and fan it out to every channel the user has.

    This is the single entry point view code uses to notify one user. It
    persists the record, then best-effort delivers to:
      * Expo mobile push  (existing behaviour, unchanged)
      * browser Web Push  (Phase 3, additive)

    A failure in one channel never blocks the other or the request.
    """
    notification = save_notification(user, order, message)
    if notification is None:
        return None

    # Mobile (unchanged).
    send_push_notification(
        user,
        notification,
        event_type=event_type,
        notification_type=notification_type,
        title=title,
    )

    # Browser Web Push (independent of mobile; safe no-op for mobile-only users).
    try:
        from .webpush import send_web_push_to_user

        payload = build_notification_payload(
            notification,
            event_type,
            notification_type,
            title,
            order_number=getattr(order, "order_number", None),
        )
        send_web_push_to_user(user, payload)
    except Exception as error:  # never let web push break delivery
        logger.error("Web push dispatch failed notification_id=%s: %s", notification.id, error)

    return notification


def deliver_notification_to_many(
    users, order, message, exclude_user=None, event_type=None,
    notification_type=None, title=None,
):
    """Deliver the same ``message`` to each resolved recipient in ``users``."""
    exclude_id = getattr(exclude_user, "id", None)
    for user in users:
        if exclude_id is not None and getattr(user, "id", None) == exclude_id:
            continue
        deliver_notification(
            user,
            order,
            message,
            event_type=event_type,
            notification_type=notification_type,
            title=title,
        )


def deactivate_push_token(user, token):
    """Deactivate a device's push token (e.g. on logout).

    Reuses the existing :class:`PushToken` model -- no schema change. Scoped to
    the requesting ``user`` so a client can only disable its own token. Returns
    the number of tokens deactivated.
    """
    if not user or not token:
        return 0
    updated = PushToken.objects.filter(user=user, token=token, is_active=True).update(
        is_active=False
    )
    logger.info(
        "Push token deactivated user_id=%s count=%s", getattr(user, "id", None), updated
    )
    return updated
