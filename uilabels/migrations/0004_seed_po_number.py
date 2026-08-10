from django.db import migrations


# The PO Number input field, seeded so it appears in the admin UI immediately.
# Defaults: shown and optional. Admins can toggle enabled/required from the
# dashboard afterwards.
PO_FIELD = {
    'field_key': 'po_number',
    'display_name': 'PO Number',
    'description': 'Purchase order number field on the order form.',
    'is_enabled': True,
    'is_required': False,
}


def seed_po_field(apps, schema_editor):
    try:
        UILabel = apps.get_model('uilabels', 'UILabel')
    except LookupError:
        return
    # get_or_create (not update_or_create) so re-running never clobbers an
    # admin's later enabled/required/label edits.
    UILabel.objects.get_or_create(
        field_key=PO_FIELD['field_key'],
        defaults={
            'display_name': PO_FIELD['display_name'],
            'description': PO_FIELD['description'],
            'is_active': True,
            'is_enabled': PO_FIELD['is_enabled'],
            'is_required': PO_FIELD['is_required'],
        },
    )


def remove_po_field(apps, schema_editor):
    try:
        UILabel = apps.get_model('uilabels', 'UILabel')
    except LookupError:
        return
    UILabel.objects.filter(field_key=PO_FIELD['field_key']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('uilabels', '0003_uilabel_is_enabled_uilabel_is_required'),
    ]

    operations = [
        migrations.RunPython(seed_po_field, reverse_code=remove_po_field),
    ]
