"""Provider boundary for the notification framework (Phase 3.4).

A provider delivers a canonical payload to ONE recipient over ONE channel. It
knows nothing about business modules — only ``(recipient, payload)``. New
channels (Email, WhatsApp, SMS) are added by implementing this interface; the
dispatcher and business modules never change.

Contract: ``send()`` MUST NOT raise for an ordinary delivery failure — it
captures the outcome in a :class:`ProviderResult`. The dispatcher additionally
isolates any unexpected exception so one provider can never break the others.
"""

from dataclasses import dataclass


@dataclass
class ProviderResult:
    """Outcome of one provider delivering to one recipient."""

    channel: str
    delivered: int = 0  # sub-deliveries that succeeded (per token/subscription)
    failed: int = 0  # sub-deliveries that failed
    skipped: bool = False  # nothing to deliver to (recipient has no endpoint)
    error: str = ""  # short error label, never sensitive content


class NotificationProvider:
    """Base class for a single-channel delivery provider."""

    channel = None  # one of notifications.constants.CHANNEL_*

    def send(self, recipient, payload):
        """Deliver ``payload`` (a canonical payload dict) to ``recipient`` (a
        user). Return a :class:`ProviderResult`; do not raise on a delivery
        failure."""
        raise NotImplementedError
