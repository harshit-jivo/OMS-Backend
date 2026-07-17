"""Service/repository layer for device management.

All device writes go through here so the views stay thin and the write rules
(idempotent upsert, throttled last_active, per-user scoping) live in one place
and can be reused by later phases (e.g. hooking register into login).

The user is ALWAYS passed in by the caller from ``request.user`` (the JWT
subject) and never taken from client input.
"""
from django.core.cache import cache
from django.utils import timezone

from .models import UserDevice

# last_active is a hot column. We coalesce writes to at most one per device per
# window via the cache, so a burst of requests from one device is a single DB
# update. NOTE: the project's default cache is per-process LocMemCache, so this
# throttle is best-effort per worker (a 4-worker deploy may write up to 4x per
# window). That is acceptable for a "last seen ~within 15 min" signal; switch
# CACHES to Redis for exactness. See settings.py cache notes.
LAST_ACTIVE_THROTTLE_SECONDS = 15 * 60

# Mutable telemetry fields refreshed on every register/update. Identity fields
# (user, device_id, first_login) are set once and never in this set.
_MUTABLE_FIELDS = (
    "platform",
    "app_type",
    "app_version",
    "build_number",
    "device_name",
    "manufacturer",
    "device_model",
    "os_name",
    "os_version",
    "browser_name",
    "browser_version",
    "language",
    "timezone",
)


def _throttle_key(device_pk):
    return f"devices:last_active:{device_pk}"


def register_device(user, data):
    """Idempotent upsert of the caller's device, keyed on (user, device_id).

    ``data`` is validated/derived field data (device_id + the mutable fields).
    Returns ``(device, created)``. Safe to call on every login — the same call
    twice is one row.
    """
    now = timezone.now()
    device_id = data["device_id"]

    defaults = {field: data.get(field) for field in _MUTABLE_FIELDS if field in data}
    defaults.update(last_login=now, last_active=now, is_active=True)

    device, created = UserDevice.objects.get_or_create(
        user=user,
        device_id=device_id,
        defaults={**defaults, "first_login": now},
    )

    if not created:
        for field, value in defaults.items():
            setattr(device, field, value)
        device.save(update_fields=[*defaults.keys(), "updated_at"])

    # We just wrote last_active; prime the throttle so an immediate heartbeat
    # doesn't write again.
    cache.set(_throttle_key(device.pk), 1, LAST_ACTIVE_THROTTLE_SECONDS)
    return device, created


def update_device(user, device_id, data):
    """Refresh mutable telemetry for an already-registered device.

    Returns the updated :class:`UserDevice`, or ``None`` if the caller has no
    device with that id (update never creates — registration is the only
    creator). Touches last_active directly (an explicit update is a real signal).
    """
    try:
        device = UserDevice.objects.get(user=user, device_id=device_id)
    except UserDevice.DoesNotExist:
        return None

    changed = []
    for field in _MUTABLE_FIELDS:
        if field in data:
            setattr(device, field, data[field])
            changed.append(field)

    device.last_active = timezone.now()
    changed.append("last_active")
    device.save(update_fields=[*changed, "updated_at"])
    cache.set(_throttle_key(device.pk), 1, LAST_ACTIVE_THROTTLE_SECONDS)
    return device


def get_current_device(user, device_id):
    """Return the caller's device with ``device_id``, or ``None``."""
    return UserDevice.objects.filter(user=user, device_id=device_id).first()


def list_devices(user, include_inactive=False):
    """Return the caller's devices, newest-active first."""
    qs = UserDevice.objects.filter(user=user)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("-last_active")


def touch_last_active(device):
    """Throttled bump of ``last_active``.

    Returns True if a DB write happened, False if suppressed by the throttle.
    Intended for a per-request heartbeat in a later phase; kept here so the
    write rule lives with the other device writes.
    """
    key = _throttle_key(device.pk)
    if cache.get(key):
        return False
    cache.set(key, 1, LAST_ACTIVE_THROTTLE_SECONDS)
    UserDevice.objects.filter(pk=device.pk).update(last_active=timezone.now())
    return True
