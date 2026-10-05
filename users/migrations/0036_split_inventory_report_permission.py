"""Back-grant `Inventory_Report` to everyone who held the umbrella `Reports`.

`Inventory_Report` was split out of the umbrella `Reports` key so the
warehouse-wise SAP stock report can be granted on its own (e.g. to a
mart_approval user who should see inventory but none of the sales reports).
The frontend route `/Inventory_Report` no longer accepts `Reports` — it accepts
the new key, with the `billing` role as a fallback (routeAccess.ts).

Without this seed, the split would silently take the Inventory Report away from
every non-billing user who reached it through `Reports`. With it, anyone who
held `Reports` — whether through a role bundle or a personal extra_pages grant —
also holds `Inventory_Report`, so the split changes nobody's access. Same
posture as 0032/0035: carry existing access across a mechanism change, grant
nothing genuinely new.

`billing` needs no back-grant: it keeps the page through the role fallback on
the route. `admin` is deliberately NOT seeded — admins hold every key
implicitly in `core.permissions.effective_keys`, and a stored copy would be a
second thing to keep in step.

Reversing removes only the key this migration added, leaving each bundle and
each user's other grants alone.
"""
from django.db import migrations

REPORTS = 'Reports'
INVENTORY_REPORT = 'Inventory_Report'


def forwards(apps, schema_editor):
    RolePermissions = apps.get_model('users', 'RolePermissions')
    User = apps.get_model('users', 'User')

    # Role bundles that tick `Reports`.
    for bundle in RolePermissions.objects.all():
        keys = list(bundle.keys or [])
        if REPORTS in keys and INVENTORY_REPORT not in keys:
            keys.append(INVENTORY_REPORT)
            bundle.keys = keys
            bundle.save(update_fields=['keys'])

    # Personal extra_pages grants that tick `Reports`.
    for user in User.objects.all():
        pages = list(user.extra_pages or [])
        if REPORTS in pages and INVENTORY_REPORT not in pages:
            pages.append(INVENTORY_REPORT)
            user.extra_pages = pages
            user.save(update_fields=['extra_pages'])


def backwards(apps, schema_editor):
    RolePermissions = apps.get_model('users', 'RolePermissions')
    User = apps.get_model('users', 'User')

    for bundle in RolePermissions.objects.all():
        keys = [k for k in (bundle.keys or []) if k != INVENTORY_REPORT]
        if keys != list(bundle.keys or []):
            bundle.keys = keys
            bundle.save(update_fields=['keys'])

    for user in User.objects.all():
        pages = [p for p in (user.extra_pages or []) if p != INVENTORY_REPORT]
        if pages != list(user.extra_pages or []):
            user.extra_pages = pages
            user.save(update_fields=['extra_pages'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0035_grant_view_all_orders'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
