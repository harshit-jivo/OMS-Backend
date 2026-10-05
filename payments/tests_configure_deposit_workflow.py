"""Creating the DEPOSIT workflow, and everything it refuses to do.

The command does the work the Workflows page does, once, from the command
line. Most of this suite is about its REFUSALS, because each one is a way the
deposit chain could otherwise be configured into something that looks right and
silently does not work:

  * a stage user who cannot approve (no `Deposit_Approve` key);
  * a second deposit workflow, which makes every deposit submission ambiguous;
  * an approver chosen by the command rather than named by a person.

PostgreSQL only: the query it writes is validated and stamped for real.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from users.models import UserRole
from workflow.models import Workflow, WorkflowQuery, WorkflowStage

from . import workflow_flow
from .apps import MODULE_CODE
from .permissions import DEPOSIT_APPROVE
from .tests_support import uniq
from .tests_workflow_fixtures import make_module, payments_workflow


def _user(prefix, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return get_user_model().objects.create(
        username=uniq(prefix), name='Approver', role=role,
        extra_pages=list(keys))


class ConfigureDepositWorkflowTests(TestCase):
    def setUp(self):
        super().setUp()
        make_module()
        self.approver = _user('cdw_appr-', [DEPOSIT_APPROVE])
        self.second = _user('cdw_appr2-', [DEPOSIT_APPROVE])
        self.keyless = _user('cdw_nokey-')
        # Stand down the live configuration so these tests describe the
        # database they build, not whatever TEST happens to hold today.
        Workflow.objects.filter(module__code=MODULE_CODE,
                                is_active=True).update(is_active=False)

    def _run(self, **kwargs):
        out = StringIO()
        call_command('configure_deposit_workflow', stdout=out, **kwargs)
        return out.getvalue()

    # -- what it creates ---------------------------------------------------

    def test_it_creates_a_workflow_a_query_and_a_stage(self):
        self._run(approver=[self.approver.username], code='CDW_ONE')

        workflow = Workflow.objects.get(code='CDW_ONE')
        self.assertEqual(workflow.module.code, MODULE_CODE)
        self.assertTrue(workflow.is_active)

        query = WorkflowQuery.objects.get(workflow=workflow)
        self.assertIn('deposit_no AS doc_no', query.query_text)
        # Stamped, or selection would refuse to run it.
        self.assertIsNotNone(query.validated_at)

        stage = WorkflowStage.objects.get(workflow=workflow)
        self.assertEqual(stage.sequence, 1)
        self.assertEqual(stage.user_id, self.approver.id)

    def test_the_query_serves_deposits_and_only_deposits(self):
        self._run(approver=[self.approver.username], code='CDW_KIND')

        query = WorkflowQuery.objects.get(workflow__code='CDW_KIND')
        self.assertEqual(
            workflow_flow.kinds_a_query_serves(query.query_text),
            {workflow_flow.DocumentKind.DEPOSIT})

    def test_several_approvers_become_stages_in_order(self):
        self._run(approver=[self.approver.username, self.second.username],
                  code='CDW_TWO')

        stages = list(WorkflowStage.objects
                      .filter(workflow__code='CDW_TWO').order_by('sequence'))
        self.assertEqual([s.sequence for s in stages], [1, 2])
        self.assertEqual([s.user_id for s in stages],
                         [self.approver.id, self.second.id])

    def test_a_dry_run_writes_nothing(self):
        output = self._run(approver=[self.approver.username],
                           code='CDW_DRY', dry_run=True)

        self.assertIn('WOULD CREATE', output)
        self.assertFalse(Workflow.objects.filter(code='CDW_DRY').exists())

    # -- what it refuses ---------------------------------------------------

    def test_it_refuses_an_approver_without_the_deposit_key(self):
        """Configuration that looks right and could never approve anything."""
        with self.assertRaises(CommandError) as caught:
            self._run(approver=[self.keyless.username], code='CDW_NOKEY')

        self.assertIn(DEPOSIT_APPROVE, str(caught.exception))
        self.assertFalse(Workflow.objects.filter(code='CDW_NOKEY').exists())

    def test_it_refuses_to_choose_an_approver_itself(self):
        with self.assertRaises(CommandError) as caught:
            self._run(code='CDW_NONE')

        self.assertIn('business decision', str(caught.exception))

    def test_it_refuses_a_second_deposit_workflow(self):
        """Two active deposit workflows would make every submission ambiguous."""
        payments_workflow(self.approver, code='CDW_EXISTING',
                          documents='deposits')

        with self.assertRaises(CommandError) as caught:
            self._run(approver=[self.approver.username], code='CDW_RIVAL')

        self.assertIn('already routes deposits', str(caught.exception))
        self.assertFalse(Workflow.objects.filter(code='CDW_RIVAL').exists())

    def test_an_existing_receipt_workflow_does_not_block_it(self):
        """The receipt side is a different business workflow, not a rival."""
        payments_workflow(self.approver, code='CDW_RECEIPTS',
                          documents='receipts')

        self._run(approver=[self.approver.username], code='CDW_BESIDE')

        self.assertTrue(Workflow.objects.filter(code='CDW_BESIDE').exists())

    def test_it_refuses_the_same_code_twice(self):
        self._run(approver=[self.approver.username], code='CDW_DUP')

        with self.assertRaises(CommandError) as caught:
            self._run(approver=[self.approver.username], code='CDW_DUP')

        self.assertIn('already exists', str(caught.exception))
        self.assertEqual(Workflow.objects.filter(code='CDW_DUP').count(), 1)

    def test_it_refuses_an_unknown_company(self):
        with self.assertRaises(CommandError) as caught:
            self._run(approver=[self.approver.username], code='CDW_CO',
                      company='NOWHERE')

        self.assertIn('Unknown company', str(caught.exception))

    def test_it_refuses_an_unknown_user(self):
        with self.assertRaises(CommandError) as caught:
            self._run(approver=['no_such_person'], code='CDW_GHOST')

        self.assertIn('No user named', str(caught.exception))

    def test_every_approver_is_checked_before_anything_is_written(self):
        """One bad approver in the list stops the whole command.

        The check runs over the FULL list up front, so a second stage user who
        cannot approve prevents the workflow being created at all rather than
        leaving a chain that stalls at stage 2.
        """
        with self.assertRaises(CommandError):
            self._run(approver=[self.approver.username,
                                self.keyless.username],
                      code='CDW_PARTIAL')

        self.assertFalse(Workflow.objects.filter(code='CDW_PARTIAL').exists())
        self.assertFalse(
            WorkflowQuery.objects.filter(workflow__code='CDW_PARTIAL').exists())

    def test_a_query_that_fails_validation_rolls_the_workflow_back(self):
        """The one path that writes before it can fail.

        The workflow row is created, then its query is validated and stamped.
        If validation refuses, the workflow must go with it — a workflow whose
        query never stamped would route nothing and say nothing.
        """
        from unittest.mock import patch

        with patch('payments.management.commands.configure_deposit_workflow'
                   '.conditions.validate_and_stamp',
                   return_value=['relation does not exist']):
            with self.assertRaises(CommandError) as caught:
                self._run(approver=[self.approver.username],
                          code='CDW_BADQUERY')

        self.assertIn('failed validation', str(caught.exception))
        self.assertFalse(Workflow.objects.filter(code='CDW_BADQUERY').exists())
        self.assertFalse(
            WorkflowQuery.objects.filter(
                workflow__code='CDW_BADQUERY').exists())


class RoutingThroughTheCreatedWorkflowTests(TestCase):
    """The configuration it writes actually routes a deposit."""

    def setUp(self):
        super().setUp()
        from datetime import date
        from decimal import Decimal

        from .models import BankDeposit

        make_module()
        Workflow.objects.filter(module__code=MODULE_CODE,
                                is_active=True).update(is_active=False)
        self.approver = _user('cdwr_appr-', [DEPOSIT_APPROVE])
        self.creator = _user('cdwr_creator-')
        call_command('configure_deposit_workflow',
                     approver=[self.approver.username], code='CDW_LIVE',
                     stdout=StringIO())
        self.deposit = BankDeposit.objects.create(
            deposit_no=uniq('DEP-CDW-'), company='OIL',
            deposit_date=date.today(), collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'), bank_key='INB:2201101',
            bank_code='INB', bank_gl_account='2201101',
            bank_display_name='INDIAN BANK', source_gl_account='1105001',
            status=BankDeposit.Status.DRAFT, created_by=self.creator)

    def test_a_deposit_routes_through_it_to_its_approver(self):
        flow = workflow_flow.start(self.deposit, user=self.creator)

        self.assertEqual(flow.workflow.code, 'CDW_LIVE')
        self.assertEqual(flow.current_user_id, self.approver.id)
        self.assertEqual(flow.total_stage, 1)
        self.assertTrue(workflow_flow.is_at_final_stage(flow))

    def test_a_receipt_is_not_captured_by_it(self):
        """The created workflow is a DEPOSIT workflow and nothing else."""
        from datetime import date
        from decimal import Decimal

        from .models import PaymentMethodEntry, PaymentReceipt

        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-CDW-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))

        with self.assertRaises(workflow_flow.PaymentFlowError):
            workflow_flow.start(receipt, user=self.creator)

        self.assertIsNone(workflow_flow.flow_of(receipt))
