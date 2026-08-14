"""Public service API for the notification framework.

A business module integrates by calling::

    from notifications.services import notify

    notify(
        event_type=MY_EVENT,
        title=...,
        message=...,
        recipients=[...],
        entity=my_object,      # optional
        company=my_company,    # optional
        actor=request.user,    # optional
    )

Everything else (persistence, ContentType/object_id, canonical payload, push
providers) is internal.
"""

from notifications.services.dispatcher import notify

__all__ = ["notify"]
