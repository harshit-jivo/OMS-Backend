"""Configured registration resolvers (Phase 3.6).

The framework must find a recipient's existing push tokens / web subscriptions
WITHOUT importing any business module. It therefore calls a resolver configured
by DOTTED PATH in settings; the concrete resolver lives outside ``notifications/``
(it may read Orders-owned tables) and is loaded lazily via ``import_string`` so
the framework never imports it — or any business module — directly.

Settings:

* ``NOTIFICATION_MOBILE_TOKEN_RESOLVER``      -> callable(user) -> [token, ...]
* ``NOTIFICATION_SUBSCRIPTION_RESOLVER`` (a.k.a.
  ``NOTIFICATION_WEB_SUBSCRIPTION_RESOLVER``) -> callable(user) -> [sub_info, ...]

Unset (or import failure) is a safe no-op: delivery is simply skipped, exactly
as in Phase 3.4. A resolver that raises is isolated and logged — it never breaks
notification creation or the business operation.
"""

import logging

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("notifications")

_MOBILE_SETTING = "NOTIFICATION_MOBILE_TOKEN_RESOLVER"
# Accept either name; the second is the historically documented one.
_WEB_SETTINGS = (
    "NOTIFICATION_WEB_SUBSCRIPTION_RESOLVER",
    "NOTIFICATION_SUBSCRIPTION_RESOLVER",
)


def _load(setting_names):
    for name in setting_names:
        path = getattr(settings, name, None)
        if path:
            try:
                return import_string(path)
            except Exception as error:  # bad dotted path — no-op, logged
                logger.error(
                    "notification resolver %s (%s) failed to import: %s",
                    name, path, error,
                )
                return None
    return None


def _resolve(setting_names, recipient, kind):
    resolver = _load(setting_names)
    if resolver is None:
        return []
    try:
        return list(resolver(recipient) or [])
    except Exception as error:  # resolver blew up — isolate it
        logger.error(
            "%s resolver failed (isolated) user_id=%s error=%s",
            kind, getattr(recipient, "pk", None), type(error).__name__,
        )
        return []


def resolve_mobile_tokens(recipient):
    """Active mobile push tokens for ``recipient`` via the configured resolver."""
    return _resolve((_MOBILE_SETTING,), recipient, "mobile token")


def resolve_web_subscriptions(recipient):
    """Active web subscriptions for ``recipient`` via the configured resolver."""
    return _resolve(_WEB_SETTINGS, recipient, "web subscription")
