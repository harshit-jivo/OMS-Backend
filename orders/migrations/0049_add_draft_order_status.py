from django.db import migrations
from django.db.models import Max

DRAFT_STATUS = {'code': 'DRAFT', 'name': 'Draft'}


def add_draft_status(apps, schema_editor):
    try:
        OrderStatus = apps.get_model('orders', 'OrderStatus')
    except LookupError:
        return

    existing = OrderStatus.objects.filter(code=DRAFT_STATUS['code']).first()
    if existing:
        existing.name = DRAFT_STATUS['name']
        existing.save(update_fields=['name'])
        return

    # The original statuses were seeded with explicit PKs, which can leave the
    # Postgres id sequence behind the actual max id. Assign an explicit id to
    # avoid a duplicate-key collision from an auto-assigned value.
    next_id = (OrderStatus.objects.aggregate(m=Max('id'))['m'] or 0) + 1
    OrderStatus.objects.create(
        id=next_id,
        code=DRAFT_STATUS['code'],
        name=DRAFT_STATUS['name'],
    )


def remove_draft_status(apps, schema_editor):
    try:
        OrderStatus = apps.get_model('orders', 'OrderStatus')
    except LookupError:
        return

    OrderStatus.objects.filter(code=DRAFT_STATUS['code']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0048_template_sub_group'),
    ]

    operations = [
        migrations.RunPython(add_draft_status, reverse_code=remove_draft_status),
    ]
