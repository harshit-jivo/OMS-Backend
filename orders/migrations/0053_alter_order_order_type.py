# Registers the new DISTRIBUTOR choice on Order.order_type.
#
# State-only on purpose. `choices` is validated in Python and never reaches
# Postgres — there is no CHECK constraint behind it — and the column itself is
# unchanged (varchar(20), default 'PARTY'). Letting the autodetector emit a bare
# AlterField would hand the schema editor an ALTER COLUMN TYPE that rewrites
# nothing but still takes an ACCESS EXCLUSIVE lock on `orders`. Splitting it
# keeps Django's state in step at zero cost to the live table.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0052_order_warehouse_code"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="order",
                    name="order_type",
                    field=models.CharField(
                        choices=[
                            ("PARTY", "Party"),
                            ("STAFF", "Staff"),
                            ("DISTRIBUTOR", "Distributor"),
                        ],
                        default="PARTY",
                        max_length=20,
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
