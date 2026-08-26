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
# Additive and reversible: nullable, blank, indexed, no default and no backfill,
# so existing rows simply get NULL -- which is what "no parent item mapped"
# already means everywhere the field is read.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0029_merge_combo_free_item_roles'),
    ]

    operations = [
        migrations.AddField(
            model_name='partyproductassignment',
            name='parent_item_code',
            field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
        ),
    ]
