"""Mobile (Expo) push provider for the notification framework (Phase 3.4).

Generic transport only: given a recipient's device tokens and a canonical
payload, POST to the Expo push service. It knows nothing about Payment/Deposit/
Order/Invoice, and imports no business module. The Expo endpoint/channel are
plain constants restated here (not imported from the Orders implementation) so
the framework stays decoupled from Orders.

Token source is a SEAM: the framework does not yet own device-token storage
(that wiring is a later phase), so ``get_tokens`` returns none by default and
mobile delivery is a safe no-op until a token source is provided. Tests override
it and mock the network — no real push is ever sent from tests.
"""

import logging

import requests

from notifications.constants import CHANNEL_MOBILE_PUSH

from .base import NotificationProvider, ProviderResult

logger = logging.getLogger("notifications")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
ANDROID_CHANNEL_ID = "default"
_TIMEOUT = 10


class MobileProvider(NotificationProvider):
    channel = CHANNEL_MOBILE_PUSH

    def get_tokens(self, recipient):
        """Resolve a recipient's active Expo push tokens via the configured
        resolver (Phase 3.6). No business module is imported — the resolver is
        loaded by dotted path from settings. Returns [] when none configured.
        """
        from .resolvers import resolve_mobile_tokens

        return resolve_mobile_tokens(recipient)

    def send(self, recipient, payload):
        tokens = [t for t in self.get_tokens(recipient) if t]
        if not tokens:
            return ProviderResult(self.channel, skipped=True)

        messages = [
            {
                "to": token,
                "title": payload.get("title") or "",
                "body": payload.get("message") or "",
                "channelId": ANDROID_CHANNEL_ID,
                "priority": "high",
                "data": payload,
            }
            for token in tokens
        ]
        try:
            response = requests.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            return ProviderResult(self.channel, delivered=len(tokens))
        except Exception as error:  # transport/HTTP failure — never raise
            return ProviderResult(
                self.channel, failed=len(tokens), error=type(error).__name__
            )
