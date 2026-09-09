"""Seed the received-from snapshot from the person each receipt already points at.

Run ONCE, immediately after the column is added and BEFORE the application
starts relying on it. At this moment the FK's current name is the best evidence
of the name recorded at creation — there is no earlier record of it anywhere, so
this is a reconstruction, not a copy, and it is only trustworthy because it runs
before any rename that has not already happened.

Receipts with no person keep the blank default: money taken from a party
directly has no collector to name, and inventing one would put a name on a
document nobody wrote it on.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    PaymentReceipt = apps.get_model('payments', 'PaymentReceipt')

    # One UPDATE ... FROM rather than a row loop: the table is small today, but
    # a migration that walks receipts one at a time is the kind that has to be
    # rewritten the first time it meets a real table.
    schema_editor.execute("""
        UPDATE payment_receipt AS r
           SET received_from_name = p.name
          FROM payment_collection_person AS p
         WHERE p.id = r.received_from_person_id
           AND r.received_from_person_id IS NOT NULL
           AND r.received_from_name = ''
    """)


def backwards(apps, schema_editor):
    """Blank the snapshots again.

    Not a no-op: leaving the values behind would make a re-applied forward
    migration skip every row it had already touched (it only fills blanks), so
    a rollback that kept them would quietly poison the retry.
    """
    schema_editor.execute("UPDATE payment_receipt SET received_from_name = ''")


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0028_receipt_received_from_name'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
