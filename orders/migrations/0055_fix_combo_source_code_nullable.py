from django.db import migrations


class Migration(migrations.Migration):
    """Drop the stray NOT NULL on `order_items.combo_source_code`.

    The column was added out of band as `NOT NULL DEFAULT ''` before the combo
    feature landed. Migration 0052 then brought it into Django's model state as
    `null=True`, but its schema half used `ADD COLUMN IF NOT EXISTS`, which is a
    no-op where the column already existed — so on those databases the constraint
    survived while the model believed the column was nullable.

    Every ordinary (non-combo) line then writes NULL here and hits an
    IntegrityError on order create. Django's state already says `null=True`, so
    this is a database-only repair: no AddField/AlterField, nothing for the
    autodetector to notice.
    """

    dependencies = [
        ('orders', '0054_scheme_category'),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                'ALTER TABLE order_items '
                'ALTER COLUMN combo_source_code DROP NOT NULL;'
            ),
            # Not reversed: re-adding NOT NULL would fail against the NULLs this
            # migration exists to allow, and would restore the bug.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
