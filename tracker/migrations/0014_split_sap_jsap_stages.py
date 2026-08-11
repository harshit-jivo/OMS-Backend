"""Split the single "SAP / JSAP Approval" desk into two stages.

Before: 5 SAP / JSAP Approval, 6 Save in SAP, 7 Payment
After:  5 SAP Approval, 6 JSAP Approval, 7 Save in SAP, 8 Payment

Invoices already sitting at the old combined stage stay on `sap_approval`
(renamed in place, so its id, history and user mappings are preserved) and
will pass through the new JSAP desk on their next advance.

Stage.order is unique, so later stages are pushed out of range before being
renumbered — otherwise Save in SAP -> 7 would collide with Payment.
"""
from django.db import migrations

SHIFTED = [('save_in_sap', 7), ('payment', 8)]


def split_stages(apps, schema_editor):
    Stage = apps.get_model('tracker', 'Stage')
    if not Stage.objects.filter(code='sap_approval').exists():
        return          # fresh install: seed_tracker creates the final layout

    # Park the stages that move, so renumbering can't hit the unique order.
    for offset, (code, _) in enumerate(SHIFTED):
        Stage.objects.filter(code=code).update(order=900 + offset)

    Stage.objects.filter(code='sap_approval').update(name='SAP Approval', order=5)
    Stage.objects.update_or_create(
        code='jsap_approval',
        defaults=dict(
            name='JSAP Approval', order=6,
            status_choices=['APPROVED', 'REJECTED'], requires_status=True,
            can_return=True, is_terminal=False, threshold_days=3, is_active=True,
        ),
    )
    for code, order in SHIFTED:
        Stage.objects.filter(code=code).update(order=order)


def merge_stages(apps, schema_editor):
    """Reverse: fold JSAP back into the combined desk.

    Anything parked at the JSAP desk is moved back to SAP Approval first, so
    no invoice is left pointing at a deleted stage.
    """
    Stage = apps.get_model('tracker', 'Stage')
    Invoice = apps.get_model('tracker', 'Invoice')
    StageEvent = apps.get_model('tracker', 'StageEvent')

    jsap = Stage.objects.filter(code='jsap_approval').first()
    sap = Stage.objects.filter(code='sap_approval').first()
    if not jsap or not sap:
        return
    Invoice.objects.filter(current_stage=jsap).update(current_stage=sap)
    StageEvent.objects.filter(stage=jsap).update(stage=sap)
    jsap.delete()

    for offset, (code, _) in enumerate(SHIFTED):
        Stage.objects.filter(code=code).update(order=900 + offset)
    Stage.objects.filter(code='sap_approval').update(name='SAP / JSAP Approval', order=5)
    for code, order in (('save_in_sap', 6), ('payment', 7)):
        Stage.objects.filter(code=code).update(order=order)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0013_invoice_hold_amount_paymentdetail_hold_added_back'),
    ]

    operations = [
        migrations.RunPython(split_stages, merge_stages),
    ]
