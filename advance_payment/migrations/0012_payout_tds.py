# TDS deducted at the Payment stage on vendor requests: which SAP TDS code,
# rate, payable account and amount, on the payout; and the journal entry each
# posting attempt booked it with.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0011_customer_refund_and_ledger_items'),
    ]

    operations = [
        migrations.AddField(
            model_name='payout',
            name='tds_code',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='payout',
            name='tds_label',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='payout',
            name='tds_rate',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name='payout',
            name='tds_account',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='payout',
            name='tds_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=19),
        ),
        migrations.AddField(
            model_name='sapvoucher',
            name='tds_trans_id',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
