"""Revert scheme engine v2.

Production was always meant to carry the Mart integration WITHOUT scheme v2;
it arrived on live when `production` absorbed `kamal`. This drops the four
scheme-v2 tables and the v2 columns on order_item_schemes.

Safe to apply: nothing had used the engine on live when this was written --
0 schemes, 0 benefits, 0 triggers, 0 assignments. CHECK BEFORE APPLYING:

    SELECT (SELECT COUNT(*) FROM schemes)            AS schemes,
           (SELECT COUNT(*) FROM scheme_assignments) AS assignments;

The legacy scheme path (users.SchemeProduct -> order_item_schemes.scheme_id,
qty_scheme) is untouched and keeps working. Combo mapping is reverted
separately in 0059.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0057_merge_20260826_1044"),
    ]

    operations = [
        # MUST come first: clearing unique_together resolves the old field names
        # ('scheme', 'scope_type', 'scope_value', 'category') against the current
        # model state, so it fails with FieldDoesNotExist if the `scheme` field
        # has already been removed. The autodetector emitted it after the
        # RemoveField, which is why this is hand-ordered.
        migrations.AlterUniqueTogether(name="schemeassignment", unique_together=None),
        migrations.RemoveField(model_name="scheme", name="created_by"),
        migrations.RemoveField(model_name="orderitemscheme", name="scheme_v2"),
        migrations.RemoveField(model_name="schemebenefit", name="scheme"),
        migrations.RemoveField(model_name="schemeassignment", name="scheme"),
        migrations.RemoveField(model_name="schemeassignment", name="created_by"),
        migrations.RemoveField(model_name="orderitemscheme", name="benefit"),
        migrations.RemoveField(model_name="orderitemscheme", name="benefit_item_code"),
        migrations.RemoveField(model_name="orderitemscheme", name="computed_qty"),
        migrations.RemoveField(model_name="orderitemscheme", name="is_manual_override"),
        migrations.RemoveField(model_name="orderitemscheme", name="scope_type"),
        migrations.RemoveField(model_name="orderitemscheme", name="scope_value"),
        migrations.DeleteModel(name="SchemeTrigger"),
        migrations.DeleteModel(name="Scheme"),
        migrations.DeleteModel(name="SchemeAssignment"),
        migrations.DeleteModel(name="SchemeBenefit"),
    ]
