"""Framework-level constants for the notification framework (Phase 3.2).

Pure identifiers and one naming-convention helper — nothing else. This module:

* imports NO business module (orders/payments/approvals/inventory/invoices/users);
* has no models, no database access, no network, no configuration, no behaviour.

The event names below are the stable CONTRACT that business modules will use
when they register handlers and publish events in a later phase. They are plain
strings: the framework never imports orders/payments to "know" about them.
"""

import re

# ---------------------------------------------------------------------------
# Channels the framework can eventually deliver through.
#
# Identifiers only. The providers that actually deliver on each channel arrive
# in a later phase (the dispatcher / provider phase); nothing here implements
# delivery.
# ---------------------------------------------------------------------------
CHANNEL_MOBILE_PUSH = "mobile_push"
CHANNEL_WEB_PUSH = "web_push"

CHANNELS = frozenset({CHANNEL_MOBILE_PUSH, CHANNEL_WEB_PUSH})

# ---------------------------------------------------------------------------
# Event-name contract.
#
# Convention (see is_valid_event_name): UPPERCASE, underscore-separated,
# machine-readable, independent of UI wording and of workflow status strings.
# e.g. PAYMENT_APPROVED — never "Approved", "Pending", or "Order Approved".
#
# These are the events the framework already anticipates. A business module
# will register a handler for its own events from its AppConfig.ready() in a
# later phase; Phase 3.2 only fixes the names.
# ---------------------------------------------------------------------------
ORDER_CREATED = "ORDER_CREATED"
ORDER_APPROVED = "ORDER_APPROVED"
ORDER_REJECTED = "ORDER_REJECTED"

PAYMENT_CREATED = "PAYMENT_CREATED"
PAYMENT_APPROVED = "PAYMENT_APPROVED"
PAYMENT_REJECTED = "PAYMENT_REJECTED"

DEPOSIT_RECEIVED = "DEPOSIT_RECEIVED"

# All event names the framework ships with. Extended (not rewritten) as more
# modules are wired in later phases.
EVENT_NAMES = frozenset(
    {
        ORDER_CREATED,
        ORDER_APPROVED,
        ORDER_REJECTED,
        PAYMENT_CREATED,
        PAYMENT_APPROVED,
        PAYMENT_REJECTED,
        DEPOSIT_RECEIVED,
    }
)

# Uppercase token, underscore-separated, first token starts with a letter.
_EVENT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")


def is_valid_event_name(name):
    """True if ``name`` follows the framework event-name convention.

    Non-empty, a ``str``, uppercase, underscore-separated, machine-readable.
    Used by the registry to reject malformed event names at registration time.
    """
    return isinstance(name, str) and bool(_EVENT_NAME_RE.match(name))
