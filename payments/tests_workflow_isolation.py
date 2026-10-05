"""A receipt is never approved by the deposit workflow, or the reverse.

Payments runs TWO business workflows through ONE engine module. They share the
module, the flow machinery, the history and the notifications — and they must
share nothing about ROUTING:

    RCP-000123  ->  receipt workflow   ->  receipt approvers   ->  receipt SAP
    DEP-000123  ->  deposit workflow   ->  deposit approvers   ->  deposit SAP

WHY THIS NEEDS ITS OWN SUITE
----------------------------
Selection is driven by configuration, not by code: the engine runs every active
query for the module and takes the workflow whose query matched. Left at that,
the only thing keeping a receipt out of the deposit chain is that their numbers
happen to be prefixed differently — a convention in DATA. One query written
against the wrong table, or one document numbered by hand, and a receipt could
be approved by deposit approvers and posted down the deposit path with nothing
in the code objecting.

So `workflow_flow._guard_routing` checks the matched query against the table it
actually reads, and these tests are what hold that guard honest. Several of
them construct configurations no administrator would write on purpose —
including a deposit numbered `RCP-…` — because the guard exists precisely for
the case where the convention has already been broken.

PostgreSQL only: selection runs the configured queries for real.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from users.models import User, UserRole
from workflow.models import COMPANY_ALL, WorkflowQuery
from workflow.services import conditions

from . import workflow_flow
from .models import BankDeposit, PaymentMethodEntry, PaymentReceipt
from .permissions import (DEPOSIT_APPROVE, DEPOSIT_CREATE, PAYMENTS_APPROVE,
                          PAYMENTS_CREATE, may_act_on)
from .tests_support import uniq
from .tests_workflow_fixtures import (DEPOSIT_TABLE, RECEIPT_TABLE,
                                      make_stage, make_workflow,
                                      payments_workflow)


def _user(prefix, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=uniq(prefix), name=prefix.title(), role=role,
        extra_pages=list(keys))


class _Base(TestCase):
    def setUp(self):
        super().setUp()
        self.creator = _user('iso_creator-', [PAYMENTS_CREATE, DEPOSIT_CREATE])
        self.receipt_approver = _user('iso_rcp_appr-', [PAYMENTS_APPROVE])
        self.deposit_approver = _user('iso_dep_appr-', [DEPOSIT_APPROVE])

    def _receipt(self, number=None):
        receipt = PaymentReceipt.objects.create(
            receipt_no=number or uniq('RCP-ISO-'), company='OIL',
            card_code='CUST1', payment_date=date.today(),
            total_amount=Decimal('100.00'), is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        return receipt

    def _deposit(self, number=None):
        return BankDeposit.objects.create(
            deposit_no=number or uniq('DEP-ISO-'), company='OIL',
            deposit_date=date.today(), collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'), bank_key='INB:2201101',
            bank_code='INB', bank_gl_account='2201101',
            bank_display_name='INDIAN BANK', source_gl_account='1105001',
            status=BankDeposit.Status.DRAFT, created_by=self.creator)


class BothConfiguredTests(_Base):
    """The intended configuration: two workflows, one module."""

    def setUp(self):
        super().setUp()
        self.receipt_wf, self.receipt_stages = payments_workflow(
            self.receipt_approver, company='OIL', code='ISO_RECEIPT',
            documents='receipts')
        self.deposit_wf, self.deposit_stages = payments_workflow(
            self.deposit_approver, company='OIL', code='ISO_DEPOSIT',
            documents='deposits')

    def test_matching_numbers_still_route_to_different_workflows(self):
        """RCP-000123 and DEP-000123 — the same serial, two workflows."""
        receipt = self._receipt('RCP-000123')
        deposit = self._deposit('DEP-000123')

        receipt_flow = workflow_flow.start(receipt, user=self.creator)
        deposit_flow = workflow_flow.start(deposit, user=self.creator)

        self.assertEqual(receipt_flow.workflow_id, self.receipt_wf.id)
        self.assertEqual(deposit_flow.workflow_id, self.deposit_wf.id)
        self.assertNotEqual(receipt_flow.workflow_id, deposit_flow.workflow_id)

    def test_the_same_internal_id_routes_to_different_workflows(self):
        """The case the `doc_no` key exists for.

        Receipt 7 and deposit 7 both exist. Keyed on `id` each would match the
        other's query and every submit would be ambiguous.
        """
        receipt = self._receipt()
        deposit = self._deposit()
        # Force the collision the id-keyed design would have produced.
        BankDeposit.objects.filter(id=deposit.pk).update(id=receipt.pk)
        deposit = BankDeposit.objects.get(id=receipt.pk)
        self.assertEqual(receipt.pk, deposit.pk)

        receipt_flow = workflow_flow.start(receipt, user=self.creator)
        deposit_flow = workflow_flow.start(deposit, user=self.creator)

        self.assertEqual(receipt_flow.workflow_id, self.receipt_wf.id)
        self.assertEqual(deposit_flow.workflow_id, self.deposit_wf.id)

    def test_each_kind_lands_with_its_own_approver(self):
        receipt_flow = workflow_flow.start(self._receipt(), user=self.creator)
        deposit_flow = workflow_flow.start(self._deposit(), user=self.creator)

        self.assertEqual(receipt_flow.current_user_id,
                         self.receipt_approver.id)
        self.assertEqual(deposit_flow.current_user_id,
                         self.deposit_approver.id)

    def test_a_receipt_approver_cannot_approve_a_deposit(self):
        """Separate permission keys, not just separate stages."""
        deposit_flow = workflow_flow.start(self._deposit(), user=self.creator)

        allowed, reason = may_act_on(self.receipt_approver, deposit_flow)
        self.assertFalse(allowed)
        self.assertIn('permission', reason)

    def test_a_deposit_approver_cannot_approve_a_receipt(self):
        receipt_flow = workflow_flow.start(self._receipt(), user=self.creator)

        allowed, reason = may_act_on(self.deposit_approver, receipt_flow)
        self.assertFalse(allowed)
        self.assertIn('permission', reason)

    def test_the_two_workflows_have_independent_stages(self):
        self.assertNotEqual(self.receipt_stages[0].id,
                            self.deposit_stages[0].id)
        self.assertEqual(self.receipt_stages[0].user_id,
                         self.receipt_approver.id)
        self.assertEqual(self.deposit_stages[0].user_id,
                         self.deposit_approver.id)

    def test_stage_counts_are_configured_independently(self):
        """Adding a receipt stage does not change the deposit workflow."""
        make_stage(self.receipt_wf, 2, self.receipt_approver)

        receipt_flow = workflow_flow.start(self._receipt(), user=self.creator)
        deposit_flow = workflow_flow.start(self._deposit(), user=self.creator)

        self.assertEqual(receipt_flow.total_stage, 2)
        self.assertEqual(deposit_flow.total_stage, 1)


class OnlyOneWorkflowConfiguredTests(_Base):
    """Neither kind falls back to the other's workflow when its own is absent."""

    def test_a_receipt_is_refused_when_only_the_deposit_workflow_exists(self):
        payments_workflow(self.deposit_approver, company='OIL',
                          code='ISO_DEP_ONLY', documents='deposits')
        receipt = self._receipt()

        with self.assertRaises(workflow_flow.PaymentFlowError):
            workflow_flow.start(receipt, user=self.creator)

        self.assertIsNone(workflow_flow.flow_of(receipt))

    def test_a_deposit_is_refused_when_only_the_receipt_workflow_exists(self):
        payments_workflow(self.receipt_approver, company='OIL',
                          code='ISO_RCP_ONLY', documents='receipts')
        deposit = self._deposit()

        with self.assertRaises(workflow_flow.PaymentFlowError):
            workflow_flow.start(deposit, user=self.creator)

        self.assertIsNone(workflow_flow.flow_of(deposit))


