"""Control Panel permissions become four page keys.

The per-sub-tab keys (`control_panel.<page>.<sub-tab>`), the segment scopes
and Inventory's values key are folded into their page's key: a user or role
holding any key of a page now holds that page, with all its sub-pages. The
segment keys are dropped (every holder sees all segments). Personal grants
(`User.extra_pages`) and role bundles (`RolePermissions.keys`) both convert.
"""
from django.db import migrations

PAGE_OF = {'oils_sale': 'control_panel.oils_sale', 'sales': 'control_panel.sales',
           'inventory': 'control_panel.inventory', 'finance': 'control_panel.finance'}
NEW_KEYS = set(PAGE_OF.values())


def _convert(keys):
    keys = list(keys or [])
    out = []
    for key in keys:
        key = str(key)
        parts = key.split('.')
        if parts[0] != 'control_panel' or key in NEW_KEYS:
            if key not in out:
                out.append(key)
            continue
        page = PAGE_OF.get(parts[1]) if len(parts) > 2 else None
        if page and page not in out:
            out.append(page)
    return out


def forwards(apps, schema_editor):
    User = apps.get_model('users', 'User')
    RolePermissions = apps.get_model('users', 'RolePermissions')
    for user in User.objects.exclude(extra_pages=None):
        new = _convert(user.extra_pages)
        if new != list(user.extra_pages or []):
            user.extra_pages = new
            user.save(update_fields=['extra_pages'])
    for bundle in RolePermissions.objects.all():
        new = _convert(bundle.keys)
        if new != list(bundle.keys or []):
            bundle.keys = new
            bundle.save(update_fields=['keys'])


class Migration(migrations.Migration):

    dependencies = [
        ('control_panel', '0002_drop_unused_tables'),
        ('users', '0037_grant_mart_cancel'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
