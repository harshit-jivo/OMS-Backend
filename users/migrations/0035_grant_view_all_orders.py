"""Grant `orders.sales.view_all` to the roles that need company-wide orders.

`_get_base_orders` used to decide scope by matching `user.role.name` against
seven literals — admin, manager, distributor, mart_approval, auditor, approver,
billing — and returned `Order.objects.none()` for anything else. Every role an
administrator creates on the Role Permissions page therefore fell through to
"no orders at all", and because `HasKey('Sales_Dashboard')` had already let
them onto the page, the result was a dashboard of zeroes rather than a refusal.

'Billing Admin' was the first role to hit it: 23 keys, `Sales_Dashboard` among
them, and 0 of 2,820 orders in scope.

Scope is now keyed (`orders/views/_shared.py:sees_all_orders`), so this grants
the new key to the roles that had company-wide visibility before, plus the one
that was supposed to.

`admin` is deliberately NOT seeded, for the same reason 0032 skipped it: admins
hold every key implicitly in `core.permissions.effective_keys`, and a stored
bundle for them would be a second copy to keep in step.

Roles are matched case-insensitively and a role that does not exist in this
database is SKIPPED, not created — the same posture as 0032. 'Billing Admin'
exists only on live, so this is a no-op elsewhere rather than a migration that
invents a role on a developer's machine.
"""
from django.db import migrations

VIEW_ALL = 'orders.sales.view_all'

#: Role names (lowercased) that should see every order.
GRANT_TO = ('billing admin',)


def forwards(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        if role.name.strip().lower() not in GRANT_TO:
            continue
        bundle, _ = RolePermissions.objects.get_or_create(
            role=role, defaults={'keys': []})
        keys = list(bundle.keys or [])
        if VIEW_ALL not in keys:
            keys.append(VIEW_ALL)
            bundle.keys = keys
            bundle.save(update_fields=['keys'])


def backwards(apps, schema_editor):
    """Remove only the key this migration added, leaving the bundle alone."""
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        if role.name.strip().lower() not in GRANT_TO:
            continue
        bundle = RolePermissions.objects.filter(role=role).first()
        if not bundle:
            continue
        keys = [k for k in (bundle.keys or []) if k != VIEW_ALL]
        if keys != list(bundle.keys or []):
            bundle.keys = keys
            bundle.save(update_fields=['keys'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0034_seed_sales_dashboard_permission'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