class QueryTargetingTests(_Base):
    """A query can only ever return the kind whose table it reads."""

    def test_a_receipt_query_cannot_return_a_deposit(self):
        workflow, _stages = payments_workflow(
            self.receipt_approver, company='OIL', code='ISO_Q_RCP',
            documents='receipts')
        query = WorkflowQuery.objects.get(workflow=workflow)
        deposit = self._deposit()

        matched = conditions.matches(query, deposit.deposit_no,
                                     key_column=workflow_flow.KEY_COLUMN)
        self.assertFalse(matched)

    def test_a_deposit_query_cannot_return_a_receipt(self):
        workflow, _stages = payments_workflow(
            self.deposit_approver, company='OIL', code='ISO_Q_DEP',
            documents='deposits')
        query = WorkflowQuery.objects.get(workflow=workflow)
        receipt = self._receipt()

        matched = conditions.matches(query, receipt.receipt_no,
                                     key_column=workflow_flow.KEY_COLUMN)
        self.assertFalse(matched)

    def test_the_kind_a_query_serves_is_read_from_its_table(self):
        serves = workflow_flow.kinds_a_query_serves
        self.assertEqual(
            serves(f'SELECT *, receipt_no AS doc_no FROM {RECEIPT_TABLE}'),
            {workflow_flow.DocumentKind.RECEIPT})
        self.assertEqual(
            serves(f'SELECT *, deposit_no AS doc_no FROM {DEPOSIT_TABLE}'),
            {workflow_flow.DocumentKind.DEPOSIT})
        self.assertEqual(serves('SELECT 1'), set())


