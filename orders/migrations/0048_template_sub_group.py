from django.db import migrations, models


def backfill_template_sub_group(apps, schema_editor):
    Template = apps.get_model('orders', 'Template')
    OrderItem = apps.get_model('orders', 'OrderItem')

    for template in Template.objects.all().only('temp_id', 'order_id'):
        sub_groups = [
            str(sub_group or '').strip()
            for sub_group in (
                OrderItem.objects
                .filter(order_id=template.order_id)
                .values_list('sub_group', flat=True)
                .distinct()
            )
            if str(sub_group or '').strip()
        ]
        sub_group = ', '.join(sorted(sub_groups)) or None
        if sub_group:
            Template.objects.filter(temp_id=template.temp_id).update(sub_group=sub_group)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0047_order_quotation_cancelled'),
    ]

    operations = [
        migrations.AddField(
            model_name='template',
            name='sub_group',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.RunPython(backfill_template_sub_group, migrations.RunPython.noop),
    ]
