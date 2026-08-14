"""Web Push provider for the notification framework (Phase 3.4).

Generic transport only: given a recipient's browser subscriptions and a
canonical payload, send an encrypted Web Push via ``pywebpush`` signed with the
project VAPID keys (read from settings — allowed). No business module import; no
dependency on the Orders web-push implementation.

Subscription source is a SEAM (see MobileProvider): the framework does not yet
own subscription storage, so ``get_subscriptions`` returns none by default and
web delivery is a safe no-op until a source is provided. Tests override it and
mock the network.
"""

import json
import logging

from django.conf import settings

from notifications.constants import CHANNEL_WEB_PUSH

from .base import NotificationProvider, ProviderResult

logger = logging.getLogger("notifications")
_TTL = 60


class WebProvider(NotificationProvider):
    channel = CHANNEL_WEB_PUSH

    def get_subscriptions(self, recipient):
        """Resolve a recipient's active Web Push subscriptions via the configured
        resolver (Phase 3.6). No business module is imported. Returns [] when
        none configured.
        """
        from .resolvers import resolve_web_subscriptions

        return resolve_web_subscriptions(recipient)

    def _vapid_claims(self):
        email = (getattr(settings, "VAPID_ADMIN_EMAIL", "") or "").strip()
        if not email.startswith(("mailto:", "https:")):
            email = f"mailto:{email}"
        return {"sub": email}

    def send(self, recipient, payload):
        subscriptions = [s for s in self.get_subscriptions(recipient) if s]
        if not subscriptions:
            return ProviderResult(self.channel, skipped=True)

        from pywebpush import webpush  # local import: optional dependency

        delivered = failed = 0
        last_error = ""
        data = json.dumps(payload)
        claims = dict(self._vapid_claims())
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=subscription,
                    data=data,
                    vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims=dict(claims),
                    ttl=_TTL,
                )
                delivered += 1
            except Exception as error:  # per-subscription failure — never raise
                failed += 1
                last_error = type(error).__name__
        return ProviderResult(
            self.channel, delivered=delivered, failed=failed, error=last_error
        )
