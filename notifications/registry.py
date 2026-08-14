"""Notification event registry — the framework side of the module→framework seam.

This mirrors the project's established registration pattern. The approvals
engine keeps a module-level ``_HOOKS`` dict (``approvals/services.py``) that each
domain app populates from its ``AppConfig.ready()`` (e.g.
``payments/apps.py`` → ``payments/hooks.py:register()``). That inversion lets the
engine drive domain behaviour without importing the domain app — no circular
dependency. The notification framework reuses exactly that idea, keyed by event
name instead of document label.

Phase 3.2 provides ONLY the registry foundation:

* it imports NO business module;
* it has no models, no database access, no network, no delivery, no background
  work — a plain in-memory dict guarded by validation;
* it is empty after startup. Business modules register their handlers in a
  later phase; what a handler *does* (the dispatcher contract) is also a later
  phase and is deliberately left unspecified here.
"""

from .constants import is_valid_event_name

# event_name (str) -> handler (callable). Populated by business modules from
# their AppConfig.ready() in a later phase; empty at startup today.
_REGISTRY = {}


def register(event_name, handler):
    """Register ``handler`` for ``event_name``.

    Called from a business module's ``AppConfig.ready()`` in a later phase.
    Deterministic and side-effect-free beyond the dict write. Raises on a
    malformed event name, a non-callable handler, or a duplicate registration,
    so wiring mistakes fail loudly at startup rather than silently.
    """
    if not is_valid_event_name(event_name):
        raise ValueError(f"Invalid notification event name: {event_name!r}")
    if not callable(handler):
        raise TypeError(f"handler for {event_name!r} must be callable")
    if event_name in _REGISTRY:
        raise ValueError(f"event {event_name!r} is already registered")
    _REGISTRY[event_name] = handler


def get_handler(event_name):
    """Return the handler registered for ``event_name``, or ``None``."""
    return _REGISTRY.get(event_name)


def is_registered(event_name):
    """Whether a handler is registered for ``event_name``."""
    return event_name in _REGISTRY


def registered_events():
    """Return the set of currently-registered event names (a snapshot)."""
    return frozenset(_REGISTRY)


def clear():
    """Empty the registry.

    Test-only helper so tests that register handlers stay isolated. Not used by
    production startup.
    """
    _REGISTRY.clear()
