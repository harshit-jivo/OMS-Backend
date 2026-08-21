"""Record `Order.warehouse_code` in migration state — the column already exists.

The `warehouse_code` column was created on the database by
`0056_order_warehouse_code` from the `harshit` branch, which is already applied
here (see django_migrations). This branch's migration files stop at 0052, so
Django cannot see that history and would otherwise generate an AddField that
tries to ADD a column the database already has.

SeparateDatabaseAndState applies the field to Django's model state ONLY and
runs no SQL, which reconciles the two without touching the live schema.

If this branch is later merged with the one carrying 0053-0056, drop this file:
the real 0056_order_warehouse_code supersedes it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0052_order_items_orphan_column_defaults'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name='order',
                    name='warehouse_code',
                    field=models.CharField(blank=True, default='', max_length=20),
                ),
            ],
        ),
    ]
