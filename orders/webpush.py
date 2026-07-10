"""Browser Web Push delivery for the ``orders`` app (Phase 3).

Mirrors the fire-and-forget shape of the existing Expo mobile push: the backend
POSTs an encrypted payload to each browser's push endpoint. It does NOT require
Redis, Channels, or an ASGI server -- it runs on the current WSGI stack.

Kept separate from the mobile push code (``notifications.send_push_notification``)
so the two channels never interfere (Task 6).
"""

import json
import logging

from django.conf import settings

from .models import WebPushSubscription

logger = logging.getLogger(__name__)

# HTTP statuses a push service returns for a permanently dead subscription.
_GONE_STATUSES = {404, 410}

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


def send_web_push(subscription, payload):
    """Send one encrypted push to a single :class:`WebPushSubscription`.

    Returns ``True`` on success. On a permanent failure (404/410 -- the browser
    unsubscribed) the row is deactivated so we stop targeting it. Transient
    errors are logged and swallowed so one bad endpoint never breaks a batch.
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
        return True
    except WebPushException as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code in _GONE_STATUSES:
            WebPushSubscription.objects.filter(pk=subscription.pk).update(is_active=False)
            logger.info(
                "Web push subscription deactivated (gone) id=%s status=%s",
                subscription.pk,
                status_code,
            )
        else:
            logger.warning(
                "Web push failed id=%s status=%s error=%s",
                subscription.pk,
                status_code,
                error,
            )
        return False
    except Exception as error:  # never let push break the request flow
        logger.error("Unexpected web push error id=%s: %s", subscription.pk, error)
        return False


def send_web_push_to_user(user, payload):
    """Send ``payload`` to every active browser subscription of ``user``.

    Returns the number of successful deliveries. Safe no-op when the user has no
    web subscriptions (e.g. mobile-only users).
    """
    if not user:
        return 0

    subscriptions = list(
        WebPushSubscription.objects.filter(user=user, is_active=True)
    )
    if not subscriptions:
        return 0

    delivered = 0
    for subscription in subscriptions:
        if send_web_push(subscription, payload):
            delivered += 1
    return delivered
