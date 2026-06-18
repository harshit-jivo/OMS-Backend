from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0039_rename_orderitem_price_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='productdetails',
            name='sub_group',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='sub_group',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
