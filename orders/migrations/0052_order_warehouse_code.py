# Hand-written so warehouse selection can ship without the scheme-engine-v2 and
# combo migrations it sits behind on the feature branch. There, this column
# arrived as 0056, chained through 0052_orderitem_auto_free_combo ..
# 0055_fix_combo_source_code_nullable. None of those are wanted on production
# yet, so the column is re-cut as a direct child of 0051 instead.
#
# The schema half is idempotent because the shared TEST database is AHEAD of
# live: it already ran the feature branch's 0056, so `orders.warehouse_code`
# exists there while `django_migrations` has no row under this name. A plain
# AddField would die with "column already exists" on test and still be needed on
# production, so the two halves are split -- the same reason
# 0052_orderitem_auto_free_combo is written this way on the feature branch.
#
# The DDL reproduces exactly what AddField would emit (add NOT NULL with a
# default so existing rows fill, then drop the default), leaving the column
# definition identical on a database that gets it from here and one that already
# had it. DROP DEFAULT is a no-op where no default is set.
#
# Additive either way: existing rows get '', which the SAP sync reads as "fall
# back to the per-category default warehouse" -- exactly how every order placed
# before the picker existed already behaves.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0051_webpushsubscription'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='order',
                    name='warehouse_code',
                    field=models.CharField(blank=True, default='', max_length=20),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE orders "
                        "ADD COLUMN IF NOT EXISTS warehouse_code varchar(20) "
                        "NOT NULL DEFAULT '';"
                        "ALTER TABLE orders "
                        "ALTER COLUMN warehouse_code DROP DEFAULT;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE orders "
                        "DROP COLUMN IF EXISTS warehouse_code;"
                    ),
                ),
            ],
        ),
    ]
