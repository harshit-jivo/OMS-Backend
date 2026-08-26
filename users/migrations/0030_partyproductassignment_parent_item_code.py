# Hand-written 2026-08-26 while integrating the branch merges into `test`.
#
# `PartyProductAssignment.parent_item_code` was added to users/models.py by
# harshit's de95c81 ("Enhance combo pack handling in SAP integration and update
# PartyProductAssignment model") without its migration -- its two siblings on the
# same commit, `free_item_code` and `free_qty_per_unit`, both got one. The gap
# predates the merge: `origin/harshit` alone already fails
# `makemigrations --check`, and the merge carried the model change across
# faithfully.
#
# Split into state + database halves on 2026-08-26, before this was ever run.
# The column ALREADY EXISTS on the shared `order_management` database at
# 138.252.101.117 -- `varchar(50)` NULL, with both
# `party_product_assignments_parent_item_code_2eeb8c2d` and its `_like`
# counterpart -- while `django_migrations` has no row for this migration. The
# field was evidently applied there from an uncommitted local migration, which
# is the same mistake that left the file missing from the tree in the first
# place. A plain AddField would emit `ADD COLUMN` and die with
#
#     column "parent_item_code" of relation "party_product_assignments"
#     already exists
#
# so the DDL is guarded and the state half is declared separately.
#
# The SQL reproduces exactly what AddField(db_index=True) emits on PostgreSQL:
# the column, the btree index, and the `varchar_pattern_ops` index that backs
# LIKE queries. The index names are Django's own deterministic ones, hardcoded
# so a database built from scratch ends up byte-identical to the one that
# already has them.
#
# Additive and reversible: nullable, blank, indexed, no default and no backfill,
# so existing rows simply read NULL -- which is what "no parent item mapped"
# already means everywhere the field is used. Reversing drops the column, and
# the indexes go with it.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0029_merge_combo_free_item_roles'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='partyproductassignment',
                    name='parent_item_code',
                    field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE party_product_assignments '
                        'ADD COLUMN IF NOT EXISTS parent_item_code varchar(50) NULL;'
                        'CREATE INDEX IF NOT EXISTS '
                        'party_product_assignments_parent_item_code_2eeb8c2d '
                        'ON party_product_assignments (parent_item_code);'
                        'CREATE INDEX IF NOT EXISTS '
                        'party_product_assignments_parent_item_code_2eeb8c2d_like '
                        'ON party_product_assignments '
                        '(parent_item_code varchar_pattern_ops);'
                    ),
                    reverse_sql=(
                        # DROP COLUMN takes both indexes with it.
                        'ALTER TABLE party_product_assignments '
                        'DROP COLUMN IF EXISTS parent_item_code;'
                    ),
                ),
            ],
        ),
    ]
