# Generated for sub_group user assignment

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0020_remove_user_categories'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='sub_group',
            field=models.TextField(blank=True, null=True),
        ),
    ]
