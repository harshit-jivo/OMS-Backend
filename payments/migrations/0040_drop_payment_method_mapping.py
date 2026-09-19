"""Drop `payment_method_mapping` — the account picker replaced it.

WHAT THE TABLE DID
------------------
One admin-chosen SAP account per payment method per company. Every receipt
resolved its G/L through it at posting time, so all a company's cash went to
one drawer and all its transfers to one bank, whatever the collector actually
did with the money.

The collector now chooses the receiving account on the payment itself and the
choice is frozen onto the line, which is both more accurate and historically
stable — a later edit cannot move money that has already been banked.

WHY THIS IS SAFE NOW, AND WAS NOT BEFORE
-----------------------------------------
The fallback was load-bearing until today. Before this migration was written,
`backfill_receiving_accounts` stamped the account the mapping WOULD have
chosen onto every line that could still be read:

    26 payment lines   (20 receipts awaiting SAP, plus depositable cash)
     7 deposit sources (frozen from their own receipts, not from the mapping)

Lines on receipts already posted AND already banked were deliberately left
blank: nothing re-resolves them, and the mapping had been edited since some of
them posted, so stamping today's value would have recorded an account SAP may
never have used.

Verified immediately before applying: 0 lines and 0 deposits still resolved
through the mapping. One MART draft has no account, but the mapping had none
for it either — it could not post before this change and cannot after, and its
owner picks an account in the app like any other receipt.

CASCADE is Django's own `DeleteModel` SQL. Checked against `pg_constraint`
first: nothing references this table, so it can reach nothing else.

IRREVERSIBLE. The backward pass would recreate an empty table that no code
reads.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0039_repair_preserved_timestamps'),
    ]

    operations = [
        migrations.DeleteModel(
            name='PaymentMethodMapping',
        ),
    ]
