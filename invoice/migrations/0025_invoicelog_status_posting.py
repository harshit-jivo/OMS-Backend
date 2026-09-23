from django.db import migrations, models


class Migration(migrations.Migration):
    """Add POSTING to InvoiceLog.status choices. Choices only: no schema change."""

    dependencies = [
        ('invoice', '0024_restore_invocie_history_device_columns'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoicelog',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'), ('EDITED', 'Edited'), ('ERROR', 'Error'), ('POSTING', 'Posting to SAP'), ('POSTED_TO_SAP', 'Posted to SAP'), ('CL_RAISED', 'CL Raised')], default='PENDING', max_length=20),
        ),
    ]
