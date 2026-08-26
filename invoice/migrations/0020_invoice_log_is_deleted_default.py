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
    """

    dependencies = [
        ('invoice', '0019_invociehistory_device_id_invociehistory_device_name'),
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
