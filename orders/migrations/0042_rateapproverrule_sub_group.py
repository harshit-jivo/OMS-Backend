# Generated for sub_group rate-approver matching

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0041_remove_orderitem_sub_group_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='rateapproverrule',
            name='sub_group',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AlterUniqueTogether(
            name='rateapproverrule',
            unique_together={('category', 'sub_group')},
        ),
    ]
