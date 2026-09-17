from django.db import migrations


# Switch for the manual "Schemes" box on each order item (web + mobile).
# Seeded shown so nothing changes on deploy; admins untick "Active" or
# "Field enabled" in UI Labels to hide it and rely on the scheme mapping alone.
SCHEME_BOX = {
    'field_key': 'manual_scheme_box',
    'display_name': 'Schemes',
    'description': 'Manual scheme picker on each order item. Disable to use only auto-mapped schemes.',
    'is_enabled': True,
    'is_required': False,
}


def seed_scheme_box(apps, schema_editor):
    try:
        UILabel = apps.get_model('uilabels', 'UILabel')
    except LookupError:
        return
    # get_or_create so re-running never clobbers an admin's later edits.
    UILabel.objects.get_or_create(
        field_key=SCHEME_BOX['field_key'],
        defaults={
            'display_name': SCHEME_BOX['display_name'],
            'description': SCHEME_BOX['description'],
            'is_active': True,
            'is_enabled': SCHEME_BOX['is_enabled'],
            'is_required': SCHEME_BOX['is_required'],
        },
    )


def remove_scheme_box(apps, schema_editor):
    try:
        UILabel = apps.get_model('uilabels', 'UILabel')
    except LookupError:
        return
    UILabel.objects.filter(field_key=SCHEME_BOX['field_key']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('uilabels', '0003_uilabel_column_defaults'),
    ]

    operations = [
        migrations.RunPython(seed_scheme_box, reverse_code=remove_scheme_box),
    ]
