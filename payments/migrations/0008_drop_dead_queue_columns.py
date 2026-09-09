"""Drop five verified-dead columns. See PAYMENTS_DATABASE_AUDIT.md.

Each was confirmed unreferenced across the backend, the React Native app and the
React web app, and confirmed constant-or-empty in the live database:

  paymentreceipt.idempotency_key   de-duplicated the OUTBOX, which no longer
  bankdeposit.idempotency_key      exists. Never sent to SAP (no UDF holds it);
                                   the SAP link is sap_doc_entry. Drops a
                                   UNIQUE index off the insert path of both
                                   document tables.

  paymentreceipt.deposit           a second, UNCONSTRAINED path to the receipt
                                   -> deposit link already owned by
                                   BankDepositLine, whose UniqueConstraint on
                                   `receipt` is what stops a receipt being
                                   banked twice. Never written; 0 of 2 live rows
                                   populated, and 0 rows disagreed with the line
                                   table at the time of the drop.

  sapcalllog.http_method           never assigned — every row held the model
                                   default 'POST'.
  sapcalllog.attempt_number        never assigned — every row held 1. Retries do
                                   not exist under synchronous posting; attempt
                                   sequencing lives on SapPostingHistory.

No business logic, validation, payload, permission or approval behaviour changes.
Irreversible by design: RemoveField cannot restore dropped values, and none of
these carried information worth restoring.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0007_sappostinghistory'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='bankdeposit',
            name='idempotency_key',
        ),
        migrations.RemoveField(
            model_name='paymentreceipt',
            name='deposit',
        ),
        migrations.RemoveField(
            model_name='paymentreceipt',
            name='idempotency_key',
        ),
        migrations.RemoveField(
            model_name='sapcalllog',
            name='attempt_number',
        ),
        migrations.RemoveField(
            model_name='sapcalllog',
            name='http_method',
        ),
    ]
