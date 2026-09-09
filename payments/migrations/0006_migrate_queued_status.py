"""Retire QUEUED.

It existed only because posting was asynchronous. With posting synchronous there
is no queue to wait in, so any document still sitting in QUEUED never reached SAP
and belongs back with its creator.

(The outbox's `sap_response` is carried onto the document in 0005, before the
table is dropped.)

    QUEUED -> PENDING_ERROR   (never posted; creator corrects and resubmits)
"""
from django.db import migrations


def forwards(apps, schema_editor):
    for label in ('PaymentReceipt', 'BankDeposit'):
        model = apps.get_model('payments', label)
        model.objects.filter(status='QUEUED').update(status='PENDING_ERROR')


def backwards(apps, schema_editor):
    """Not a true inverse — PENDING_ERROR now covers both "never queued" and
    "SAP rejected", and the two cannot be told apart afterwards."""
    for label in ('PaymentReceipt', 'BankDeposit'):
        model = apps.get_model('payments', label)
        model.objects.filter(status='POSTING_TO_SAP').update(status='QUEUED')


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0005_remove_sapoutbox_content_type_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
