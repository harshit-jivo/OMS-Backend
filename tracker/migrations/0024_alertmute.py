"""The per-visit "stop emailing me about this one" flag.

A new table only — nothing existing is touched, so this is safe to apply to the
production database without a rehearsal. See `tracker.models.AlertMute` for why
the key is the stage VISIT rather than the invoice.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('tracker', '0023_transport_approval_stage'),
    ]

    operations = [
        migrations.CreateModel(
            name='AlertMute',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name='ID')),
                ('stage_entered_at', models.DateTimeField()),
                ('reason', models.TextField()),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('cleared_at', models.DateTimeField(blank=True, null=True)),
                ('cleared_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='tracker_alert_mutes_cleared',
                    to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='tracker_alert_mutes',
                    to=settings.AUTH_USER_MODEL)),
                ('invoice', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='alert_mutes', to='tracker.invoice')),
                ('stage', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='alert_mutes', to='tracker.stage')),
            ],
            options={
                'db_table': 'tracker_alert_mute',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['is_active', 'stage'],
                                         name='tracker_ale_is_acti_50a2e5_idx')],
                'unique_together': {('invoice', 'stage', 'stage_entered_at')},
            },
        ),
    ]
