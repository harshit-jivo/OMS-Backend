# The Accounts desk's changes to an Item bill's GRPO user fields (2026-10-22).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0028_request_director_approval'),
    ]

    operations = [
        migrations.AddField(
            model_name='advancerequest',
            name='expense_grpo_fields',
            field=models.JSONField(blank=True, default=None, help_text="Item bill: the Accounts desk's changes to its GRPO's user fields (bilty, vehicle, litres...).", null=True),
        ),
    ]
