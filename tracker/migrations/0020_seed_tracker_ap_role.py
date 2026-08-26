"""Seed the `tracker_ap` role.

Tracker sub-roles are rows in users.UserRole, so a new one has to exist in every
environment before a tracker admin can assign it. Idempotent, and reversible
only when no user still holds the role (deleting it out from under a user would
orphan their access).
"""
from django.db import migrations

ROLE_NAME = 'tracker_ap'
ROLE_DISPLAY = 'Tracker AP Entry'


def create_role(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    UserRole.objects.get_or_create(
        name=ROLE_NAME,
        defaults={'display_name': ROLE_DISPLAY, 'is_active': True},
    )


def remove_role(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    User = apps.get_model('users', 'User')
    if User.objects.filter(role__name=ROLE_NAME).exists():
        # Users still hold it — leave the role alone rather than break their login.
        return
    UserRole.objects.filter(name=ROLE_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0019_remove_invoice_uniq_live_invoice_number_and_more'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_role, remove_role),
    ]
