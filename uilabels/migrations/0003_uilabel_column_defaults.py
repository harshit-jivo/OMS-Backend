from django.db import migrations


class Migration(migrations.Migration):
    """Give `uilabels_uilabel.is_enabled` / `is_required` database defaults.

    Same divergence as `invoice_log.is_deleted`: both columns are NOT NULL with
    no default, but `UILabel` does not declare them, so Django omits them from
    the INSERT and Postgres rejects the row. That breaks
    `POST /uilabels/` (AdminLabelListView.post) — creating a label at all.

    Defaults chosen to match what a newly created label means:
      is_enabled  -> true,  mirroring the model's own `is_active` default;
                            a label is created to be shown.
      is_required -> false, the conservative side — creating a label must not
                            silently make the field it names mandatory.

    Guarded so it is a no-op on a database without these columns.
    """

    dependencies = [
        ('uilabels', '0002_seed_price_list'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'uilabels_uilabel' AND column_name = 'is_enabled'
                ) THEN
                    ALTER TABLE uilabels_uilabel ALTER COLUMN is_enabled SET DEFAULT true;
                END IF;
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'uilabels_uilabel' AND column_name = 'is_required'
                ) THEN
                    ALTER TABLE uilabels_uilabel ALTER COLUMN is_required SET DEFAULT false;
                END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
