"""Shared fixtures for the workflow tests."""
from django.contrib.auth import get_user_model

from workflow.models import (
    CompanyScope,
    TestDocument,
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
)
from workflow.services import conditions

User = get_user_model()

#: The harness module's own relation, as written in a configured query.
#:
#: NOTE the form. Django's `db_table = 'workflow"."workflow_test_document'` is
#: a quoting trick — Django wraps it, emitting `"workflow"."workflow_test_document"`.
#: A configured query is RAW SQL, so it must use the ordinary dotted form;
#: `workflow"."x` in raw SQL parses as the identifier `workflow` followed by a
#: quoted `.`, which is not a table reference at all.
TESTDOC_TABLE = 'workflow.workflow_test_document'
TESTFLOW_TABLE = 'workflow.workflow_test_flow'


def make_user(username, **kwargs):
    return User.objects.create_user(
        username=username,
        password='test-pass-12345',
        **kwargs,
    )


def make_module(code='TESTFLOW'):
    return WorkflowModule.objects.create(
        code=code,
        name='Workflow Test Harness',
        business_table=TESTDOC_TABLE,
        business_key_column='id',
        flow_table=TESTFLOW_TABLE,
        flow_model='workflow.TestFlow',
    )


def make_workflow(module, code='WF_STD', name='Standard', company=None):
    """A workflow. `company=None` means scope ALL; a code means SPECIFIC."""
    return Workflow.objects.create(
        module=module, code=code, name=name,
        company_scope=CompanyScope.ALL if company is None
        else CompanyScope.SPECIFIC,
        company=company,
    )


def make_query(workflow, name='all', where='', company=None, validate=True):
    """A configured set-selector query, validated by default.

    Mirrors the JSAP shape: the query selects a SET of documents and carries
    no document parameter of its own. The engine adds the bound predicate.

    `company=None` means scope ALL; a code means SPECIFIC.
    """
    sql = f'SELECT * FROM {TESTDOC_TABLE}'
    if where:
        sql += f' WHERE {where}'
    query = WorkflowQuery.objects.create(
        workflow=workflow, name=name, query_text=sql,
        company_scope=CompanyScope.ALL if company is None
        else CompanyScope.SPECIFIC,
        company=company,
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


def make_document(company='OIL', amount=1000, **kwargs):
    return TestDocument.objects.create(
        company=company, amount=amount, **kwargs)
