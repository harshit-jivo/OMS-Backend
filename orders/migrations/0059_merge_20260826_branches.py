# Merge migration, hand-written 2026-08-26 while integrating rathod-updates,
# kamal, harshit and Mukesh into `test`.
#
# `orders` was left with two independent leaves after the branch merges:
#
#   0058_alter_order_order_type      -- registers the DISTRIBUTOR choice
#                                       (off 0057_merge_20260822_1319)
#   0058_scheme_benefit_uom_snapshot -- scheme benefit qty/uom snapshot fields
#                                       (off 0057_scheme_uom_pcs_box_only)
#
# Django requires exactly one leaf per app and refuses to run with more
# ("Conflicting migrations detected; multiple leaf nodes in the migration
# graph"). The two touch different models -- Order.order_type versus
# OrderItemScheme.benefit_qty / benefit_uom -- so they commute and need nothing
# more than to be joined.
#
# A third leaf, 0053_alter_order_order_type, was folded into
# 0057_merge_20260822_1319 instead, because it is a superseded duplicate of
# 0058_alter_order_order_type and had to be ordered BEFORE it, not merged
# alongside it.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0058_alter_order_order_type"),
        ("orders", "0058_scheme_benefit_uom_snapshot"),
    ]

    operations = []
