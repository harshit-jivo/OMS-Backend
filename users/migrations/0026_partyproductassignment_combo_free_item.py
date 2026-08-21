from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0025_user_categories'),
    ]

    operations = [
        migrations.AddField(
            model_name='partyproductassignment',
            name='free_item_code',
            field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='partyproductassignment',
            name='free_qty_per_unit',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
    ]
