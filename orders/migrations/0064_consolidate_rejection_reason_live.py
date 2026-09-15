"""Finish what 0063 started, using live's data rather than test's.

0063 assumed `rejection_reason` and `reject_reason` were two columns holding
one fact. On the test database that was true — it measured 132 affected orders
and *zero* conflicts, and it ran there cleanly.

On live the assumption does not hold, and 0063's own guard caught it: 90 orders
hold DIFFERENT text in the two columns. Reading them shows why — the columns
carry two DIFFERENT facts:

    order 1335   rejection_reason 'rate revise'
                 reject_reason    'Sales quotation created by auditor'
    order 1448   rejection_reason 'Wrong party'
                 reject_reason    'Sent to auditor by billing'
    order 1256   rejection_reason 'WRONG'
                 reject_reason    'Approved'

`rejection_reason` holds the human reason. `reject_reason`, on live, has also
been used by the status-transition path to record a TRANSITION REMARK.

That matters because 0063's UPDATE — "copy reject_reason across wherever
rejection_reason is blank" — would have written 1,847 rows here, and 1,503 of
them (81%) carry one of five machine-written transition remarks:

     621  'Approved'
     541  'Sales quotation created by auditor'
     158  'approved by billing'
     128  'Sent to auditor by billing'
      55  'Accepted by billing'

`orders/views/mart.py` serves `rejection_reason` to the MartApproval screen as
the rejection reason, so running 0063 unchanged on live would have displayed
"Approved" as the reason an order was rejected, 621 times over.

So 0063 is faked on live (it genuinely ran on test, and stays as the record of
that), and this migration does the copy that 0063 meant to do: the same
fill-the-blanks UPDATE, minus those five remarks. It moves the ~344 genuine
orphaned reasons — 'Wrong sku', 'Incorrect address', 'rate approval needed' —
onto the canonical column, and leaves the remarks where they are.

The exclusion list is EXACT, case-insensitive matches, not a keyword search.
Keyword matching on "approv" would have swallowed real reasons: 'rate approval
needed' (15 orders), 'SCHEME NOT APPROVED', 'Rates are different from the
approved rates', 'billing said to reject'. The five below are machine-written —
they repeat 55 to 621 times verbatim, and the next most common value appears
18 times and is plainly typed by a person.

No conflict guard is needed: this only ever fills a blank, so it cannot
overwrite anyone's text. It is idempotent, and a no-op on any database where
0063 already ran for real (nothing is left blank there).

Reverse is a no-op, for 0063's reason: a copied value is indistinguishable from
one that was always there, so undoing it would delete legitimate reasons.
`reject_reason` still holds every original.
"""
from django.db import migrations

#: Machine-written status-transition remarks. Exact, case-insensitive.
TRANSITION_REMARKS = (
    'approved',
    'sales quotation created by auditor',
    'approved by billing',
    'sent to auditor by billing',
    'accepted by billing',
)


def forwards(apps, schema_editor):
    schema_editor.execute(
        """
        UPDATE orders
           SET rejection_reason = reject_reason
         WHERE COALESCE(reject_reason, '') <> ''
           AND COALESCE(rejection_reason, '') = ''
           AND lower(btrim(reject_reason)) <> ALL(%s)
        """,
        [list(TRANSITION_REMARKS)],
    )


def backwards(apps, schema_editor):
    """Deliberately does nothing — see the module docstring."""


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0063_consolidate_rejection_reason"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
