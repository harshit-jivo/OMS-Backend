"""Deposits count CASH only; cheques become a record, not an amount.

WHY. `collected_amount` used to add cash and cheques into one figure, and the
SAP posting ignored `deposit_amount` entirely and sent the full cash share of
the linked receipts. Both are wrong for the same reason: a cheque reached the
bank when its own RECEIPT posted, so it is already in SAP, and the AP team
routinely banks less cash than was collected (which is what `shortfall_reason`
records). Observed on DEP-OIL-20260919-000002 — SAP credited 618,000 out of the
cash drawer against 2,091 actually banked.

After this, `collected_amount` is the cash in the linked receipts,
`deposit_amount` is the cash banked, and the SAP document is `deposit_amount`
exactly.

SAFETY. Read the data before changing it; every UPDATE is keyed on values this
migration computes from the rows themselves, and nothing touches SAP. The three
statements are ordered so no constraint is ever violated mid-flight:

  1. drop the `> 0` constraint FIRST, because step 2 legitimately writes zeros
  2. rewrite the amounts
  3. add the `>= 0` constraint back

REVERSIBILITY. Backwards restores the old constraint and recomputes
`collected_amount` as cash + cheque, which is what it held before. It cannot
restore a `deposit_amount` that this migration lowered — the old value was the
cash-plus-cheque total and carried no record of the cash share — so a rollback
leaves those rows with the truthful smaller figure. That is deliberate: the
alternative is re-introducing a number we know to be wrong.
"""
from django.db import migrations, models
from django.db.models import Q


# The cash in a deposit, from its receipts' method entries. Written as raw SQL
# rather than the ORM so the migration does not depend on the current shape of
# payments.models, which will keep moving.
CASH_PER_DEPOSIT = """
    SELECT d.id AS deposit_id,
           COALESCE(SUM(m.amount) FILTER (WHERE m.method = 'CASH'), 0) AS cash,
           COALESCE(SUM(m.amount) FILTER (WHERE m.method = 'CHEQUE'), 0) AS chq
    FROM payment_bank_deposit d
    JOIN payment_bank_deposit_line l ON l.deposit_id = d.id
    JOIN payment_method_entry m ON m.receipt_id = l.receipt_id
    GROUP BY d.id
"""


def to_cash_only(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        # BOTH AMOUNTS IN ONE STATEMENT, and it has to be one.
        #
        # `bank_deposit_shortfall_requires_reason` fires on any row where
        # deposit_amount < collected_amount with an empty reason. Lowering
        # deposit_amount in a statement of its own leaves exactly that state
        # for as long as collected_amount is still the old cash+cheque figure,
        # and PostgreSQL checks the constraint per statement, not at commit.
        # DEP-OIL-20260907-000001 (147,440 banked in full, 97,440 of it cash)
        # failed on precisely this. Moving both together means the row is never
        # observed inconsistent.
        #
        # LEAST, not a bare assignment: a deposit banked in FULL had
        # deposit_amount == old collected (cash + cheque), and its honest new
        # value is the whole cash share — but a deposit already recorded as
        # SHORT must keep the smaller figure its reason explains. LEAST is both
        # rules at once, and it also guarantees
        # `bank_deposit_not_over_collected` can never be violated.
        cur.execute(f"""
            UPDATE payment_bank_deposit d
               SET collected_amount = c.cash,
                   deposit_amount   = LEAST(d.deposit_amount, c.cash)
              FROM ({CASH_PER_DEPOSIT}) c
             WHERE c.deposit_id = d.id
               AND (d.collected_amount IS DISTINCT FROM c.cash
                    OR d.deposit_amount > c.cash)
        """)

        # Re-derive the label from the contents. MIXED is gone, and the column
        # was unreliable regardless — most MIXED rows held pure cash.
        cur.execute(f"""
            UPDATE payment_bank_deposit d
               SET deposit_type = CASE WHEN c.cash > 0 THEN 'CASH' ELSE 'CHEQUE' END
              FROM ({CASH_PER_DEPOSIT}) c
             WHERE c.deposit_id = d.id
               AND d.deposit_type IS DISTINCT FROM
                   (CASE WHEN c.cash > 0 THEN 'CASH' ELSE 'CHEQUE' END)
        """)

        # A deposit with no lines at all cannot be classified from contents and
        # has no cash; leave its amounts alone but make the label honest.
        cur.execute("""
            UPDATE payment_bank_deposit d
               SET deposit_type = 'CASH'
             WHERE d.deposit_type = 'MIXED'
               AND NOT EXISTS (SELECT 1 FROM payment_bank_deposit_line l
                                WHERE l.deposit_id = d.id)
        """)

        # Fire the deferred FK triggers these UPDATEs queued. Without this the
        # AddConstraint that follows fails with "cannot ALTER TABLE ... because
        # it has pending trigger events": PostgreSQL refuses to alter a table
        # inside a transaction that still has trigger events outstanding
        # against it, and the whole migration is one transaction.
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')


def to_cash_plus_cheque(apps, schema_editor):
    """Restore `collected_amount` to cash + cheque. See REVERSIBILITY above."""
    with schema_editor.connection.cursor() as cur:
        cur.execute(f"""
            UPDATE payment_bank_deposit d
               SET collected_amount = c.cash + c.chq
              FROM ({CASH_PER_DEPOSIT}) c
             WHERE c.deposit_id = d.id
               AND d.collected_amount IS DISTINCT FROM (c.cash + c.chq)
        """)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0041_drop_legacy_approval_tables'),
    ]

    operations = [
        # 1. Out of the way before the backfill writes zeros for cheque-only
        #    deposits. Dropping a CHECK is a catalogue-only operation.
        migrations.RemoveConstraint(
            model_name='bankdeposit',
            name='bank_deposit_amount_positive',
        ),
        # 2. The data.
        migrations.RunPython(to_cash_only, to_cash_plus_cheque),
        # 3. Back on, now permitting zero.
        migrations.AddConstraint(
            model_name='bankdeposit',
            constraint=models.CheckConstraint(
                condition=Q(deposit_amount__gte=0),
                name='bank_deposit_amount_positive'),
        ),
        # State-only for Django: `choices` is not represented in PostgreSQL, so
        # this emits no SQL. Listed so makemigrations stays quiet.
        migrations.AlterField(
            model_name='bankdeposit',
            name='deposit_type',
            field=models.CharField(
                choices=[('CASH', 'Cash'), ('CHEQUE', 'Cheque')],
                default='CASH', max_length=10),
        ),
    ]
