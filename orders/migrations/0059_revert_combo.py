"""Revert the combo companion columns on order_items.

Companion to users/0028_revert_combo, which drops the mapping itself. Split
from 0058 so scheme v2 and combo mapping can be reverted independently.

Safe to apply: no order line on live had been created by a combo when this was
written. CHECK BEFORE APPLYING:

    SELECT COUNT(*) FROM order_items WHERE is_auto_free;
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0058_revert_scheme_v2"),
    ]

    operations = [
        migrations.RemoveField(model_name="orderitem", name="combo_source_code"),
        migrations.RemoveField(model_name="orderitem", name="is_auto_free"),
    ]
