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
    # The old approval engine is being retired. While it still exists a role
    # wired into one of its levels must survive this reverse, exactly as
    # before; once the app is gone there is no such wiring to protect, so its
    # absence is not an error. LookupError is the only thing that changes
    # here — the rule itself is untouched.
    try:
        ApprovalLevel = apps.get_model('approvals', 'ApprovalLevel')
    except LookupError:
        ApprovalLevel = None
    for name, _ in PAYMENT_ROLES:
        role = UserRole.objects.filter(name=name).first()
        if not role:
            continue
        in_use = (
            User.objects.filter(role=role).exists()
            or User.objects.filter(extra_roles=role).exists()
            # ApprovalLevel.role is PROTECT as well — a role wired into a
            # workflow must survive the reverse just like an assigned one.
            or (ApprovalLevel is not None
                and ApprovalLevel.objects.filter(role=role).exists())
        )
        if not in_use:
            role.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0026_user_extra_roles'),
        # The reverse path reads approvals.ApprovalLevel WHEN THAT APP IS
        # STILL INSTALLED, so its tables must exist first. This dependency is
        # the one thing that must be removed in the same change that removes
        # the `approvals` app, or a fresh-database replay will look for a
        # migration that no longer exists. `remove_roles` above already
        # tolerates the model being gone.
        # The `approvals` app was REMOVED once payments moved to the
        # Workflow Engine. Its dependency edge is dropped with it —
        # Django only needs the graph to resolve, and an applied
        # migration is never re-run. The body above already tolerates
        # the models being absent (`LookupError`), so this migration
        # still applies cleanly to a fresh database.
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
