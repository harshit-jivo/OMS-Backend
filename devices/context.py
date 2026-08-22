"""Resolve the device behind an authenticated request, for audit trails.

Both clients already send ``X-Device-Id`` on *every* request (the web app wires
it into the single axios interceptor -- see webDeviceService.getHeaders), so any
view can name the device an action came from without a new client contract.

What this returns is a **snapshot**, not a foreign key. An audit line must keep
reading "approved from Ravi's Galaxy Tab" even after that device is renamed,
deactivated, or the user row is deleted -- so the label is resolved once, at
write time, and copied into the audit row as plain text.

Caveat for anyone leaning on this: ``device_id`` is client-generated telemetry,
not an authentication factor. It *corroborates* who acted (same browser profile,
same install, same machine as their other actions); the JWT is what proves it.
Treat a mismatch as a lead worth investigating, never as proof on its own.
"""
from .models import UserDevice
from .utils import is_valid_device_id, parse_browser, parse_os

# Kept in step with InvocieHistory.device_name's max_length.
MAX_DEVICE_NAME = 150


def _label_from_device(device):
    """Best human-readable name for a registered device.

    Preference order mirrors what an operator would recognise on a screen: the
    name the device calls itself, then its hardware identity, then the browser
    it ran in, then the bare platform.
    """
    if device.device_name:
        return device.device_name

    hardware = " ".join(part for part in (device.manufacturer, device.device_model) if part)
    if hardware:
        return hardware

    browser = " ".join(part for part in (device.browser_name, device.browser_version) if part)
    os_label = " ".join(part for part in (device.os_name, device.os_version) if part)
    if browser and os_label:
        return f"{browser} on {os_label}"
    if browser or os_label:
        return browser or os_label

    return f"{device.platform}/{device.app_type}"


def _label_from_user_agent(user_agent):
    """Fallback label for a request whose device was never registered."""
    browser_name, browser_version = parse_browser(user_agent)
    os_name, os_version = parse_os(user_agent)

    browser = " ".join(part for part in (browser_name, browser_version) if part)
    os_label = " ".join(part for part in (os_name, os_version) if part)
    if browser and os_label:
        return f"{browser} on {os_label}"
    return browser or os_label or ""


def describe_request_device(request):
    """Return ``{'device_id': ..., 'device_name': ...}`` for the caller.

    Both values are always present and always strings -- empty when the client
    sent no usable header and the User-Agent told us nothing. Audit writes must
    never fail because telemetry was missing, so every branch here degrades to
    an empty string rather than raising.
    """
    device_id = (request.META.get("HTTP_X_DEVICE_ID") or "").strip()
    if not is_valid_device_id(device_id):
        device_id = ""

    device_name = ""
    user = getattr(request, "user", None)
    if device_id and user is not None and getattr(user, "is_authenticated", False):
        # Scoped to the caller, matching the (user, device_id) uniqueness rule:
        # a forged header can only ever name the forger's own device.
        device = UserDevice.objects.filter(user=user, device_id=device_id).first()
        if device is not None:
            device_name = _label_from_device(device)

    if not device_name:
        device_name = _label_from_user_agent(request.META.get("HTTP_USER_AGENT", ""))

    return {"device_id": device_id, "device_name": device_name[:MAX_DEVICE_NAME]}
