from django.db import migrations, models


class Migration(migrations.Migration):
    """Bring `is_auto_free` / `combo_source_code` into Django's model state.

    Both columns already exist on some databases (they were added out of band
    before the feature landed), so the schema half of this migration uses
    `ADD COLUMN IF NOT EXISTS` and is idempotent; the state half is a normal
    AddField so Django's autodetector stays in sync.
    """

    dependencies = [
        ('orders', '0051_webpushsubscription'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='orderitem',
                    name='is_auto_free',
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name='orderitem',
                    name='combo_source_code',
                    field=models.CharField(blank=True, max_length=50, null=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE order_items '
                        'ADD COLUMN IF NOT EXISTS is_auto_free boolean NOT NULL DEFAULT false;'
                    ),
                    reverse_sql='ALTER TABLE order_items DROP COLUMN IF EXISTS is_auto_free;',
                ),
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE order_items '
                        'ADD COLUMN IF NOT EXISTS combo_source_code varchar(50) NULL;'
                    ),
                    reverse_sql='ALTER TABLE order_items DROP COLUMN IF EXISTS combo_source_code;',
                ),
            ],
        ),
    ]
