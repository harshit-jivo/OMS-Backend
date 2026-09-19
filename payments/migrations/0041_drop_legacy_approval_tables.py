"""Drop the retired `approvals` engine's tables, from payments.

WHY THIS LIVES IN `payments` AND NOT IN `approvals`
---------------------------------------------------
Because the `approvals` app no longer exists in this codebase. Its own
`0003_delete_legacy_engine` dropped these tables on TEST while the app was
still present, and then the app was removed — so a deployment running THIS
code has no migration that can reach them, and would leave five orphan tables
behind forever.

This closes that gap: one release, from the code as it stands, ends with the
tables gone. On a database where `approvals/0003` already ran, every statement
is a no-op.

THE AUDIT TRAIL IS PRESERVED FIRST, AND THE DEPENDENCY ENFORCES IT
-------------------------------------------------------------------
`0038_preserve_approval_decisions` copies every approval decision into
`payment_status_history`, and `0039_repair_preserved_timestamps` puts the real
decision times back on them. Both run before this by dependency, so the
tables cannot be dropped on a database that has not kept their contents.

Measured on TEST before this was written: of 165 approval actions, 0 remained
without a counterpart in payments' own history.

NO CASCADE
----------
Dropped leaf-first, so each table's dependents are already gone when it goes:

    approval_action          -> references approval_request
    approval_level_approver  -> references approval_level
    approval_request         -> references approval_workflow
    approval_level           -> references approval_workflow
    approval_workflow        -> referenced by nothing left

Nothing outside this group references any of them (checked against
`pg_constraint`), so explicit ordering is enough and CASCADE is not needed —
which also means this cannot reach a table it was not meant to.

IRREVERSIBLE. The backward pass is a no-op: recreating five empty tables would
restore nothing, and the data lives on in `payment_status_history`.
"""
from django.db import migrations

#: Leaf-first. See the module docstring — this order is what removes the need
#: for CASCADE.
DROP_ORDER = (
    'approval_action',
    'approval_level_approver',
    'approval_request',
    'approval_level',
    'approval_workflow',
)

DROP_SQL = '\n'.join(
    f'DROP TABLE IF EXISTS payments.{table};' for table in DROP_ORDER
)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0040_drop_payment_method_mapping'),
        # Nothing may drop these tables before their contents were copied.
        ('payments', '0039_repair_preserved_timestamps'),
    ]

    operations = [
        migrations.RunSQL(DROP_SQL, migrations.RunSQL.noop),
    ]
