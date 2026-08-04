"""Map the retired status values onto the simplified set.

Migration 0003 narrowed the choices. Existing rows still hold the old values,
and a `status` that is not in `choices` renders as a raw string in the admin and
fails every UI filter, so they are converted here rather than left behind.

    Documents:  FAILED                    -> PENDING_ERROR
    Outbox:     DEAD / NEEDS_REVIEW       -> FAILED
                IN_FLIGHT                 -> PENDING   (never actually posted)
"""
from django.db import migrations

DOC_MAP = {'FAILED': 'PENDING_ERROR'}
OUTBOX_MAP = {'DEAD': 'FAILED', 'NEEDS_REVIEW': 'FAILED', 'IN_FLIGHT': 'PENDING'}


def forwards(apps, schema_editor):
    for label in ('PaymentReceipt', 'BankDeposit'):
        model = apps.get_model('payments', label)
        for old, new in DOC_MAP.items():
            model.objects.filter(status=old).update(status=new)

    SapOutbox = apps.get_model('payments', 'SapOutbox')
    for old, new in OUTBOX_MAP.items():
        SapOutbox.objects.filter(status=old).update(status=new)


def backwards(apps, schema_editor):
    """Reverse to the closest old value.

    Not a perfect inverse — DEAD and NEEDS_REVIEW both collapsed into FAILED, so
    the distinction between them is gone. Recorded here rather than pretending
    otherwise.
    """
    for label in ('PaymentReceipt', 'BankDeposit'):
        model = apps.get_model('payments', label)
        model.objects.filter(status='PENDING_ERROR').update(status='FAILED')


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0003_alter_sapoutbox_options_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
