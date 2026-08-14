"""Delivery providers for the notification framework.

A new channel (Email, WhatsApp, SMS, …) is added by writing a provider that
implements :class:`~notifications.providers.base.NotificationProvider` and
listing it in :func:`default_providers` — the dispatcher and business modules do
not change.
"""

from .base import NotificationProvider, ProviderResult
from .mobile import MobileProvider
from .web import WebProvider

__all__ = [
    "NotificationProvider",
    "ProviderResult",
    "MobileProvider",
    "WebProvider",
    "default_providers",
]


def default_providers():
    """The providers the dispatcher fans a notification out to.

    Kept as a function (not a module constant) so a later phase can make it
    configurable / queue-backed without changing callers.
    """
    return [MobileProvider(), WebProvider()]