class MisconfiguredRoutingTests(_Base):
    """The guard, exercised against configurations that break the convention."""

    def test_a_deposit_numbered_like_a_receipt_cannot_capture_it(self):
        """THE CASE THE GUARD EXISTS FOR.

        Document numbers are the selection key, so a deposit numbered `RCP-…`
        makes the deposit workflow's query match a RECEIPT's number. Without
        the guard that receipt would be routed into the deposit workflow, land
        with deposit approvers and post down the deposit path.
        """
        number = 'RCP-TRAP-000001'
        self._deposit(number)                      # deposit numbered as RCP-
        receipt = self._receipt(number.replace('RCP-TRAP', 'RCP-TRAPX'))
        # Give the receipt the SAME number the rogue deposit carries.
        PaymentReceipt.objects.filter(pk=receipt.pk).update(receipt_no=number)
        receipt.refresh_from_db()

        # Only the DEPOSIT workflow is configured, so the only query that can
        # match this number is the deposit one.
        payments_workflow(self.deposit_approver, company='OIL',
                          code='ISO_TRAP_DEP', documents='deposits')

        with self.assertRaises(workflow_flow.PaymentFlowError) as caught:
            workflow_flow.start(receipt, user=self.creator)

        message = str(caught.exception).lower()
        self.assertIn('deposit', message)
        self.assertIsNone(workflow_flow.flow_of(receipt))

    def test_a_query_reading_both_tables_is_refused(self):
        """Ambiguous by construction: it could return either kind."""
        # Stand down every other active payments workflow first. Without
        # this the live configuration in the shared TEST database also matches
        # and selection fails as AMBIGUOUS — which would make this test pass
        # without ever reaching the guard it is about.
        from workflow.models import Workflow

        from .apps import MODULE_CODE
        from .tests_workflow_fixtures import FIXTURE_MARK

        Workflow.objects.filter(module__code=MODULE_CODE,
                                is_active=True).update(is_active=False)

        workflow = make_workflow(code='ISO_BOTH', company='OIL')
        make_stage(workflow, 1, self.receipt_approver)
        query = WorkflowQuery.objects.create(
            workflow=workflow, name='both tables', company=COMPANY_ALL,
            query_text=(
                f'SELECT *, receipt_no AS doc_no FROM {RECEIPT_TABLE} '
                f'WHERE EXISTS (SELECT 1 FROM {DEPOSIT_TABLE})'))
        problems = conditions.validate_and_stamp(query)
        self.assertFalse(problems, problems)

        receipt = self._receipt()
        self._deposit()          # so the EXISTS is satisfied

        with self.assertRaises(workflow_flow.PaymentFlowError) as caught:
            workflow_flow.start(receipt, user=self.creator)

        self.assertIn('both', str(caught.exception).lower())
        self.assertIsNone(workflow_flow.flow_of(receipt))

    def test_the_guard_leaves_a_correct_configuration_alone(self):
        """The guard must not become a second source of refusals."""
        payments_workflow(self.receipt_approver, company='OIL',
                          code='ISO_FINE', documents='receipts')
        receipt = self._receipt()

        flow = workflow_flow.start(receipt, user=self.creator)

        self.assertIsNotNone(flow)
        self.assertEqual(flow.current_user_id, self.receipt_approver.id)


class MigrationRepairLogicTests(TestCase):
    """The `doc_no` repair in payments/0037, as pure functions.

    Asserted directly rather than by running the migration: the rule it encodes
    — which table means which key column — is the same one routing enforces,
    and the two must not drift.
    """

    def _module(self):
        import importlib

        return importlib.import_module(
            'payments.migrations.0037_repair_payments_doc_no_alias')

    def test_the_tables_match_the_routing_definition(self):
        module = self._module()
        self.assertEqual(module.RECEIPT[0], RECEIPT_TABLE)
        self.assertEqual(module.DEPOSIT[0], DEPOSIT_TABLE)
        self.assertEqual(
            module.RECEIPT[0],
            workflow_flow.source_table_for(workflow_flow.DocumentKind.RECEIPT))
        self.assertEqual(
            module.DEPOSIT[0],
            workflow_flow.source_table_for(workflow_flow.DocumentKind.DEPOSIT))

    def test_it_repairs_a_receipt_query(self):
        module = self._module()
        text = f"SELECT * FROM {RECEIPT_TABLE} WHERE company = 'OIL'"
        self.assertTrue(module._needs_repair(text))
        self.assertEqual(module._target(text), module.RECEIPT)

    def test_it_leaves_a_repaired_query_alone(self):
        module = self._module()
        text = (f'SELECT *, receipt_no AS doc_no FROM {RECEIPT_TABLE}')
        self.assertFalse(module._needs_repair(text))

    def test_it_refuses_to_guess_at_a_query_naming_both_tables(self):
        module = self._module()
        text = (f'SELECT * FROM {RECEIPT_TABLE} '
                f'WHERE EXISTS (SELECT 1 FROM {DEPOSIT_TABLE})')
        self.assertIsNone(module._target(text))

    def test_it_ignores_a_query_naming_neither_table(self):
        module = self._module()
        self.assertIsNone(module._target('SELECT * FROM something_else'))
