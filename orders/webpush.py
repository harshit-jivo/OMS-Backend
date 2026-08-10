"""Browser Web Push delivery for the ``orders`` app (Phase 3).

Mirrors the fire-and-forget shape of the existing Expo mobile push: the backend
POSTs an encrypted payload to each browser's push endpoint. It does NOT require
Redis, Channels, or an ASGI server -- it runs on the current WSGI stack.

Kept separate from the mobile push code (``notifications.send_push_notification``)
so the two channels never interfere (Task 6).
"""

import json
import logging
import time

from django.conf import settings

from .models import WebPushSubscription

logger = logging.getLogger(__name__)

# HTTP statuses a push service returns for a permanently dead subscription.
#   404/410 -- the browser unsubscribed / the endpoint no longer exists.
#   403/401 -- the row was created against a DIFFERENT application server key
#              (the VAPID pair was rotated, or it predates the real keys and was
#              made with the dev fallback). A subscription is permanently bound
#              to the key it was created with, so this can never succeed again.
#              Services disagree on the code: FCM answers 403 ("the VAPID
#              credentials ... do not correspond to the credentials used to
#              create the subscriptions"), while WNS (Edge) answers a bare 401.
#
# Deactivating is safe even if a 401 were ever transient (e.g. clock skew on the
# VAPID JWT): the subscribe endpoint upserts with is_active=True, so a browser
# that is still healthy simply re-registers and switches the row back on.
_GONE_STATUSES = {401, 403, 404, 410}

# Lazily-built Vapid signer (cached across calls).
_vapid_instance = None


def _get_vapid():
    global _vapid_instance
    if _vapid_instance is None:
        from py_vapid import Vapid

        _vapid_instance = Vapid.from_raw(settings.VAPID_PRIVATE_KEY.encode("utf-8"))
    return _vapid_instance


def _vapid_claims():
    # VAPID "sub" must be a mailto: (or https:) URI. Accept the admin email with
    # or without an explicit "mailto:" prefix so either .env style works.
    email = (settings.VAPID_ADMIN_EMAIL or "").strip()
    if not email.startswith(("mailto:", "https:")):
        email = f"mailto:{email}"
    return {"sub": email}


def get_vapid_public_key():
    """Public (application server) key the browser needs to subscribe."""
    return settings.VAPID_PUBLIC_KEY


# Delivery outcomes for a single subscription.
DELIVERED = "delivered"  # push accepted by the service
DEACTIVATED = "deactivated"  # endpoint permanently dead -> row switched off
ERROR = "error"  # transient failure -> row left active, retried next time

# A tiny data-only payload used to *validate* a subscription without showing the
# user anything. The service worker recognises this type and returns without
# calling showNotification (see public/service-worker.js).
KEEPALIVE_PAYLOAD = {"type": "__keepalive__"}


def deliver_web_push(subscription, payload):
    """Send one encrypted push and report the outcome.

    Returns one of :data:`DELIVERED`, :data:`DEACTIVATED`, :data:`ERROR`.

    On a permanent failure (see ``_GONE_STATUSES``: the browser unsubscribed,
    the endpoint expired, or the row is bound to an old VAPID key) the row is
    switched off immediately so we never target it again. Transient errors are
    logged and swallowed so one bad endpoint never breaks a batch.

    This is the single place a dead subscription is retired -- used by live
    notification sends AND the nightly cleanup, so both behave identically.
    """
    from pywebpush import WebPushException, webpush

    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }

    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=_get_vapid(),
            vapid_claims=dict(_vapid_claims()),
            ttl=60,
        )
        return DELIVERED
    except WebPushException as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code in _GONE_STATUSES:
            WebPushSubscription.objects.filter(pk=subscription.pk).update(
                is_active=False
            )
            logger.info(
                "Web push subscription deactivated (gone) id=%s user_id=%s status=%s",
                subscription.pk,
                subscription.user_id,
                status_code,
            )
            return DEACTIVATED
        logger.warning(
            "Web push failed id=%s user_id=%s status=%s error=%s",
            subscription.pk,
            subscription.user_id,
            status_code,
            error,
        )
        return ERROR
    except Exception as error:  # never let push break the request flow
        logger.error("Unexpected web push error id=%s: %s", subscription.pk, error)
        return ERROR


def send_web_push(subscription, payload):
    """Backward-compatible wrapper: ``True`` only when the push was delivered."""
    return deliver_web_push(subscription, payload) == DELIVERED


def send_web_push_to_user(user, payload):
    """Send ``payload`` to every active browser subscription of ``user``.

    Returns the number of successful deliveries. Dead endpoints (404/410, and a
    stale-key 401/403) are deactivated on the spot and delivery continues to the
    remaining subscriptions -- the same immediate self-healing the Expo mobile
    path has. Safe no-op when the user has no web subscriptions.
    """
    if not user:
        return 0

    subscriptions = list(
        WebPushSubscription.objects.filter(user=user, is_active=True)
    )
    if not subscriptions:
        return 0

    started = time.monotonic()
    delivered = 0
    removed = 0
    for subscription in subscriptions:
        outcome = deliver_web_push(subscription, payload)
        if outcome == DELIVERED:
            delivered += 1
        elif outcome == DEACTIVATED:
            removed += 1

    elapsed_ms = int((time.monotonic() - started) * 1000)
    failed = len(subscriptions) - delivered - removed
    # Logged on every send, not only when a subscription was retired: without
    # this there was no record that web push ran at all for a healthy user, so
    # "did the browser get it?" was unanswerable from the logs.
    logger.info(
        "push channel=webpush outcome=%s notification_id=%s user_id=%s "
        "order_id=%s event_type=%s subscriptions=%s delivered=%s "
        "deactivated=%s failed=%s duration_ms=%s",
        "ok" if failed == 0 else "partial",
        payload.get("notification_id"),
        user.id,
        payload.get("order_id"),
        payload.get("event_type"),
        len(subscriptions),
        delivered,
        removed,
        failed,
        elapsed_ms,
    )
    return delivered
