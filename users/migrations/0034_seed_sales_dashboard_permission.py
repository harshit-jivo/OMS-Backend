"""Back-grant `Sales_Dashboard` to every role that could open the dashboard.

The dashboard moved from `/Dashboard` (open to any signed-in user, because it
doubled as the landing page and denying it would have looped the user) to
`/Sales_Dashboard`, behind the `Sales_Dashboard` key. `/Home` took over the
landing job, which is what freed the analytics page to carry a real gate.

Without this seed that flip would take the dashboard away from every non-admin
on deploy day. With it, each role's bundle grants exactly what the role could
see the day before — same contract as 0032 and 0033: carry existing access
across a mechanism change, grant nothing new.

Where the list comes from
-------------------------
`Frontend/src/pages/dashboard/constants.ts`'s `roleContent` — the five roles
the dashboard renders tailored KPIs for. Any other role reached the page only
because it was ungated, and saw a screen built for someone else; those are the
accounts the gate is FOR, so they are deliberately not seeded.

`admin` is not seeded, for the same reason as 0032: admins hold every key
implicitly in `core.permissions.effective_keys`, and a stored bundle would be a
second copy to keep in step.

Reversal removes only this key from these roles' bundles, so an admin's other
edits survive in both directions.
"""

from django.db import migrations

SALES_DASHBOARD = 'Sales_Dashboard'

#: Roles that `roleContent` has a dashboard for. Rate approver is spelled
#: several ways in the live table — every spelling routeAccess.ts accepts is
#: listed, so a user under any of them keeps their screen.
ROLES = [
    'manager',
    'billing',
    'auditor',
    'approver',
    'rate approver',
    'rate_approver',
    'rate-approver',
    'rateapprover',
]


def seed(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        if str(role.name or '').strip().lower() not in ROLES:
            continue
        bundle, _ = RolePermissions.objects.get_or_create(role=role)
        keys = bundle.keys or []
        if SALES_DASHBOARD in keys:
            continue
        # Union, not overwrite: an admin who edited the bundle between deploys
        # keeps their edits.
        bundle.keys = [*keys, SALES_DASHBOARD]
        bundle.save(update_fields=['keys', 'updated_at'])


def unseed(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        if str(role.name or '').strip().lower() not in ROLES:
            continue
        bundle = RolePermissions.objects.filter(role=role).first()
        if bundle is None:
            continue
        remaining = [k for k in (bundle.keys or []) if k != SALES_DASHBOARD]
        if remaining != (bundle.keys or []):
            bundle.keys = remaining
            bundle.save(update_fields=['keys', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0033_seed_tracker_invoice_permissions'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
