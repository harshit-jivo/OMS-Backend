"""Remove the Transport Approval desk (added in 0015/0016).

Before: 3 Pre-Audit, 4 Transport Approval, 5 Data Entry, 6 SAP Approval,
        7 JSAP Approval, 8 Save in SAP, 9 Payment
After:  3 Pre-Audit, 4 Data Entry, 5 SAP Approval, 6 JSAP Approval,
        7 Save in SAP, 8 Payment

The desk was live and used, so this is not a clean un-apply:

* Its `StageEvent` rows are **re-pointed at Pre-Audit** rather than deleted, so
  no invoice loses a leg of its timeline. Those visits keep their APPROVED /
  REJECTED status, which Pre-Audit itself never issues — read them as "the
  approval trip that used to be its own desk".
* Pre-Audit events already carrying `stage_status='TRANSPORT_APPROVAL'` are left
  exactly as recorded. The status is gone from the dropdown, so nothing new can
  be written with it; rewriting the old ones would only add a second distortion.
* `UserStageAccess` rows for the desk are dropped (nothing to be assigned to).

Anything parked at the desk is moved to Pre-Audit first, so no invoice is left
pointing at a deleted stage. Stage.order is unique, so the stages that move up
are parked out of range before renumbering.

Reverse re-creates the stage and the Pre-Audit status, but CANNOT tell which
events used to belong to it — that information is gone once this runs.
"""
from django.db import migrations

# Stages that move up one place, in the order they end up.
SHIFTED = [
    ('data_entry', 4),
    ('sap_approval', 5),
    ('jsap_approval', 6),
    ('save_in_sap', 7),
    ('payment', 8),
]

STATUS = 'TRANSPORT_APPROVAL'

TRANSPORT_APPROVAL = dict(
    name='Transport Approval', order=4,
    status_choices=['APPROVED', 'REJECTED'], requires_status=True,
    can_return=True, is_terminal=False, threshold_days=2, is_active=True,
)


def remove_stage(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    Invoice = apps.get_model('tracker', 'Invoice')
    StageEvent = apps.get_model('tracker', 'StageEvent')
    StuckAlert = apps.get_model('tracker', 'StuckAlert')
    AlertNotification = apps.get_model('tracker', 'AlertNotification')
    UserStageAccess = apps.get_model('tracker', 'UserStageAccess')

    pre_audit = Stage.objects.filter(code='pre_audit').first()
    ta = Stage.objects.filter(code='transport_approval').first()

    if ta and pre_audit:
        Invoice.objects.filter(current_stage=ta).update(current_stage=pre_audit)
        StageEvent.objects.filter(stage=ta).update(stage=pre_audit)
        StuckAlert.objects.filter(stage=ta).delete()
        AlertNotification.objects.filter(stage=ta).delete()
        UserStageAccess.objects.filter(stage=ta).delete()
        ta.delete()

        # Close the gap left at order 4.
        for offset, (code, _) in enumerate(SHIFTED):
            Stage.objects.filter(code=code).update(order=900 + offset)
        for code, order in SHIFTED:
            Stage.objects.filter(code=code).update(order=order)

    # Drop the manual "send for transport approval" disposition.
    if pre_audit and STATUS in (pre_audit.status_choices or []):
        pre_audit.status_choices = [c for c in pre_audit.status_choices
                                    if c != STATUS]
        pre_audit.save(update_fields=['status_choices'])


def restore_stage(apps, schema_editor):
    """Put the desk back in the flow. History is NOT restored — the events this
    migration moved are indistinguishable from real Pre-Audit ones by then."""
    Stage = apps.get_model('tracker', 'Stage')
    if not Stage.objects.filter(code='pre_audit').exists():
        return

    for offset, (code, _) in enumerate(SHIFTED):
        Stage.objects.filter(code=code).update(order=900 + offset)
    Stage.objects.update_or_create(
        code='transport_approval', defaults=dict(TRANSPORT_APPROVAL))
    for offset, (code, _) in enumerate(SHIFTED):
        Stage.objects.filter(code=code).update(order=5 + offset)

    pre_audit = Stage.objects.filter(code='pre_audit').first()
    choices = list(pre_audit.status_choices or [])
    if STATUS not in choices:
        if 'RETURN' in choices:
            choices.insert(choices.index('RETURN'), STATUS)
        else:
            choices.append(STATUS)
        pre_audit.status_choices = choices
        pre_audit.save(update_fields=['status_choices'])


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0016_pre_audit_transport_approval_status'),
    ]

    operations = [
        migrations.RunPython(remove_stage, restore_stage),
    ]
