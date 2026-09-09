"""Consolidate the two rejection-reason columns onto `rejection_reason`.

`orders` carried two columns for one fact, and different code paths wrote
different ones:

    orders/services/order_status.py:149   reject_reason only
    orders/services/order_status.py:247   both
    orders/views/lifecycle.py:1098        rejection_reason only
    orders/views/mart.py:223              rejection_reason only

So a reader of either column alone saw an incomplete picture. Measured across
132 orders before this migration:

    reject_reason populated, rejection_reason empty   69
    rejection_reason populated, reject_reason empty    3
    both populated                                     3
    both populated AND DIFFERENT                       0   <- no data conflict

`rejection_reason` is the canonical survivor because it is the one anything
actually READS: `orders/views/mart.py:97` serves it to the web MartApproval
screen. `reject_reason` has no reader anywhere in the repository — it is written
by two paths and otherwise only appears in a serializer field list.

This migration copies the 69 orphaned values ACROSS, so `rejection_reason` holds
every reason the system has ever recorded. It does NOT drop `reject_reason`:
that column is still written (harmlessly, in step with the canonical one) and is
still exposed by `orders/serializers.py:554`, so dropping it now would be a
phase-order violation and could break a client reading it. Dropping it is a
later migration, once the field is out of the serializer and the API contract
has moved.

Zero conflicts is what makes this safe, and it is re-checked at runtime rather
than trusted: if any row disagrees, the migration raises instead of silently
picking a winner and destroying the other value.

Reverse is a no-op by design. The copied values are indistinguishable from
values that were always there, so "undoing" the copy would mean deleting
legitimate reasons. `reject_reason` still holds its originals, and a full
pre-migration export of both columns for all 75 affected rows was taken before
this ran.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Order = apps.get_model("orders", "Order")

    conflicts = (Order.objects
                 .exclude(rejection_reason__isnull=True)
                 .exclude(rejection_reason="")
                 .exclude(reject_reason="")
                 .exclude(reject_reason__isnull=True))
    clashing = [o.pk for o in conflicts
                if (o.rejection_reason or "").strip()
                != (o.reject_reason or "").strip()]
    if clashing:
        raise RuntimeError(
            "Refusing to consolidate: orders %s hold DIFFERENT text in "
            "rejection_reason and reject_reason. Resolve them by hand first — "
            "picking one automatically would destroy the other." % clashing)

    # Only fills blanks, so it is idempotent and can never overwrite a
    # canonical value that is already set.
    schema_editor.execute("""
        UPDATE orders
           SET rejection_reason = reject_reason
         WHERE COALESCE(reject_reason, '') <> ''
           AND COALESCE(rejection_reason, '') = ''
    """)


def backwards(apps, schema_editor):
    """Deliberately does nothing — see the module docstring."""


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0062_delete_orders_partyaddress"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
