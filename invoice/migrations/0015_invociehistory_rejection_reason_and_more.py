from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoice', '0014_alter_invociehistory_created_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='invociehistory',
            name='rejection_reason',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invociehistory',
            name='error_message',
            field=models.TextField(blank=True, null=True),
        ),
    ]
