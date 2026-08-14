"""Persistence for the reusable notification framework (Phase 3.3).

This is a NEW, independent notification system for Payments, Deposits and future
modules. It is deliberately SEPARATE from the old Orders notification tables
(`notifications`, `push_tokens`, `web_push_subscriptions`), which stay exactly as
they are and keep serving Orders. Nothing here touches or migrates those.

Business-module agnostic by construction:

* the source object is attached generically via (content_type, object_id) +
  GenericForeignKey — reusing the proven ``approvals.ApprovalRequest`` pattern —
  so there is NO `order`/`payment`/`deposit`/`invoice` foreign key;
* the only concrete relationships are the GENERIC `User` and `Company`, declared
  by STRING reference (`settings.AUTH_USER_MODEL`, `'users.Company'`), so this
  module imports no business module (the boundary test stays green);
* the framework never branches on `if payment` / `if order`. A new module
  integrates by publishing an event with its own entity + company; it never
  edits this file.
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Notification(models.Model):
    """One notification for one recipient, owned by the framework."""

    # Recipient. String ref → no import of the users app.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="framework_notifications",
    )
    # Company scope. The module supplies this from the entity/event context; the
    # framework never derives it from business logic. Nullable so notifications
    # that are genuinely company-less (e.g. a global system message) are valid.
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="framework_notifications",
        null=True,
        blank=True,
    )

    # Stable, machine-readable event name (e.g. PAYMENT_APPROVED). Validation of
    # the name happens at publish time (registry, a later phase); the column just
    # stores and indexes it.
    event_type = models.CharField(max_length=50, db_index=True)
    title = models.CharField(max_length=255, blank=True, default="")
    message = models.TextField()

    # Generic business entity — the polymorphic target (Payment, Deposit, …).
    # Nullable so an entity-less notification is representable. object_id is a
    # PositiveBigIntegerField (like approvals) so it holds any PK, incl. the
    # BigAutoField PKs of payments/deposits.
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    entity = GenericForeignKey("content_type", "object_id")

    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Explicitly schema-qualified to the PUBLIC schema, and deliberately
        # DISTINCT from the old Orders `notifications` table.
        #
        # This project runs with `search_path=payments,public` (the Payments app
        # owns a `payments` schema). A plain, unqualified table name would be
        # created in the FIRST schema on the path (`payments`) — wrong for a
        # SHARED framework used by Payments, Deposits and future modules. Pinning
        # `public"."…` makes Django emit `"public"."notifications_notification"`
        # everywhere (create, query, drop), so placement never depends on the
        # search_path order. All other app tables live in `public`; this one
        # joins them there.
        db_table = 'public"."notifications_notification'
        ordering = ["-created_at"]
        indexes = [
            # Inbox: a recipient's notifications, newest first.
            models.Index(fields=["user", "-created_at"], name="nfw_user_created_idx"),
            # Unread count / mark-read filter by recipient + read state.
            models.Index(fields=["user", "is_read"], name="nfw_user_read_idx"),
            # Company-scoped listing.
            models.Index(fields=["company", "-created_at"], name="nfw_company_created_idx"),
            # Resolve/filter by the generic entity.
            models.Index(fields=["content_type", "object_id"], name="nfw_entity_idx"),
        ]

    def __str__(self):
        return f"Notification<{self.event_type}> for user {self.user_id}"
