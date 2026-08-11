from django.db import migrations, models


def backfill_effective_month(apps, schema_editor):
    """Existing rows get an effective_month derived from their invoice_date
    (first day of that invoice's month), so the column can become mandatory."""
    Invoice = apps.get_model('tracker', 'Invoice')
    # Historical model exposes a plain `objects` manager that returns every row
    # (including soft-deleted ones), which is exactly what we want to backfill.
    for inv in Invoice.objects.all().only('id', 'invoice_date', 'effective_month'):
        d = inv.invoice_date
        inv.effective_month = d.replace(day=1) if d else None
        inv.save(update_fields=['effective_month'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0010_invoice_rejection_pending'),
    ]

    operations = [
        # 1) add as nullable so existing rows are accepted
        migrations.AddField(
            model_name='invoice',
            name='effective_month',
            field=models.DateField(null=True),
        ),
        # 2) backfill from invoice_date
        migrations.RunPython(backfill_effective_month, noop),
        # 3) enforce NOT NULL (mandatory going forward)
        migrations.AlterField(
            model_name='invoice',
            name='effective_month',
            field=models.DateField(),
        ),
    ]
