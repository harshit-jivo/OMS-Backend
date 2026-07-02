from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0046_rename_orderitem_variety_sub_group'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='quotation_cancelled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='order',
            name='quotation_cancelled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='quotation_cancelled_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='cancelled_quotations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
