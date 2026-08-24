from django.db import migrations, models


def collapse_uoms(apps, schema_editor):
    """Fold the two retired measures onto PCS.

    QTY was pieces under another name, so it maps across unchanged. LTR is a
    derived measure (pack_unit x qty) and has no equivalent slab; PCS is the
    closest honest reading, and any such scheme is reported below so it can be
    re-checked by hand.
    """
    SchemeTrigger = apps.get_model('orders', 'SchemeTrigger')
    SchemeBenefit = apps.get_model('orders', 'SchemeBenefit')

    for model, field in ((SchemeTrigger, 'min_uom'), (SchemeBenefit, 'free_uom')):
        litre_rows = list(
            model.objects.filter(**{f'{field}': 'LTR'}).values_list('scheme__code', flat=True)
        )
        if litre_rows:
            print(
                f'\n  {model.__name__}.{field}: {len(litre_rows)} row(s) were in LTR and '
                f'become PCS — re-check {sorted(set(litre_rows))}'
            )
        model.objects.filter(**{f'{field}__in': ['QTY', 'LTR']}).update(**{field: 'PCS'})


class Migration(migrations.Migration):
    """Schemes are written in pieces or cartons only (UOM_CHOICES).

    Also renames nothing on the DB side — `min_uom` / `free_uom` stay varchar(10);
    only the choices and the default change, plus the data collapse above.
    """

    dependencies = [
        ('orders', '0056_order_warehouse_code'),
    ]

    operations = [
        migrations.RunPython(collapse_uoms, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='schemetrigger',
            name='min_uom',
            field=models.CharField(
                choices=[('PCS', 'Pieces'), ('BOX', 'Boxes')],
                default='PCS',
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name='schemebenefit',
            name='free_uom',
            field=models.CharField(
                choices=[('PCS', 'Pieces'), ('BOX', 'Boxes')],
                default='PCS',
                max_length=10,
            ),
        ),
    ]
