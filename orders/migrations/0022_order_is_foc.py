from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0021_merge_20260502_1753"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="is_foc",
            field=models.BooleanField(default=False),
        ),
    ]
