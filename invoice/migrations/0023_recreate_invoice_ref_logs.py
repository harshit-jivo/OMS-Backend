from django.db import migrations


class Migration(migrations.Migration):
    """Recreate the missing `invoice_ref_logs` table.

    `0005_invoicereflogs` is recorded as applied, but the table is not in the
    database — it was dropped out of band. `InvoiceRefLogs` is still live code:
    `InvoiceRefLogCreateView` (`POST /invoice/refLogs/`) does an
    `update_or_create` against it, and the SAP draft-verification path reads it,
    so every one of those calls fails with

        relation "invoice_ref_logs" does not exist

    Written as `CREATE TABLE IF NOT EXISTS` rather than a CreateModel so it is a
    no-op on every database that still has the table. `error_message` is created
    NULLable to match the model as it stands today (0005 created it NOT NULL,
    and the model has carried `null=True` since).
    """

    dependencies = [
        ('invoice', '0022_invocie_history_drop_device_columns'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE IF NOT EXISTS invoice_ref_logs (
                id            serial       PRIMARY KEY,
                ref_id        varchar(25)  NOT NULL,
                card_name     varchar(255) NOT NULL,
                doc_date      date         NOT NULL,
                so_number     varchar(255) NOT NULL,
                status        varchar(255) NOT NULL,
                error_message text         NULL,
                posted_at     timestamptz  NOT NULL,
                posted_by_id  integer      NOT NULL
                    REFERENCES users_user (id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE INDEX IF NOT EXISTS invoice_ref_logs_posted_by_id_idx
                ON invoice_ref_logs (posted_by_id);
            """,
            # Never auto-dropped: reversing would destroy the audit rows.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
