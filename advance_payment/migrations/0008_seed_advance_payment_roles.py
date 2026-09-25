"""Create the two Payments roles, with their permission bundles.

    advance_payment_user       raises requests and follows their own
                               (Payments page: `Advance_Payment`)
    advance_payment_approver   the same, and the approval desk
                               (+ `Advance_Payment_Approval`)

The desk key only OPENS the desk: acting on a request also needs being its
current stage's user in the Workflows page, which the server checks on every
action.

Named `advance_payment_*`, not `payment_*`: `payment_approver` already exists
(users/0027) for the receipts & deposits approval ladder, which grants
approval to anyone holding a level's role, so reusing that name would let a
Payments approver approve receipts too.

Idempotent (`get_or_create`; keys are unioned into an existing bundle), and
the reverse removes a role only when no user holds it.
"""
from django.db import migrations

ROLES = [
    ('advance_payment_user', 'Advance Payment User', ['Advance_Payment']),
    ('advance_payment_approver', 'Advance Payment Approver',
     ['Advance_Payment', 'Advance_Payment_Approval']),
]


def create_roles(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')
    for name, display_name, keys in ROLES:
        role, _ = UserRole.objects.get_or_create(
            name=name, defaults={'display_name': display_name, 'is_active': True})
        bundle, _ = RolePermissions.objects.get_or_create(role=role)
        merged = list(dict.fromkeys([*(bundle.keys or []), *keys]))
        if merged != (bundle.keys or []):
            bundle.keys = merged
            bundle.save(update_fields=['keys', 'updated_at'])


def remove_roles(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    User = apps.get_model('users', 'User')
    for name, _display, _keys in ROLES:
        role = UserRole.objects.filter(name=name).first()
        if role is None:
            continue
        if User.objects.filter(role=role).exists() or User.objects.filter(extra_roles=role).exists():
            # Held by someone: keep it rather than take their access away.
            continue
        role.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0007_general_approval_stages'),
        ('users', '0031_role_permissions'),
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
