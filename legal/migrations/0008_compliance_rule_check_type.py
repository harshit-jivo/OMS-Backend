"""Split the rule book into rules the AI judges and rules we measure.

Both fields are additive with defaults, so every existing row keeps behaving
exactly as it does today: `check_type` defaults to AI, which is what all
twenty-one seeded rules are, and `params` defaults to an empty dict that
nothing reads for an AI rule.

`params` is a JSONField rather than a column per setting because the settings
are per-check and there are only ever a handful — the colour tolerance is the
only one today. A column would have to be added, migrated and deployed for the
next one, which is the deploy-to-change-a-rule problem `ComplianceRule` was
created to get rid of.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('legal', '0007_backfill_preview_paths'),
    ]

    operations = [
        migrations.AddField(
            model_name='compliancerule',
            name='check_type',
            field=models.CharField(
                choices=[
                    ('AI', 'AI — judged from the label image'),
                    ('MEASUREMENT',
                     'Measurement — computed from package dimensions'),
                ],
                default='AI',
                help_text='AI rules are judged from the label image. '
                          'Measurement rules are computed from the dimensions '
                          'entered on the check form and are never sent to the '
                          'AI — their code must be one the backend implements.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='compliancerule',
            name='params',
            field=models.JSONField(
                blank=True, default=dict,
                help_text='Optional settings for a measurement rule, e.g. '
                          '{"tolerance_de": 10} for the fortification colour '
                          'check. Ignored by AI rules.',
            ),
        ),
    ]
