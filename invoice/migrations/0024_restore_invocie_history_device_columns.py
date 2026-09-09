from django.db import migrations, models


class Migration(migrations.Migration):
    """Put `device_id` / `device_name` back on InvocieHistory.

    Migration 0022 removed them, reading the database (which had dropped the
    columns) as the intended state. That was the wrong direction: `invoice/views.py`
    still writes both on every history row via `**describe_request_device(request)`
    in four places, so the audit feature is live in the running code and only the
    columns were missing. Removing the fields turned the original
    ProgrammingError into

        TypeError: InvocieHistory() got unexpected keyword arguments:
        'device_id', 'device_name'

    Restoring both sides makes the code work as written. `ADD COLUMN IF NOT
    EXISTS` keeps it a no-op wherever the columns survived.
    """

    dependencies = [
        ('invoice', '0023_recreate_invoice_ref_logs'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='invociehistory',
                    name='device_id',
                    field=models.CharField(blank=True, default='', max_length=64),
                ),
                migrations.AddField(
                    model_name='invociehistory',
                    name='device_name',
                    field=models.CharField(blank=True, default='', max_length=150),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE invoice_history "
                        "ADD COLUMN IF NOT EXISTS device_id varchar(64) NOT NULL DEFAULT '';"
                        "ALTER TABLE invoice_history "
                        "ADD COLUMN IF NOT EXISTS device_name varchar(150) NOT NULL DEFAULT '';"
                    ),
                    reverse_sql=(
                        'ALTER TABLE invoice_history DROP COLUMN IF EXISTS device_id;'
                        'ALTER TABLE invoice_history DROP COLUMN IF EXISTS device_name;'
                    ),
                ),
            ],
        ),
    ]
