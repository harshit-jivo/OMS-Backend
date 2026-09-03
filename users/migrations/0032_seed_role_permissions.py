"""Seed each role's permission bundle from today's hardcoded behaviour.

Phase 3 replaces the order endpoints' `HasAnyRole('billing', ...)` gates with
`HasKey('orders.sales.create')`. Without this seed, that flip would silently
lock every non-admin out of the order flow on deploy; with it, each role's
bundle grants exactly what the role names grant today, so the flip changes
nobody's access. Same pattern as EXIM's 0010_split_balance_sheet_permissions:
carry existing access across a mechanism change, grant nothing new.

Where each bundle comes from
----------------------------
The Phase-1 gates in orders/views/lifecycle.py (which were themselves read
off the frontend route table and the desk pages), plus mart.py's
`_is_mart_approver`. `admin` is deliberately NOT seeded: admins hold every
key implicitly in `core.permissions.effective_keys`, and a stored bundle for
them would be a second copy to keep in step.

Role names are matched case-insensitively (the live table has `Distributor`
and `Mart_Approval`). A role that does not exist in this database is skipped,
not created — same posture as EXIM's 0010.

Reversing deletes only bundles this migration created content for, leaving
any admin-edited bundle alone is NOT attempted: reverse is a clean delete of
the seeded role names' bundles, acceptable because reverse-migrating past
this point means abandoning key-based auth anyway.
"""

from django.db import migrations

ORDER_VIEW = 'orders.sales.view'
ORDER_CREATE = 'orders.sales.create'
ORDER_EDIT = 'orders.sales.edit'
DECIDE_SIMPLE = 'orders.decision.simple'
TRANSITION = 'orders.status.transition'
MART_DECIDE = 'orders.mart.decide'

#: role name (lowercased) -> the keys mirroring its Phase-1 access.
SEED = {
    'manager':     [ORDER_VIEW, ORDER_CREATE, ORDER_EDIT, DECIDE_SIMPLE, TRANSITION],
    'billing':     [ORDER_VIEW, ORDER_CREATE, ORDER_EDIT, DECIDE_SIMPLE, TRANSITION],
    'distributor': [ORDER_VIEW, ORDER_CREATE, ORDER_EDIT, TRANSITION],
    'auditor':     [ORDER_VIEW, ORDER_EDIT, DECIDE_SIMPLE, TRANSITION],
    # `rate approver` spellings — every one that routeAccess.ts accepts, so a
    # user under any spelling keeps their desk.
    'approver':      [ORDER_VIEW, DECIDE_SIMPLE, TRANSITION],
    'rate approver': [ORDER_VIEW, DECIDE_SIMPLE, TRANSITION],
    'rate_approver': [ORDER_VIEW, DECIDE_SIMPLE, TRANSITION],
    'rate-approver': [ORDER_VIEW, DECIDE_SIMPLE, TRANSITION],
    'rateapprover':  [ORDER_VIEW, DECIDE_SIMPLE, TRANSITION],
    'mart_approval':    [ORDER_VIEW, MART_DECIDE, TRANSITION],
    'factory_approver': [ORDER_VIEW, TRANSITION],
}


def seed(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        keys = SEED.get(str(role.name or '').strip().lower())
        if not keys:
            continue
        bundle, _ = RolePermissions.objects.get_or_create(role=role)
        # Union, not overwrite: if an admin edited the bundle between deploys
        # (or the migration reruns on another database), their additions stay.
        merged = list(dict.fromkeys([*(bundle.keys or []), *keys]))
        if merged != (bundle.keys or []):
            bundle.keys = merged
            bundle.save(update_fields=['keys', 'updated_at'])


def unseed(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')
    seeded_role_ids = [
        r.id for r in UserRole.objects.all()
        if str(r.name or '').strip().lower() in SEED
    ]
    RolePermissions.objects.filter(role_id__in=seeded_role_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0031_role_permissions'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
