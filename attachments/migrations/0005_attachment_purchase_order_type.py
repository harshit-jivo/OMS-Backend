"""Add PURCHASE_ORDER: a sales order's customer PO, attached on Add Sales.

Choices only — Django keeps them in Python, so this changes no column.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('attachments', '0004_drop_stored_and_original_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='attachment',
            name='attachment_type',
            field=models.CharField(
                choices=[
                    ('CHEQUE_IMAGE', 'Cheque image'),
                    ('UPI_SCREENSHOT', 'UPI screenshot'),
                    ('DEPOSIT_SLIP', 'Bank deposit slip'),
                    ('DEPOSIT_RECEIPT', 'Deposit receipt'),
                    ('PURCHASE_ORDER', 'Purchase order'),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
    ]
