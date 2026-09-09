"""The ONE canonical payload builder — shared by every channel (Phase 3.4).

There is a single serialized representation of a notification. Mobile and Web
providers deliver the same dict; there are no per-channel duplicate builders.

The entity is represented GENERICALLY as ``entity_type`` + ``entity_id`` (from
the GenericForeignKey), never as ``order_id``/``payment_id``/etc. Clients route
by ``(entity_type, entity_id)``.
"""


def build_payload(notification):
    """Build the canonical payload dict for one :class:`Notification`."""
    content_type = notification.content_type
    return {
        "notification_id": notification.id,
        "event_type": notification.event_type,
        "title": notification.title,
        "message": notification.message,
        # Generic entity reference (e.g. entity_type="paymentreceipt", entity_id=123).
        "entity_type": content_type.model if content_type else None,
        "entity_id": notification.object_id,
        "company_id": notification.company_id,
        # Generic navigation hint: clients open the relevant screen using the
        # entity_type/entity_id above, not a module-specific id.
        "screen": "notification",
    }
