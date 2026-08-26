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

    Re-parented onto 0004_seed_po_number when the branches were merged
    (2026-08-26). This was written as a sibling of 0002 because
    `0003_uilabel_is_enabled_uilabel_is_required` — the migration that actually
    creates both columns — was missing from the tree at the time; the branch
    merge brought it back, leaving `uilabels` with two leaves and Django
    refusing to run. Depending on the tip of that chain restores a single leaf
    and means the columns exist before this ALTER runs on a database built from
    scratch. The number reads out of order, which Django does not mind: the
    graph is built from `dependencies`, not filenames.
    """

    dependencies = [
        ('uilabels', '0004_seed_po_number'),
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
