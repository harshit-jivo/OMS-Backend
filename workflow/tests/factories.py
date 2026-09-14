"""Shared fixtures for the workflow tests.

THERE IS NO TEST MODULE TABLE, DELIBERATELY
-------------------------------------------
These tests used to run against a `TestDocument` / `TestFlow` harness that
lived as real tables inside the `workflow` schema. That harness is gone: it
made the engine ship a permanent fake business module, and its flow table
encoded the runtime shape the engine no longer owns.

A condition query is just SQL over whatever relation the business module
names, so the fixtures here stand a REAL, already-present table in for a
module's document table — `users_user`, which every deployment has and whose
rows these tests create anyway for stage users. A "document" is therefore a
`User` row, and `DOCUMENT_TABLE` is what a query selects from. That exercises
the identical code path (validate → bind `id` → execute) with nothing added to
the schema.
"""
from django.contrib.auth import get_user_model

from workflow.models import (
    COMPANY_ALL,
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
)
from workflow.services import conditions

User = get_user_model()

#: The relation a fixture query selects from, written as it must appear in RAW
#: SQL. A module would name its own document table here; these tests borrow a
#: table that already exists rather than creating one under `workflow`.
DOCUMENT_TABLE = User._meta.db_table


def make_user(username, **kwargs):
    return User.objects.create_user(
        username=username,
        password='test-pass-12345',
        **kwargs,
    )


def make_module(code='BUDGET', name='Budget Approval'):
    """A registry row. Identity only — the registry holds nothing else."""
    return WorkflowModule.objects.create(code=code, name=name)


def make_workflow(module, code='WF_STD', name='Standard', company=None):
    """A workflow. `company=None` is shorthand for `ALL` in these fixtures."""
    return Workflow.objects.create(
        module=module, code=code, name=name,
        company=company or COMPANY_ALL,
    )


def make_query(workflow, name='all', where='', company=None, validate=True):
    """A configured set-selector query, validated by default.

    The query selects a SET of documents and carries no document parameter of
    its own; the engine adds the bound predicate at evaluation time.

    `company=None` is shorthand for `ALL` in these fixtures.
    """
    sql = f'SELECT * FROM {DOCUMENT_TABLE}'
    if where:
        sql += f' WHERE {where}'
    query = WorkflowQuery.objects.create(
        workflow=workflow, name=name, query_text=sql,
        company=company or COMPANY_ALL,
    )
    if validate:
        problems = conditions.validate_and_stamp(query)
        assert not problems, f'fixture query failed validation: {problems}'
        query.refresh_from_db()
    return query


def make_stage(workflow, sequence, user, name=None):
    return WorkflowStage.objects.create(
        workflow=workflow,
        sequence=sequence,
        user=user,
        name=name or f'Stage {sequence}',
    )


def make_document(username=None, **kwargs):
    """A stand-in business document.

    Returns a `User`, because that is the table the fixture queries read. What
    matters to the engine is only that it has an `id` to bind.
    """
    import uuid
    return make_user(username or f'doc-{uuid.uuid4().hex[:10]}', **kwargs)
