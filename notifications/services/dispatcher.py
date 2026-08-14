"""The notification dispatcher — the reusable engine (Phase 3.4).

Public API: :func:`notify`. A business module publishes *what* happened, *who*
should receive it, and *which* entity it concerns; the framework handles
persistence, the canonical payload, delivery, channel abstraction and error
isolation.

Business-module agnostic by construction: there is no ``if payment`` /
``if order`` anywhere. The dispatcher processes ``entity`` as an opaque model
instance and ``recipients`` as opaque users; it imports no business module.

Transaction safety
------------------
* Notification RECORDS are created synchronously, so they participate in the
  caller's transaction: if the caller's ``atomic()`` block rolls back, the
  records roll back too (no notification for a rolled-back event).
* External PUSH DELIVERY is scheduled with ``transaction.on_commit``. Inside an
  atomic block it runs only after a successful commit; in autocommit it runs
  immediately. A rollback therefore delivers nothing.
"""

import logging
import time

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from notifications.constants import is_valid_event_name
from notifications.models import Notification
from notifications.providers import default_providers
from notifications.services.payloads import build_payload

logger = logging.getLogger("notifications")


def notify(*, event_type, title, message, recipients, entity=None,
           company=None, actor=None):
    """Publish a notification event.

    Parameters
    ----------
    event_type : str
        A validated event name (see ``constants.is_valid_event_name``). Business
        modules own their names; the framework only validates the contract.
    title, message : str
        Presentation supplied by the caller. The framework never infers type or
        module from this text.
    recipients : iterable of users
        Each must be an instance of the project's user model.
    entity : model instance, optional
        The business object the notification concerns. Resolved internally to
        (content_type, object_id) — the caller never builds those.
    company : Company, optional
        When given, ONLY recipients belonging to this company are notified
        (isolation); mismatched recipients are skipped. When omitted, each
        recipient's own company is used. Never trusts a client-supplied id.
    actor : user, optional
        Who triggered the event (logged, not persisted here).

    Returns the list of created :class:`Notification` records.
    """
    if not is_valid_event_name(event_type):
        raise ValueError(f"Invalid event_type: {event_type!r}")

    User = get_user_model()
    recipients = list(recipients or [])
    for recipient in recipients:
        if not isinstance(recipient, User):
            raise TypeError(
                f"recipient must be a {User.__name__} instance, "
                f"got {type(recipient).__name__}"
            )

    content_type = (
        ContentType.objects.get_for_model(type(entity)) if entity is not None else None
    )
    object_id = entity.pk if entity is not None else None

    created = []
    for recipient in recipients:
        recipient_company_id = getattr(recipient, "company_id", None)
        if company is not None:
            if recipient_company_id != company.pk:
                logger.warning(
                    "notify skipped recipient (company mismatch) "
                    "event_type=%s user_id=%s",
                    event_type, recipient.pk,
                )
                continue
            company_id = company.pk
        else:
            company_id = recipient_company_id

        notification = Notification.objects.create(
            user=recipient,
            company_id=company_id,
            event_type=event_type,
            title=title or "",
            message=message or "",
            content_type=content_type,
            object_id=object_id,
            is_read=False,
        )
        created.append(notification)

    if created:
        payloads = [(n, build_payload(n)) for n in created]
        transaction.on_commit(lambda: _deliver(payloads, event_type))

    logger.info(
        "notify created event_type=%s recipients=%s created=%s actor_id=%s",
        event_type, len(recipients), len(created),
        getattr(actor, "pk", None),
    )
    return created


def _deliver(payloads, event_type):
    """Fan out delivery across providers, fully isolated.

    One recipient's or provider's failure never stops the others, and this never
    raises — a push failure must not turn the business operation into a 500.
    """
    providers = default_providers()
    for notification, payload in payloads:
        for provider in providers:
            started = time.monotonic()
            channel = getattr(provider, "channel", "?")
            try:
                result = provider.send(notification.user, payload)
                if result.skipped:
                    outcome = "skipped"
                elif result.failed == 0:
                    outcome = "ok"
                elif result.delivered == 0:
                    outcome = "failed"
                else:
                    outcome = "partial"
                logger.info(
                    "notify delivery event_type=%s notification_id=%s user_id=%s "
                    "channel=%s outcome=%s delivered=%s failed=%s duration_ms=%s",
                    event_type, notification.id, notification.user_id, channel,
                    outcome, result.delivered, result.failed,
                    round((time.monotonic() - started) * 1000),
                )
            except Exception as error:  # provider blew up — isolate it
                logger.error(
                    "notify delivery error (isolated) event_type=%s "
                    "notification_id=%s user_id=%s channel=%s error=%s",
                    event_type, notification.id, notification.user_id, channel,
                    type(error).__name__,
                )
