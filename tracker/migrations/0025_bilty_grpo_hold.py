"""Bilty / GRPO can hold an invoice, as Pre-Audit does.

The desk now records a status, like Pre-Audit: OK advances it, HOLD keeps it
(full hold — it stays at the desk until released) or withholds an amount and
advances it (partial hold), RETURN sends it back. DEBIT stays Pre-Audit's: it
puts a figure on the invoice, which is Pre-Audit's job.

Hold itself is stage-agnostic (`services._validate_disposition`, the queue's
Hold tab, Release hold): it follows `status_choices`, so this is configuration.
"""
from django.db import migrations

CHOICES = ['OK', 'HOLD', 'RETURN']


def forwards(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    Stage.objects.filter(code='bilty_grpo').update(status_choices=CHOICES, requires_status=True)


def backwards(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    Stage.objects.filter(code='bilty_grpo').update(status_choices=[], requires_status=False)


class Migration(migrations.Migration):

    dependencies = [('tracker', '0024_alertmute')]

    operations = [migrations.RunPython(forwards, backwards)]
