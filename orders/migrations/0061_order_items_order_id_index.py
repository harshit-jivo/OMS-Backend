"""Create the missing index on `order_items.order_id`.

The model has ALWAYS declared this index — `OrderItem.order` is a ForeignKey, and
Django sets `db_index=True` on those by default. The database simply does not
have it:

    SELECT indexname FROM pg_indexes WHERE tablename = 'order_items';
    order_items_pkey          <- the only one

So this is schema drift, not a design change. `makemigrations` cannot detect it
and will never generate this migration on its own: Django's autodetector compares
model state against model state, and by that measure the index already exists.
That is why the SQL is written by hand here rather than as an AlterField, which
would produce an empty operation.

The cost of the gap, measured before writing this:

    EXPLAIN ANALYZE SELECT * FROM order_items WHERE order_id = 1;
    Seq Scan on order_items  (cost=0.00..5.90 rows=1)
      Filter: (order_id = 1)
      Rows Removed by Filter: 161

Every read of an order's lines scans the table, as does the FK check behind any
`DELETE FROM orders`. `orders_pkey` is the most-scanned index in the database
(68,982 lifetime scans), so this path is hot and gets slower with every order.

IF NOT EXISTS makes it safe to run against a database where someone has already
created the index by hand. `scheme_id` is deliberately left alone: it is also an
unindexed FK, but no query joins through it, and unused indexes cost write
throughput for nothing.

Not CONCURRENTLY: that cannot run inside a transaction, and Django wraps
migrations in one. On a 161-row table the exclusive lock lasts microseconds. If
this is ever applied to a large table, take the lock consideration seriously.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0060_delete_branches"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                'CREATE INDEX IF NOT EXISTS "order_items_order_id_412ad78b" '
                'ON "order_items" ("order_id");'
            ),
            reverse_sql=(
                'DROP INDEX IF EXISTS "order_items_order_id_412ad78b";'
            ),
        ),
    ]
