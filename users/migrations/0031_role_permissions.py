# Hand-written (equivalent to `makemigrations users`): the users_role_permissions
# table backing users.RolePermissions. Schema only; 0032 seeds the rows.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0030_partyproductassignment_parent_item_code'),
    ]

    operations = [
        migrations.CreateModel(
            name='RolePermissions',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('keys', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('role', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='permission_bundle', to='users.userrole')),
            ],
            options={
                'db_table': 'users_role_permissions',
            },
        ),
    ]
