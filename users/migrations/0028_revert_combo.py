"""Revert the combo mapping columns on party_product_assignments.

This is where a combo's free-of-cost item was configured. Companion to
orders/0059_revert_combo, which drops the columns the mapping wrote onto
order lines.

Safe to apply: no assignment on live carried a mapping when this was written.
CHECK BEFORE APPLYING:

    SELECT COUNT(*) FROM party_product_assignments
    WHERE free_item_code IS NOT NULL AND free_item_code <> '';
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0027_merge_20260826_1044"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="partyproductassignment",
            name="free_item_code",
        ),
        migrations.RemoveField(
            model_name="partyproductassignment",
            name="free_qty_per_unit",
        ),
    ]
