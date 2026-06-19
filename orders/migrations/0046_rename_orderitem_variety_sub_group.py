from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0045_alter_partyorderflowconfig_unique_together_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='orderitem',
            old_name='variety',
            new_name='sub_group',
        ),
    ]
