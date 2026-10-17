from django.db import migrations, models


class Migration(migrations.Migration):
    """Utilities and insurance: the Bill Due Date the requester types (2026-10-17).

    One nullable column, so every existing request keeps working unchanged.
    """

    dependencies = [
        ('advance_payment', '0023_request_document_hold_amount'),
    ]

    operations = [
        migrations.AddField(
            model_name='advancerequest',
            name='bill_due_date',
            field=models.DateField(blank=True, help_text='Utilities / insurance: the due date on the bill.',
                                   null=True),
        ),
    ]
