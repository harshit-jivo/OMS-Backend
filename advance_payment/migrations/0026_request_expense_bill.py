from django.db import migrations, models


class Migration(migrations.Migration):
    """An Expense paid to a vendor names its bill (2026-10-20): category, number, date.

    Three columns, empty on every existing request.
    """

    dependencies = [
        ('advance_payment', '0025_requestflow_current_role_ca'),
    ]

    operations = [
        migrations.AddField(
            model_name='advancerequest',
            name='expense_category',
            field=models.CharField(blank=True, default='', help_text='Expense to a vendor: ITEM or SERVICE.',
                                   max_length=10),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='expense_bill_number',
            field=models.CharField(blank=True, default='', help_text="Expense to a vendor: the vendor's bill number.",
                                   max_length=100),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='expense_bill_date',
            field=models.DateField(blank=True, help_text="Expense to a vendor: the vendor's bill date.", null=True),
        ),
    ]
