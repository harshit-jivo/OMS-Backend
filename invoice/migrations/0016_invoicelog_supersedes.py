import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoice', '0015_invociehistory_rejection_reason_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoicelog',
            name='supersedes',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='superseded_by',
                to='invoice.invoicelog',
            ),
        ),
    ]
