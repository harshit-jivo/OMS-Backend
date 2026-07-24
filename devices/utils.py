"""Helpers for device registration — User-Agent parsing and device-id checks.

The browser/OS parsing here is a deliberately small, dependency-free heuristic.
It covers the browsers this org's users actually run and degrades gracefully to
empty strings for anything it doesn't recognise (browser is telemetry, so an
unknown UA must never break registration). If richer or more accurate parsing is
needed later, swap in a maintained library (e.g. ``ua-parser``) behind these
same function signatures -- see the Phase 3 follow-up notes.
"""
import re

# Order matters: Edge/Opera/Samsung UAs all also contain "Chrome", and Chrome's
# UA also contains "Safari", so the more specific patterns must be tried first.
_BROWSER_PATTERNS = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/([\d.]+)")),
    ("Opera", re.compile(r"OPR/([\d.]+)")),
    ("Samsung Internet", re.compile(r"SamsungBrowser/([\d.]+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/([\d.]+)")),
    ("Firefox", re.compile(r"(?:Firefox|FxiOS)/([\d.]+)")),
    ("Safari", re.compile(r"Version/([\d.]+).*Safari")),
)

_OS_PATTERNS = (
    ("Windows", re.compile(r"Windows NT ([\d.]+)")),
    ("Android", re.compile(r"Android ([\d.]+)")),
    ("iOS", re.compile(r"(?:iPhone|iPad); CPU (?:iPhone )?OS ([\d_]+)")),
    ("macOS", re.compile(r"Mac OS X ([\d_]+)")),
    ("Linux", re.compile(r"(Linux)")),
)


def parse_browser(user_agent):
    """Return ``(browser_name, browser_version)`` from a UA string.

    Both empty when the UA is missing or unrecognised.
    """
    ua = user_agent or ""
    for name, pattern in _BROWSER_PATTERNS:
        match = pattern.search(ua)
        if match:
            return name, match.group(1)
    return "", ""


def parse_os(user_agent):
    """Return ``(os_name, os_version)`` from a UA string (best-effort).

    Used only as a fallback for web clients that don't self-report the OS;
    native clients send os_name/os_version explicitly.
    """
    ua = user_agent or ""
    for name, pattern in _OS_PATTERNS:
        match = pattern.search(ua)
        if match:
            version = match.group(1).replace("_", ".") if name != "Linux" else ""
            return name, version
    return "", ""


# A permissive shape check: reject obvious garbage (unbounded/rich text) without
# being so strict that a legitimate UUID library variant is refused. The real
# guarantee we need is "bounded and not free-form", not "canonical UUIDv4".
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def is_valid_device_id(value):
    """True if ``value`` is a plausible client-generated device id."""
    return bool(value and _DEVICE_ID_RE.match(value))
