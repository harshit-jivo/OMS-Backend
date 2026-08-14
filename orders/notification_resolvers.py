"""Registration resolvers for the generic notification framework (Phase 3.6).

Bridges the notification framework's provider seams to the existing user-level
device registration storage (``push_tokens`` / ``web_push_subscriptions``)
WITHOUT the framework importing Orders.

Dependency direction:

    notifications  ->  (settings dotted path)  ->  this module  ->  orders.models

Only THIS module imports the Orders models; the framework stays unaware of
Orders and calls these by the dotted paths in
``settings.NOTIFICATION_MOBILE_TOKEN_RESOLVER`` /
``settings.NOTIFICATION_WEB_SUBSCRIPTION_RESOLVER``.

These functions are strictly READ-ONLY: they never create, update or delete a
registration, and they add no business-module fields. Tokens/subscriptions
belong to users/devices, not to Orders/Payments/Deposits — this module simply
reads the store that already exists. It is intentionally a NEW file so the
existing Orders notification implementation is left completely untouched.
"""

from .models import PushToken, WebPushSubscription


def resolve_mobile_tokens(user):
    """Active Expo push tokens for ``user`` (a list of token strings).

    Mirrors the existing Orders resolution (``filter(user=user,
    is_active=True)``): a user's own, active registrations only.
    """
    return list(
        PushToken.objects.filter(user=user, is_active=True)
        .values_list("token", flat=True)
        .distinct()
    )


def resolve_web_subscriptions(user):
    """Active Web Push subscriptions for ``user`` as subscription_info dicts.

    Each item is the shape pywebpush expects:
    ``{"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}``.
    """
    return [
        {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
        for subscription in WebPushSubscription.objects.filter(
            user=user, is_active=True
        )
    ]
