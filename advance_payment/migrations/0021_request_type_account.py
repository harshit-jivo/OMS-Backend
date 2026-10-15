# State only: a new choice for request_type (no column changes).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0020_payment_against_journal'),
    ]

    operations = [
        migrations.AlterField(
            model_name='advancerequest',
            name='request_type',
            field=models.CharField(choices=[('VENDOR', 'Vendor'), ('EMPLOYEE_ADVANCE', 'Employee'), ('EMPLOYEE_IMPREST', 'Employee Imprest'), ('CUSTOMER', 'Customer'), ('EXPENSE', 'Expense'), ('ACCOUNT', 'Account Payment')], max_length=20),
        ),
    ]
