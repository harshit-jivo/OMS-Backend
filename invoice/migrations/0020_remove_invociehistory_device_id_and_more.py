from django.db import migrations


class Migration(migrations.Migration):
    """Superseded — kept as a no-op so applied databases stay consistent.

    Originally (2026-08-12) this dropped the device-tracking columns added by
    0019, because 0019 had been applied to the shared database from a feature
    branch that was never merged: the columns existed as NOT NULL with no
    default while the deployed model knew nothing about them, so every history
    insert failed. The comment then read "device tracking can come back with the
    feature itself".

    It has. `0024_restore_invocie_history_device_columns` (2026-08-21) puts both
    fields back on the model and the table, and explains why removing them was
    the wrong direction: `invoice/views.py` writes both on every history row via
    `**describe_request_device(request)`, so dropping the fields only turned a
    ProgrammingError into a TypeError. Both branches are now merged, so the two
    migrations would fight — this one removing the fields from model state while
    0024 adds them back, with the end state decided by whichever the graph
    happens to order last.

    Emptying the operations settles it in favour of the later, better-informed
    decision. Nothing is lost: 0022 still issues `DROP COLUMN IF EXISTS` for the
    database half, and 0024 re-adds with `ADD COLUMN IF NOT EXISTS`, so a
    database that never ran this migration converges on the same schema as one
    that did. The file itself must stay: `django_migrations` already records it
    as applied on the shared databases, and deleting it would strand that row.
    """

    dependencies = [
        ('invoice', '0019_invociehistory_device_id_invociehistory_device_name'),
    ]

    operations = []
