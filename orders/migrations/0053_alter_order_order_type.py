# Superseded -- kept as a no-op so applied databases stay consistent.
#
# Registered the DISTRIBUTOR choice on Order.order_type, state-only, as a direct
# child of the re-cut 0052_order_warehouse_code. 0058_alter_order_order_type
# makes the identical AlterField on the other lineage, and `test` now carries
# both. Two AlterFields on the same field are not an error, but the end state
# would be decided by whichever the graph happens to order last, which is not
# something to leave to chance.
#
# Worth preserving from the original note, because 0058 does NOT do this and it
# is a real consideration for a large `orders` table: `choices` is validated in
# Python and never reaches Postgres -- there is no CHECK constraint behind it --
# and the column is unchanged (varchar(20), default 'PARTY'). A bare AlterField
# still hands the schema editor an ALTER COLUMN TYPE that rewrites nothing but
# takes an ACCESS EXCLUSIVE lock on `orders`. If that lock is a problem when 0058
# is applied to a live database, split it the same way this file did rather than
# reviving this migration.
#
# The file itself must stay: `django_migrations` already records it as applied on
# the shared databases, and deleting it would strand that row.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0052_order_warehouse_code"),
    ]

    operations = []
