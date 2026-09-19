"""Shared fixtures for the payments workflow tests.

Every payments approval test needs the same four rows of engine
configuration — a module, a workflow, a validated query and one stage per
approver — so they are built here once, the way an administrator would build
them in the Workflows UI.

THE QUERY PROJECTS `doc_no`, WHICH IS THE WHOLE POINT
-----------------------------------------------------
Payments registers ONE module for two document types, and receipt ids and
deposit ids overlap. Selection therefore keys on the document NUMBER, and
every payments query must expose it under the common alias `doc_no`:

    SELECT *, receipt_no AS doc_no FROM payment_receipt
    SELECT *, deposit_no AS doc_no FROM payment_bank_deposit

The leading `SELECT *` is not decoration. Configuration-time validation cannot
know the runtime key column — the module supplies that at submit — so it checks
the DEFAULT one, `id`, and a projection that named only `doc_no` would be
refused before it could ever be stamped.

A query written against the wrong table simply never matches that document,
which is how one module serves both types without ambiguity.

These fixtures are DATABASE-BACKED and PostgreSQL-only: the engine's schema,
its GiST exclusion constraint and `conditions.validate_and_stamp` all need a
real Postgres. There is no SQLite path, deliberately.
"""
from datetime import date
from decimal import Decimal

from workflow.models import (COMPANY_ALL, Workflow, WorkflowModule,
                             WorkflowQuery, WorkflowStage)
from workflow.services import conditions

from payments.apps import MODULE_CODE
from payments.models import BankDeposit, PaymentReceipt

#: The two document tables, and the column each exposes as `doc_no`.
RECEIPT_TABLE = PaymentReceipt._meta.db_table
DEPOSIT_TABLE = BankDeposit._meta.db_table


def make_module():
    """The registry row. Identity only — `code` + `name`, nothing else."""
    module, _ = WorkflowModule.objects.get_or_create(
        code=MODULE_CODE, defaults={'name': 'Payments'})
    return module


#: Marks a workflow as built by these fixtures. `payments_workflow` stands
#: down every OTHER active payments workflow so selection is deterministic, and
#: this is how it tells its own apart — a suite may legitimately build several
#: (one per company, say), and they must not cancel each other out.
FIXTURE_MARK = '[test-fixture]'


def make_workflow(module=None, code='PAY_STD', name='Payments standard',
                  company=None):
    return Workflow.objects.create(
        module=module or make_module(), code=code,
        name=f'{FIXTURE_MARK} {name}',
        company=company or COMPANY_ALL)


def make_query(workflow, *, table, column, name='all', company=None,
               where='', validate=True):
    """A set-selector query projecting `doc_no`, validated like a real one."""
    sql = f'SELECT *, {column} AS doc_no FROM {table}'
    if where:
        sql += f' WHERE {where}'
    query = WorkflowQuery.objects.create(
        workflow=workflow, name=name, query_text=sql,
        company=company or COMPANY_ALL)
    if validate:
        problems = conditions.validate_and_stamp(query)
        assert not problems, f'fixture query failed validation: {problems}'
        query.refresh_from_db()
    return query


def receipt_query(workflow, **kwargs):
    return make_query(workflow, table=RECEIPT_TABLE, column='receipt_no',
                      name=kwargs.pop('name', 'receipts'), **kwargs)


def deposit_query(workflow, **kwargs):
    return make_query(workflow, table=DEPOSIT_TABLE, column='deposit_no',
                      name=kwargs.pop('name', 'deposits'), **kwargs)


def make_stage(workflow, sequence, user, name=None):
    return WorkflowStage.objects.create(
        workflow=workflow, sequence=sequence, user=user,
        name=name or f'Stage {sequence}')


def payments_workflow(*approvers, company=None, code='PAY_STD',
                      documents='both'):
    """One workflow routing payments, with a stage per approver in order.

    `documents` picks which query/queries to attach: 'receipts', 'deposits' or
    'both'. Returns (workflow, [stages]).

    THE WORKFLOW BUILT HERE IS THE ONLY ACTIVE ONE, BY CONSTRUCTION
    ---------------------------------------------------------------
    The engine selects a workflow by RUNNING the configured queries, so any
    other active PAYMENTS workflow in the database takes part in that
    selection. On the shared TEST database there are real ones, configured by
    whoever is working in TEST that day — which makes an unguarded test depend
    on configuration it does not own, and fail in ways that have nothing to do
    with the code under test.

    So the others are stood down first. This happens inside the test's own
    transaction and is rolled back with it, so no live configuration is
    changed; what it buys is a test that gives the same answer today and after
    someone adds a workflow tomorrow.
    """
    (Workflow.objects
     .filter(module__code=MODULE_CODE, is_active=True)
     .exclude(name__startswith=FIXTURE_MARK)
     .update(is_active=False))
    workflow = make_workflow(code=code, company=company)
    if documents in ('receipts', 'both'):
        receipt_query(workflow, company=company)
    if documents in ('deposits', 'both'):
        deposit_query(workflow, company=company)
    stages = [make_stage(workflow, index, approver)
              for index, approver in enumerate(approvers, start=1)]
    return workflow, stages


def make_receipt(creator, *, receipt_no='RCP-OIL-20260918-000001',
                 company='OIL', amount='100.00', verified=True):
    """A receipt ready to submit: verified, advance, one cash line."""
    receipt = PaymentReceipt.objects.create(
        receipt_no=receipt_no, company=company, card_code='CUST1',
        card_name='A Customer', payment_date=date.today(),
        total_amount=Decimal(amount), is_advance=True, sap_branch_id=1,
        created_by=creator,
        verification_status=(PaymentReceipt.VerificationStatus.VERIFIED
                             if verified
                             else PaymentReceipt.VerificationStatus.PENDING))
    return receipt


def make_deposit(creator, *, deposit_no='DEP-OIL-20260918-000001',
                 company='OIL', amount='100.00'):
    return BankDeposit.objects.create(
        deposit_no=deposit_no, company=company, deposit_date=date.today(),
        collected_amount=Decimal(amount), deposit_amount=Decimal(amount),
        bank_key='INB:2201101', bank_code='INB', bank_gl_account='2201101',
        bank_display_name='INDIAN BANK', created_by=creator)
