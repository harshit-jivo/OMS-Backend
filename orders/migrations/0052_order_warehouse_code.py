# Superseded -- kept as a no-op so applied databases stay consistent.
#
# This was a deliberate RE-CUT of the same column the feature branch adds as
# 0056_order_warehouse_code. Its original comment said so plainly: "There, this
# column arrived as 0056, chained through 0052_orderitem_auto_free_combo ..
# 0055_fix_combo_source_code_nullable. None of those are wanted on production
# yet, so the column is re-cut as a direct child of 0051 instead."
#
# `test` now carries BOTH lineages -- the scheme-engine chain 0052_orderitem_auto
# _free_combo .. 0056 arrived with the same merge as this file. Two migrations
# adding `order.warehouse_code` to model state would raise
# "Field 'warehouse_code' already exists" during state building, so exactly one
# of them has to do the work. 0056 keeps it, because the whole chain this file
# was written to avoid is now present anyway.
#
# The database half is not lost: 0056 has been rewritten to carry the same
# guarded `ADD COLUMN IF NOT EXISTS` DDL this file used, so a database that
# already has the column (because it ran this migration) and one that does not
# both end up with an identical column definition.
#
# The file itself must stay: `django_migrations` already records it as applied on
# the shared databases, and deleting it would strand that row.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0051_webpushsubscription'),
    ]

    operations = []
