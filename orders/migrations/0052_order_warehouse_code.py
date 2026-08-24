# Hand-written so warehouse selection can ship without the scheme-engine-v2 and
# combo migrations it sits behind on the feature branch. There, this column
# arrived as 0056, chained through 0052_orderitem_auto_free_combo ..
# 0055_fix_combo_source_code_nullable. None of those are wanted on production
# yet, so the column is re-cut as a direct child of 0051 instead.
#
# Additive and nullable-by-default: existing rows get '', which the SAP sync
# reads as "fall back to the per-category default warehouse" — exactly how every
# order placed before the picker existed already behaves.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0051_webpushsubscription'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='warehouse_code',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
    ]
