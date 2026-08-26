"""Add the TRANSPORT_APPROVAL disposition to the Pre-Audit desk.

Transport-category invoices already detour to the Transport Approval desk on an
ordinary OK. This adds the explicit option, so Pre-Audit can send anything else
there when it needs that desk's sign-off (`services.apply_action` treats the
status as an advance and overrides the route).

Appends rather than replacing, so a status list retuned in Admin is preserved.
"""
from django.db import migrations

STATUS = 'TRANSPORT_APPROVAL'


def add_status(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    stage = Stage.objects.filter(code='pre_audit').first()
    if not stage:
        return
    choices = list(stage.status_choices or [])
    if STATUS in choices:
        return
    # Keep RETURN last — it reads as the odd one out in the dropdown.
    if 'RETURN' in choices:
        choices.insert(choices.index('RETURN'), STATUS)
    else:
        choices.append(STATUS)
    stage.status_choices = choices
    stage.save(update_fields=['status_choices'])


def remove_status(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    stage = Stage.objects.filter(code='pre_audit').first()
    if not stage:
        return
    choices = [c for c in (stage.status_choices or []) if c != STATUS]
    stage.status_choices = choices
    stage.save(update_fields=['status_choices'])


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0015_transport_approval_stage'),
    ]

    operations = [
        migrations.RunPython(add_status, remove_status),
    ]
