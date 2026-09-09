"""Seed tracker and invoice permission bundles (registry fold, Phase 5).

Same contract as 0032: each role's bundle receives exactly what the role
already confers today, so the fold changes nobody's access on deploy day.

* Tracker bundles mirror `tracker/permissions.py:ROLE_PAGE_MAP` verbatim.
  The role map still works on its own — `tracker_pages_for` unions both
  sources — so these seeds are the matrix page showing the truth rather
  than a new grant. Editing a bundle afterwards IS a real change, which is
  the point.

* Invoice bundles mirror the frontend route table (routeAccess.ts): billing
  holds the Sales Invoice, Invoice Review and Invoice Report pages;
  factory_approver holds Invoice Review.

Union, not overwrite, and reversal deletes only these keys from the named
roles' bundles — an admin's other edits survive both directions.
"""

from django.db import migrations

SEED = {
    # --- tracker sub-roles: verbatim ROLE_PAGE_MAP -------------------------
    'tracker_admin': ['Tracker_Entry', 'Tracker_Queue', 'Tracker_Alerts',
                      'Tracker_Reports', 'Tracker_Admin', 'Tracker_Invoices',
                      'Ap_Invoice_Entry'],
    'tracker_entry': ['Tracker_Entry', 'Tracker_Queue'],
    'tracker_user':  ['Tracker_Queue'],
    'tracker_ap':    ['Ap_Invoice_Entry'],

    # --- invoice pages: verbatim routeAccess.ts ----------------------------
    'billing': ['invoices.sales.create', 'invoices.review.decide',
                'invoices.report.view'],
    'factory_approver': ['invoices.review.decide'],
}


def seed(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        keys = SEED.get(str(role.name or '').strip().lower())
        if not keys:
            continue
        bundle, _ = RolePermissions.objects.get_or_create(role=role)
        merged = list(dict.fromkeys([*(bundle.keys or []), *keys]))
        if merged != (bundle.keys or []):
            bundle.keys = merged
            bundle.save(update_fields=['keys', 'updated_at'])


def unseed(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        keys = SEED.get(str(role.name or '').strip().lower())
        if not keys:
            continue
        bundle = RolePermissions.objects.filter(role=role).first()
        if bundle is None:
            continue
        remaining = [k for k in (bundle.keys or []) if k not in keys]
        if remaining != (bundle.keys or []):
            bundle.keys = remaining
            bundle.save(update_fields=['keys', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0032_seed_role_permissions'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
