from django.db import migrations


# Default labels shipped with the feature. More rows can be added to this list
# in future migrations (or from the admin dashboard) without any code change.
DEFAULT_LABELS = [
    {
        'field_key': 'price_list',
        'display_name': 'Price List',
        'description': 'Unified price list field label shown on web and mobile.',
    },
]


def seed_labels(apps, schema_editor):
    try:
        UILabel = apps.get_model('uilabels', 'UILabel')
    except LookupError:
        return
    for label in DEFAULT_LABELS:
        UILabel.objects.update_or_create(
            field_key=label['field_key'],
            defaults={
                'display_name': label['display_name'],
                'description': label['description'],
                'is_active': True,
            },
        )


def remove_labels(apps, schema_editor):
    try:
        UILabel = apps.get_model('uilabels', 'UILabel')
    except LookupError:
        return
    keys = [label['field_key'] for label in DEFAULT_LABELS]
    UILabel.objects.filter(field_key__in=keys).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('uilabels', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_labels, reverse_code=remove_labels),
    ]
