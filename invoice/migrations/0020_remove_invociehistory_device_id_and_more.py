# Drops the device-tracking columns added by 0019. That migration was applied to
# the shared database from a feature branch that was never merged, so the columns
# existed as NOT NULL with no default while the deployed model knew nothing about
# them -- every history insert failed. Device tracking can come back with the
# feature itself; until then the schema follows the code.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('invoice', '0019_invociehistory_device_id_invociehistory_device_name'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='invociehistory',
            name='device_id',
        ),
        migrations.RemoveField(
            model_name='invociehistory',
            name='device_name',
        ),
    ]
