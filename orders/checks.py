"""Startup validation for Web Push (VAPID) configuration.

A browser ``PushSubscription`` is permanently bound to the application server
key (VAPID public key) it was created with. If the server later signs with a
private key from a *different* pair, every existing subscription is rejected
forever -- FCM answers ``403`` ("the VAPID credentials in the authorization
header do not correspond to the credentials used to create the subscriptions")
and WNS answers a bare ``401``.

The dangerous part is that a mismatched pair looks completely healthy at boot:
tokens are signed fine, requests are sent fine, and the failure only shows up as
per-subscription rejections later. These checks turn that silent, delayed
breakage into a loud, immediate startup error.

Registered from ``OrdersConfig.ready()`` so they run on ``manage.py check``,
``runserver``, and deploys.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register

VAPID_MISMATCH_ID = "orders.E001"
VAPID_UNVERIFIABLE_ID = "orders.W001"


def _derive_public_key(private_key: str) -> str:
    """base64url public key derived from a raw VAPID private key."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid

    vapid = Vapid.from_raw(private_key.encode("utf-8"))
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@register()
def check_vapid_keypair(app_configs, **kwargs):
    """Fail fast if VAPID_PUBLIC_KEY isn't the public half of VAPID_PRIVATE_KEY."""
    public_key = (getattr(settings, "VAPID_PUBLIC_KEY", "") or "").strip()
    private_key = (getattr(settings, "VAPID_PRIVATE_KEY", "") or "").strip()

    # settings.py already refuses to boot with missing keys when DEBUG=False.
    if not public_key or not private_key:
        return []

    try:
        derived = _derive_public_key(private_key)
    except Exception as error:  # unreadable key / library missing
        return [
            Warning(
                f"Could not verify the VAPID key pair: {error}",
                hint=(
                    "VAPID_PRIVATE_KEY should be a raw base64url key, as printed "
                    "by `python manage.py generate_vapid_keys`."
                ),
                id=VAPID_UNVERIFIABLE_ID,
            )
        ]

    if derived != public_key:
        return [
            Error(
                "VAPID_PUBLIC_KEY is not the public half of VAPID_PRIVATE_KEY.",
                hint=(
                    "Web Push would fail for every browser (FCM: 403, WNS: 401), "
                    "because subscriptions are bound to the public key they were "
                    "created with. Set a matching pair in .env -- generate one "
                    "with `python manage.py generate_vapid_keys`. Never edit only "
                    "one of the two values."
                ),
                id=VAPID_MISMATCH_ID,
            )
        ]

    return []
