from django.db import migrations, models


class Migration(migrations.Migration):
    """A deliberate free line on an order. Additive only: two columns with
    defaults, so existing rows read as not-free and older code is unaffected."""

    dependencies = [
        ('orders', '0064_consolidate_rejection_reason_live'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='is_free',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='free_reason',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
