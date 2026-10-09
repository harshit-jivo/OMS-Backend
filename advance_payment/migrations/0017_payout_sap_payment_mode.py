from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0016_expense_requests'),
    ]

    operations = [
        migrations.AddField(
            model_name='payout',
            name='sap_payment_mode',
            field=models.CharField(blank=True, default='', max_length=10),
        ),
    ]
