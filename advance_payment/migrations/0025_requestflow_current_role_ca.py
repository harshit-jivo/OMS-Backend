from django.db import migrations, models


class Migration(migrations.Migration):
    """The CA desk (Navdeep, 2026-10-19): a new stage role after Payment.

    Choices only: no SQL is run (`sqlmigrate` shows an empty migration).
    """

    dependencies = [
        ('advance_payment', '0024_request_bill_due_date'),
    ]

    operations = [
        migrations.AlterField(
            model_name='requestflow',
            name='current_role',
            field=models.CharField(blank=True, choices=[
                ('APPROVAL', 'Approval'), ('PAYMENT', 'Payment Approval'), ('CA', 'CA Approval'),
                ('AUDIT', 'Audit Approval'), ('FINAL', 'Final Approval'),
            ], default='', max_length=10),
        ),
    ]
