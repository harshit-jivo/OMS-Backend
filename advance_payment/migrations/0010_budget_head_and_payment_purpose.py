# The form's Department is now SAP's budget head (budget_code), and Payment
# Purpose is the Payment Desk's purpose list. Department and Sub-department
# are no longer asked, so they become optional; nothing is deleted, and the
# requests raised before keep theirs.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0009_request_payment_purpose'),
    ]

    operations = [
        migrations.AlterField(
            model_name='advancerequest',
            name='department',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='requests', to='advance_payment.department'),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='purpose_code',
            field=models.CharField(blank=True, default='', max_length=30),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='purpose_label',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
