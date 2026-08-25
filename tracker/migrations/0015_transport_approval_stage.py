"""Add the Transport Approval desk, a branch off Pre-Audit.

Before: 3 Pre-Audit, 4 Data Entry, 5 SAP Approval, 6 JSAP Approval,
        7 Save in SAP, 8 Payment
After:  3 Pre-Audit, 4 Transport Approval, 5 Data Entry, 6 SAP Approval,
        7 JSAP Approval, 8 Save in SAP, 9 Payment

Only invoices whose category is Transport visit it, and it is not a step along
the line: Pre-Audit sends them there, and both verdicts hand them straight back
to Pre-Audit, which then advances them to Data Entry. Its `order` only places it
in the flow display (and the delete cut-off) — the routing lives in
`services._branch_neighbour()`.

Nothing already in flight is moved: an invoice parked at Pre-Audit picks the
detour up on its next advance.

Stage.order is unique, so the stages that shift down are parked out of range
before being renumbered — otherwise Data Entry -> 5 would collide with SAP
Approval.
"""
from django.db import migrations

# Stages pushed down one place, in the order they end up.
SHIFTED = [
    ('data_entry', 5),
    ('sap_approval', 6),
    ('jsap_approval', 7),
    ('save_in_sap', 8),
    ('payment', 9),
]

TRANSPORT_APPROVAL = dict(
    name='Transport Approval', order=4,
    status_choices=['APPROVED', 'REJECTED'], requires_status=True,
    can_return=True, is_terminal=False, threshold_days=2, is_active=True,
)


def add_stage(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    if not Stage.objects.filter(code='pre_audit').exists():
        return          # fresh install: seed_tracker creates the final layout

    for offset, (code, _) in enumerate(SHIFTED):
        Stage.objects.filter(code=code).update(order=900 + offset)
    Stage.objects.update_or_create(
        code='transport_approval', defaults=dict(TRANSPORT_APPROVAL))
    for code, order in SHIFTED:
        Stage.objects.filter(code=code).update(order=order)


def remove_stage(apps, schema_editor):
    """Reverse: drop the desk, moving anything parked there back to Pre-Audit
    so no invoice is left pointing at a deleted stage."""
    Stage = apps.get_model('tracker', 'Stage')
    Invoice = apps.get_model('tracker', 'Invoice')
    StageEvent = apps.get_model('tracker', 'StageEvent')
    StuckAlert = apps.get_model('tracker', 'StuckAlert')
    AlertNotification = apps.get_model('tracker', 'AlertNotification')
    UserStageAccess = apps.get_model('tracker', 'UserStageAccess')

    ta = Stage.objects.filter(code='transport_approval').first()
    pre_audit = Stage.objects.filter(code='pre_audit').first()
    if not ta or not pre_audit:
        return
    Invoice.objects.filter(current_stage=ta).update(current_stage=pre_audit)
    StageEvent.objects.filter(stage=ta).update(stage=pre_audit)
    StuckAlert.objects.filter(stage=ta).delete()
    AlertNotification.objects.filter(stage=ta).delete()
    UserStageAccess.objects.filter(stage=ta).delete()
    ta.delete()

    for offset, (code, _) in enumerate(SHIFTED):
        Stage.objects.filter(code=code).update(order=900 + offset)
    for code, order in (('data_entry', 4), ('sap_approval', 5),
                        ('jsap_approval', 6), ('save_in_sap', 7),
                        ('payment', 8)):
        Stage.objects.filter(code=code).update(order=order)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0014_split_sap_jsap_stages'),
    ]

    operations = [
        migrations.RunPython(add_stage, remove_stage),
    ]
