"""Grant `orders.mart.cancel` to the mart_approval role.

The Mart Cancel page lets the Mart Approval desk cancel a completed
(SAP-posted) distributor order, reversing its SAP Sales Order. It is gated by
the new `orders.mart.cancel` key (core/permission_registry.py), kept separate
from `orders.mart.decide` because cancelling an order already booked into SAP
is a heavier, financial action than approving/rejecting a pending one.

This seed hands the key to the mart_approval role so the desk has the page on
deploy — the same union-not-overwrite posture as 0032_seed_role_permissions.
`admin` is deliberately not seeded: admins hold every key implicitly in
`core.permissions.effective_keys`.
"""
from django.db import migrations

MART_CANCEL = 'orders.mart.cancel'
TARGET_ROLE = 'mart_approval'


def grant(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        if str(role.name or '').strip().lower() != TARGET_ROLE:
            continue
        bundle, _ = RolePermissions.objects.get_or_create(role=role)
        keys = list(bundle.keys or [])
        if MART_CANCEL not in keys:
            keys.append(MART_CANCEL)
            bundle.keys = keys
            bundle.save(update_fields=['keys', 'updated_at'])


def ungrant(apps, schema_editor):
    UserRole = apps.get_model('users', 'UserRole')
    RolePermissions = apps.get_model('users', 'RolePermissions')

    for role in UserRole.objects.all():
        if str(role.name or '').strip().lower() != TARGET_ROLE:
            continue
        bundle = RolePermissions.objects.filter(role=role).first()
        if not bundle:
            continue
        keys = [k for k in (bundle.keys or []) if k != MART_CANCEL]
        if keys != list(bundle.keys or []):
            bundle.keys = keys
            bundle.save(update_fields=['keys', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0036_split_inventory_report_permission'),
    ]

    operations = [
        migrations.RunPython(grant, ungrant),
    ]
