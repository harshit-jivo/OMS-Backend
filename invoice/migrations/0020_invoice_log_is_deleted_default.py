from django.db import migrations


class Migration(migrations.Migration):
    """Give `invoice_log.is_deleted` a database default.

    The column is NOT NULL with no default, but `InvoiceLog` no longer declares
    it, so Django leaves it out of the INSERT entirely and Postgres rejects the
    row:

        null value in column "is_deleted" of relation "invoice_log"
        violates not-null constraint

    That broke every new invoice log. The column was added by a migration whose
    file is no longer in the tree (`0021_invoicelog_delete_reason_...`, still
    recorded as applied), so the model and the schema have diverged.

    A default is the repair that unblocks writes without reinstating a model
    field the tree no longer carries: an INSERT that omits the column now stores
    false, which is what a newly created log means. Guarded so it is a no-op on
    a database that never grew the column.

    Re-parented onto 0021 when the branches were merged (2026-08-26). The
    docstring above was written while `0021_invoicelog_delete_reason_...` was
    missing from the tree; it is now present, and it is what creates
    `is_deleted`. Depending on it means the column exists before this ALTER runs
    on a database built from scratch, instead of relying on the graph ordering
    two siblings of 0019 favourably. The `IF EXISTS` guard is kept anyway.
    """

    dependencies = [
        ('invoice', '0021_invoicelog_delete_reason_invoicelog_deleted_at_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'invoice_log' AND column_name = 'is_deleted'
                ) THEN
                    ALTER TABLE invoice_log ALTER COLUMN is_deleted SET DEFAULT false;
                END IF;
            END $$;
            """,
            # Dropping the default would restore the IntegrityError.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
