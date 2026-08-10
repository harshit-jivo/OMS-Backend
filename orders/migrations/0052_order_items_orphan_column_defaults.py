"""Give DB-level defaults to order_items columns the model doesn't manage.

`is_auto_free` (boolean NOT NULL) and `combo_source_code` (varchar NOT NULL)
exist in the `order_items` table but are NOT defined on the `OrderItem` model
and are referenced nowhere in the codebase — they are orphaned columns from an
out-of-band schema change. Because they are NOT NULL with no default, every
INSERT that goes through the ORM (which never sets them) fails with a
NotNullViolation, breaking order creation.

Rather than resurrect dead model fields, this migration adds sane DB-level
defaults (false / '') so ORM inserts succeed. The columns keep their NOT NULL
constraint; the database now fills them automatically.

Uses ALTER TABLE ... IF EXISTS-style guards via a state-less RunSQL so it is a
no-op on any environment where the columns were never added.
"""
from django.db import migrations


FORWARD_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'order_items' AND column_name = 'is_auto_free'
    ) THEN
        ALTER TABLE order_items ALTER COLUMN is_auto_free SET DEFAULT false;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'order_items' AND column_name = 'combo_source_code'
    ) THEN
        ALTER TABLE order_items ALTER COLUMN combo_source_code SET DEFAULT '';
    END IF;
END $$;
"""

REVERSE_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'order_items' AND column_name = 'is_auto_free'
    ) THEN
        ALTER TABLE order_items ALTER COLUMN is_auto_free DROP DEFAULT;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'order_items' AND column_name = 'combo_source_code'
    ) THEN
        ALTER TABLE order_items ALTER COLUMN combo_source_code DROP DEFAULT;
    END IF;
END $$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0051_webpushsubscription'),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
