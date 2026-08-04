"""Create the four payment/deposit function roles.

These are selectable as an approval level's Role, and assignable to users via
`extra_roles` so a user keeps their primary role (manager, billing, …) while
also holding a payment function.

Holding one of these roles confers the matching action permission — see
payments/permissions.py:ROLE_PERMISSION_MAP.
"""
from django.db import migrations

# (name, display_name) — `name` is the lookup key used by ROLE_PERMISSION_MAP
# and must match it exactly.
PAYMENT_ROLES = [
    ('payment_creator', 'Payment Creator'),
    ('payment_approver', 'Payment Approver'),
    ('deposit_creator', 'Deposit Creator'),
    ('deposit_approver', 'Deposit Approver'),
]


def create_roles(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    for name, display_name in PAYMENT_ROLES:
        # get_or_create, not create: `name` is unique and this migration may
        # run against a database where an admin already added one by hand.
        UserRole.objects.get_or_create(
            name=name,
            defaults={'display_name': display_name, 'is_active': True},
        )


def remove_roles(apps, schema_editor):
    """Reverse — but never delete a role that is still in use.

    User.role is PROTECT, so deleting an assigned role would raise. Skipping
    those keeps the migration reversible without destroying real assignments.
    """
    UserRole = apps.get_model('users', 'UserRole')
    User = apps.get_model('users', 'User')
    ApprovalLevel = apps.get_model('approvals', 'ApprovalLevel')
    for name, _ in PAYMENT_ROLES:
        role = UserRole.objects.filter(name=name).first()
        if not role:
            continue
        in_use = (
            User.objects.filter(role=role).exists()
            or User.objects.filter(extra_roles=role).exists()
            # ApprovalLevel.role is PROTECT as well — a role wired into a
            # workflow must survive the reverse just like an assigned one.
            or ApprovalLevel.objects.filter(role=role).exists()
        )
        if not in_use:
            role.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0026_user_extra_roles'),
        # The reverse path reads approvals.ApprovalLevel, so that app's tables
        # must exist before this migration runs on a fresh database.
        ('approvals', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
