"""Repair payments workflow queries that do not project `doc_no`.

`workflow_queries.query_text` is CONFIGURATION — administrators write it on the
Workflows page — so this migration follows the precedent set by
`backdate/migrations/0007_repoint_condition_queries.py`: a row in a table that
CODE has made wrong is repaired like data, because grep cannot find it and an
administrator has no way to know the rule changed under them.

WHAT MAKES A QUERY WRONG
------------------------
Payments registers ONE module for two document types, and receipt ids and
deposit ids overlap, so selection is keyed on the document NUMBER. Every
payments query must therefore expose it under the common alias `doc_no`
(`payments/workflow_flow.py`). A query without it is accepted at configuration
time — validation checks the engine's default key, `id`, and cannot know the
runtime one — and then fails at SUBMIT with:

    ProgrammingError: column wf_q.doc_no does not exist

which stops every submission it routes. That is the state TEST was found in:
`1ST_APPROVAL_FOR_OIL` selecting from the receipt table with no alias.

WHAT THIS DOES AND DOES NOT DO
------------------------------
It appends the correct projection to PAYMENTS queries that read exactly one of
the two payments tables and do not already project `doc_no`. The column is
chosen by the table the query reads, which is the same rule routing itself
enforces (`workflow_flow.kinds_a_query_serves`), so the repair can never
introduce the cross-routing that rule exists to prevent.

It CREATES NOTHING. Workflows, stages and approver assignments are business
configuration — who may approve what — and a migration must not invent them.
A deployment that has no payments workflow configured still has none after
this runs; the audit command reports that, and an administrator configures it.

It also leaves alone, deliberately:

  * queries belonging to any other module;
  * queries that already project `doc_no`;
  * queries reading BOTH payments tables, or neither — those cannot be
    repaired without guessing which workflow they were meant to serve, and
    guessing is how a receipt ends up in the deposit chain;
  * `validated_at`, for the same reason backdate/0007 leaves it: selection
    refuses a query whose stamp is NULL, so clearing it would swap one broken
    state for another. Adding a projection changes nothing the validator
    checks — still a single SELECT, still no DML, still exposing the default
    key `id`.

REVERSIBLE. The backward pass removes exactly the text the forward pass added,
and only where it added it.
"""
from django.db import migrations

MODULE_CODE = 'PAYMENTS'

#: (table, key column) for each payments document type. Kept in step with
#: `payments.workflow_flow.source_table_for`; asserted by the tests.
RECEIPT = ('payment_receipt', 'receipt_no')
DEPOSIT = ('payment_bank_deposit', 'deposit_no')


def _projection(column):
    return f', {column} AS doc_no'


def _target(query_text):
    """The ONE payments table this query reads, or None if it is not exactly one."""
    lowered = (query_text or '').lower()
    hits = [pair for pair in (RECEIPT, DEPOSIT) if pair[0] in lowered]
    return hits[0] if len(hits) == 1 else None


def _needs_repair(query_text):
    lowered = (query_text or '').lower()
    return 'as doc_no' not in lowered.replace('  ', ' ')


def _forward(apps, schema_editor):
    WorkflowQuery = apps.get_model('workflow', 'WorkflowQuery')
    for query in WorkflowQuery.objects.filter(
            workflow__module__code=MODULE_CODE):
        if not _needs_repair(query.query_text):
            continue
        target = _target(query.query_text)
        if target is None:
            continue
        _table, column = target
        text = query.query_text
        # Inserted after the first SELECT list, which is where a projection
        # belongs; `SELECT *` is what every payments query starts with (the
        # leading `*` is required so configuration-time validation can find the
        # default key `id`).
        marker = 'select *'
        lowered = text.lower()
        if marker not in lowered:
            continue
        at = lowered.index(marker) + len(marker)
        query.query_text = text[:at] + _projection(column) + text[at:]
        query.save(update_fields=['query_text'])


def _backward(apps, schema_editor):
    WorkflowQuery = apps.get_model('workflow', 'WorkflowQuery')
    for query in WorkflowQuery.objects.filter(
            workflow__module__code=MODULE_CODE):
        target = _target(query.query_text)
        if target is None:
            continue
        _table, column = target
        added = _projection(column)
        if added in query.query_text:
            query.query_text = query.query_text.replace(added, '', 1)
            query.save(update_fields=['query_text'])


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0036_workflow_engine_runtime'),
        # The configuration rows this repairs live in the workflow app.
        ('workflow', '0007_drop_runtime_and_testflow'),
    ]

    operations = [
        migrations.RunPython(_forward, _backward),
    ]
