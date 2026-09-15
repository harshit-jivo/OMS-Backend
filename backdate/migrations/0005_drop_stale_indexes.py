"""Drop the three indexes 0004 replaced but left behind.

`RemoveIndex` updated the migration STATE but did not emit a `DROP INDEX` for
these — the models had `db_table` in the `schema"."table` form, which is the
same quoting quirk that made the table renames in 0004 need raw SQL. The result
was two indexes covering the same columns:

    bkdt_req_creator_idx   == bkdt_creator_idx        (created_by, -created_at)
    bkdt_req_company_idx   == bkdt_company_idx        (company)
    bkdt_flow_status_idx   -> a prefix of bkdt_flow_queue_idx (status, ...)

A duplicate index is not wrong, it is just paid for twice on every write. There
is no state change here, so this is database-only: the models already say these
do not exist.
"""
from django.db import migrations

STALE = [
    'backdate.bkdt_req_creator_idx',
    'backdate.bkdt_req_company_idx',
    'backdate.bkdt_flow_status_idx',
]


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0004_clean_runtime_model'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[f'DROP INDEX IF EXISTS {name};' for name in STALE],
            # Irreversible by design: re-creating them would restore the
            # duplication. The model state never claimed they existed.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
