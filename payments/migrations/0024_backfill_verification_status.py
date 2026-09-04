"""Backfill: every receipt that predates verification counts as VERIFIED.

The gate did not exist when these were raised, so applying it retroactively
would strand every in-flight receipt in a queue nobody knew they had to work —
including receipts already POSTED to SAP, which cannot be un-posted and must
never be blocked. The new column defaults to PENDING for rows created from now
on; this migration corrects the ones that came before.

`verified_by` is deliberately left NULL. Naming a verifier would be inventing an
audit fact: nobody performed a handover check on these, and a null honestly
records "verified before the feature existed". No PaymentStatusHistory row is
written for the same reason — the timeline must not claim an event that never
happened.

Financially inert: it touches ONLY `verification_status`, so amounts, statuses,
methods, allocations, SAP DocEntry/DocNum/TransId, company and created_by are
all untouched by construction.
"""
from django.db import migrations


def backfill_verified(apps, schema_editor):
    PaymentReceipt = apps.get_model('payments', 'PaymentReceipt')
    # A single UPDATE, not a loop with .save(): .save() on a historical model
    # would rewrite every column of every row, which is exactly what this
    # migration promises not to do.
    PaymentReceipt.objects.all().update(verification_status='VERIFIED')


def unbackfill(apps, schema_editor):
    """Reverse to PENDING.

    Not a true inverse — it cannot know which rows were VERIFIED by a real
    handover after this shipped, and would reset those too. It exists so the
    migration is not irreversible in development; running it on production data
    would push genuinely verified receipts back into the queue.
    """
    PaymentReceipt = apps.get_model('payments', 'PaymentReceipt')
    PaymentReceipt.objects.filter(verified_by__isnull=True).update(
        verification_status='PENDING')


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0023_add_payment_verification'),
    ]

    operations = [
        migrations.RunPython(backfill_verified, unbackfill),
    ]
