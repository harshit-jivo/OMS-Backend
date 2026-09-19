"""`audit_approval_config` — is the engine configured to route payments?

The command answers whether payments can actually be submitted: are both
workflows configured, does each query project `doc_no` and read only its own
table, and is any document mid-approval with no flow. A wrong PASS here would
let a release ship that cannot route a single receipt.

It used to audit the OLD `approvals` engine as well — whether its levels could
be expressed as one-user-per-stage, and what was still in flight. That engine
and its tables were removed, so those suites went with them; what remains is
everything that still has a subject.

The environment guard is tested without a database, so it runs anywhere. The
rest builds real workflow configuration and therefore needs PostgreSQL, like
every other database-backed test in this project. SQLite is not substituted.
"""
from datetime import date
from decimal import Decimal
import re
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from .tests_support import uniq
from payments.models import (BankDeposit, PaymentReceipt,
                            PaymentStatusHistory)
from users.models import User, UserRole


def _run(**options):
    """Run the command, returning (output, blocked)."""
    out = StringIO()
    blocked = False
    try:
        call_command('audit_approval_config', stdout=out, stderr=out, **options)
    except SystemExit as exit_code:
        blocked = int(getattr(exit_code, 'code', 1) or 0) == 1
    return out.getvalue(), blocked


class EnvironmentGuardTests(SimpleTestCase):
    """It must be impossible to point this at production by accident."""

    @override_settings(SENTRY_ENVIRONMENT='production')
    def test_production_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            call_command('audit_approval_config', stdout=StringIO())
        self.assertIn('TEST environments only', str(caught.exception))

    @override_settings(SENTRY_ENVIRONMENT='production')
    def test_production_cannot_be_unlocked_by_the_flag(self):
        """--allow-environment is for unrecognised labels, never production."""
        with self.assertRaises(CommandError):
            call_command('audit_approval_config',
                         allow_environment='production', stdout=StringIO())

    @override_settings(SENTRY_ENVIRONMENT='')
    def test_an_unlabelled_environment_fails_closed(self):
        with self.assertRaises(CommandError):
            call_command('audit_approval_config', stdout=StringIO())

    @override_settings(SENTRY_ENVIRONMENT='prod-eu')
    def test_an_unrecognised_label_needs_naming_and_must_match(self):
        with self.assertRaises(CommandError):
            call_command('audit_approval_config',
                         allow_environment='something-else', stdout=StringIO())


@override_settings(SENTRY_ENVIRONMENT='test')
class _Base(TestCase):
    """A valid two-workflow configuration, so every test starts from a PASS.

    Receipts and deposits are separate business workflows under one module.
    Each test then breaks exactly one thing, which is what makes the failure
    it asserts attributable.
    """

    @classmethod
    def setUpTestData(cls):
        # Stand down whatever the shared TEST database happens to hold, inside
        # the class-level transaction that is rolled back afterwards. Without
        # this every test would also be asserting someone else's live
        # configuration.
        from workflow.models import Workflow

        from payments.apps import MODULE_CODE
        from payments.tests_workflow_fixtures import payments_workflow

        Workflow.objects.filter(module__code=MODULE_CODE,
                                is_active=True).update(is_active=False)

        role, _ = UserRole.objects.get_or_create(name='payments_approver')
        cls.approver = User.objects.create(username=uniq('auditor_a-'),
                                           name='A', role=role)
        cls.other = User.objects.create(username=uniq('auditor_b-'), name='B',
                                        role=role)
        cls.role = role

        cls.receipt_wf, _ = payments_workflow(
            cls.approver, company='OIL', code='AUDIT_ENGINE_PAY',
            documents='receipts')
        cls.deposit_wf, _ = payments_workflow(
            cls.approver, company='OIL', code='AUDIT_ENGINE_DEP',
            documents='deposits')

    def _receipt(self, receipt_no=None, company='OIL'):
        return PaymentReceipt.objects.create(
            receipt_no=receipt_no or uniq('RCP-OIL-AUDIT-'), company=company,
            card_code='C1', payment_date=date.today(),
            total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1)

    def _deposit(self, deposit_no=None, company='OIL'):
        return BankDeposit.objects.create(
            deposit_no=deposit_no or uniq('DEP-OIL-AUDIT-'), company=company,
            deposit_date=date.today(),
            collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'))


class DocumentNumberTests(_Base):
    """Receipt and deposit numbers become the single `doc_no` selection key."""

    def test_clean_numbers_are_usable(self):
        self._receipt()
        self._deposit()

        out, blocked = _run()
        self.assertFalse(blocked, out)
        self.assertIn('receipt/deposit collisions   : 0', out)
        self.assertIn('doc_no is usable', out)

    def test_a_collision_between_the_two_types_blocks(self):
        """The whole reason payments keys on the number and not on id."""
        self._receipt(receipt_no='SHARED-0001')
        self._deposit(deposit_no='SHARED-0001')

        out, blocked = _run()
        self.assertTrue(blocked, out)
        self.assertIn('receipt/deposit collisions   : 1', out)
        self.assertIn('SHARED-0001', out)

    def test_an_unexpected_prefix_blocks(self):
        self._receipt(receipt_no='XXX-OIL-20260918-000001')

        out, blocked = _run()
        self.assertTrue(blocked, out)
        self.assertIn('XXX-OIL-20260918-000001', out)


class ReadOnlyTests(_Base):
    """The audit reports; it never changes anything."""

    def test_nothing_is_written(self):
        from workflow.models import Workflow, WorkflowQuery, WorkflowStage

        from . import workflow_flow

        receipt = self._receipt()
        flow = workflow_flow.start(receipt, user=self.approver)

        def snapshot():
            flow.refresh_from_db()
            return {
                'workflows': Workflow.objects.count(),
                'queries': WorkflowQuery.objects.count(),
                'stages': WorkflowStage.objects.count(),
                'receipts': PaymentReceipt.objects.count(),
                'deposits': BankDeposit.objects.count(),
                'history': PaymentStatusHistory.objects.count(),
                'flow_status': flow.status,
                'flow_stage': flow.current_stage_id,
            }

        before = snapshot()
        _run()
        self.assertEqual(before, snapshot())

    def test_the_environment_is_stated_without_secrets(self):
        out, _ = _run()
        self.assertIn('Environment :', out)
        self.assertIn('Database    :', out)
        self.assertNotIn(
            str(__import__('django.conf', fromlist=['settings'])
                .settings.DATABASES['default'].get('PASSWORD') or 'PASSWORD'),
            out)
