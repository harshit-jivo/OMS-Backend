"""Move the payments module's tables into their own PostgreSQL schema.

Everything the module owns — its own tables, the approval engine it runs on,
and the document-number counter — moves from `public` into `payments`, so the
whole feature appears as one group in pgAdmin instead of being scattered among
orders, users and Django's own tables.

Nothing about the tables themselves changes: no column, index, constraint or
row is touched. `ALTER TABLE ... SET SCHEMA` only moves the namespace, and
PostgreSQL rewrites dependent foreign keys automatically — including the ones
pointing OUT to `public.users_user` and `public.django_content_type`, which is
why `public` has to stay on the connection's search_path (see settings.py).

Django keeps addressing the tables by bare name, so no model, `db_table` or
query changes. That is also what makes this reversible: the down migration
moves them straight back.

Runs only on PostgreSQL. The guard matters because the test suite may use
SQLite, which has no schemas and would otherwise fail on the first statement.
"""
from django.db import migrations

SCHEMA = 'payments'

# Every table the payments module owns, across its four apps. Listed
# explicitly rather than discovered at runtime so this migration does exactly
# what it says, whatever the models look like when it eventually runs.
TABLES = [
    # payments
    'payment_sap_company_map',
    'payment_collection_person',
    'payment_method_mapping',
    'payment_receipt',
    'payment_method_entry',
    'payment_cash_denomination',
    'payment_invoice_allocation',
    'payment_bank_deposit',
    'payment_bank_deposit_line',
    'payment_sap_call_log',
    'payment_sap_posting_history',
    'payment_status_history',
    # attachments (payment_attachment is this module's own file store)
    'payment_attachment',
    # approvals — the engine exists for this module and nothing else uses it
    'approval_workflow',
    'approval_level',
    'approval_level_approver',
    'approval_request',
    'approval_action',
    # core
    'core_document_counter',
]


def _move(apps, schema_editor, target, source):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS {SCHEMA}')
        for table in TABLES:
            # to_regclass returns NULL rather than raising when the table is
            # not where we expect, so a partially-applied state can be re-run
            # instead of dying half way through.
            cursor.execute('SELECT to_regclass(%s)', [f'{source}.{table}'])
            if cursor.fetchone()[0] is None:
                continue
            cursor.execute(
                f'ALTER TABLE {source}."{table}" SET SCHEMA {target}')


def forwards(apps, schema_editor):
    _move(apps, schema_editor, SCHEMA, 'public')


def backwards(apps, schema_editor):
    _move(apps, schema_editor, 'public', SCHEMA)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0011_alter_paymentmethodentry_method_paymentmethodmapping'),
        # The `approvals` app was REMOVED once payments moved to the
        # Workflow Engine. Its dependency edge is dropped with it —
        # Django only needs the graph to resolve, and an applied
        # migration is never re-run. The body above already tolerates
        # the models being absent (`LookupError`), so this migration
        # still applies cleanly to a fresh database.
        ('attachments', '0001_initial'),
        ('core', '0002_alter_documentcounter_fiscal_year'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
