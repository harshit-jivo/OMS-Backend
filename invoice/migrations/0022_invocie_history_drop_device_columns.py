from django.db import migrations


class Migration(migrations.Migration):
    """Drop `device_id` / `device_name` from InvocieHistory's model state.

    The database removed both columns via
    `0020_remove_invociehistory_device_id_and_more`, which is still recorded as
    applied but whose file is no longer in the tree. The model was left at the
    `0019` state that added them, so Django kept naming them in every INSERT:

        column "device_id" of relation "invoice_history" does not exist

    That broke every history row — i.e. every status change on an invoice. The
    recorded migration's name states the intent plainly (remove), so the model
    is brought down to the schema rather than the columns being put back.

    The schema half is `DROP COLUMN IF EXISTS` rather than a plain RemoveField:
    on this database the columns are already gone and a bare DROP would error,
    while a database that never ran the missing migration still needs them
    dropped. Idempotent either way.
    """

    dependencies = [
        ('invoice', '0020_invoice_log_is_deleted_default'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='invociehistory', name='device_id'),
                migrations.RemoveField(model_name='invociehistory', name='device_name'),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE invoice_history DROP COLUMN IF EXISTS device_id;'
                        'ALTER TABLE invoice_history DROP COLUMN IF EXISTS device_name;'
                    ),
                    reverse_sql=(
                        "ALTER TABLE invoice_history "
                        "ADD COLUMN IF NOT EXISTS device_id varchar(64) NOT NULL DEFAULT '';"
                        "ALTER TABLE invoice_history "
                        "ADD COLUMN IF NOT EXISTS device_name varchar(150) NOT NULL DEFAULT '';"
                    ),
                ),
            ],
        ),
    ]
