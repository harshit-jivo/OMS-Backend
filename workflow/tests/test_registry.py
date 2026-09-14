"""The module-registration contract — `docs/architecture/WORKFLOW_MODULE_INTEGRATION.md`.

Proves that registering a module is enough to configure and run a workflow
through the generic engine, with no module-specific engine code. Reuses the
existing TestFlow harness rather than adding a second one.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.companies import OIL
from workflow.models import (
    FlowStatus, TaskStatus, TestFlow, WorkflowModule,
)
from workflow.registry import (
    is_registered, register_module, registration_for,
)
from workflow.services import engine
from workflow.tests.factories import (
    TESTDOC_TABLE, TESTFLOW_TABLE, make_document, make_query, make_stage,
    make_user, make_workflow,
)

REG = dict(
    code='REGTEST',
    name='Registry Contract Harness',
    business_table=TESTDOC_TABLE,
    business_key_column='id',
    flow_table=TESTFLOW_TABLE,
    flow_model='workflow.TestFlow',
)


class RegistrationTests(TestCase):
    def test_register_creates_exactly_one_row(self):
        module, created = register_module(**REG)
        self.assertTrue(created)
        self.assertEqual(WorkflowModule.objects.filter(code='REGTEST').count(), 1)
        self.assertEqual(module.business_table, TESTDOC_TABLE)
        self.assertEqual(module.flow_model, 'workflow.TestFlow')

    def test_registration_is_idempotent(self):
        register_module(**REG)
        module, created = register_module(**REG)
        self.assertFalse(created)
        self.assertEqual(WorkflowModule.objects.filter(code='REGTEST').count(), 1)

    def test_re_registration_updates_in_place(self):
        first, _ = register_module(**REG)
        second, created = register_module(**{**REG, 'name': 'Renamed'})
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.name, 'Renamed')
        self.assertEqual(WorkflowModule.objects.filter(code='REGTEST').count(), 1)

    def test_code_is_uppercased(self):
        module, _ = register_module(**{**REG, 'code': 'regtest'})
        self.assertEqual(module.code, 'REGTEST')

    def test_helpers(self):
        self.assertFalse(is_registered('REGTEST'))
        register_module(**REG)
        self.assertTrue(is_registered('regtest'))
        self.assertEqual(registration_for('REGTEST').code, 'REGTEST')

    # --- validation guards -------------------------------------------
    def test_db_table_quoting_form_is_refused(self):
        """`schema"."table` is Django's quoting trick, not raw SQL."""
        with self.assertRaises(ValidationError) as caught:
            register_module(**{**REG, 'business_table': 'workflow"."x'})
        self.assertIn('raw SQL', str(caught.exception))

    def test_unresolvable_flow_model_is_refused(self):
        with self.assertRaises(ValidationError):
            register_module(**{**REG, 'flow_model': 'workflow.NoSuchModel'})

    def test_flow_model_must_subclass_the_base(self):
        with self.assertRaises(ValidationError) as caught:
            register_module(**{**REG, 'flow_model': 'workflow.TestDocument'})
        self.assertIn('WorkflowFlowBase', str(caught.exception))

    def test_malformed_flow_model_is_refused(self):
        with self.assertRaises(ValidationError):
            register_module(**{**REG, 'flow_model': 'TestFlow'})

    def test_blank_code_is_refused(self):
        with self.assertRaises(ValidationError):
            register_module(**{**REG, 'code': '  '})


class RegisteredModuleDrivesTheEngineTests(TestCase):
    """registration -> workflow -> query -> stage -> submit -> task -> approve.

    The whole point of the registry: a module that has registered can be
    driven by the generic engine with no module-specific engine code.
    """

    @classmethod
    def setUpTestData(cls):
        cls.module, _ = register_module(**REG)
        cls.u1 = make_user('reg-stage1')
        cls.u2 = make_user('reg-stage2')
        cls.workflow = make_workflow(cls.module, code='WF_REG', company=None)
        make_query(cls.workflow, name='all', company=None)
        make_stage(cls.workflow, 1, cls.u1)
        make_stage(cls.workflow, 2, cls.u2)

    def test_end_to_end_through_the_generic_engine(self):
        doc = make_document(company=OIL)
        flow = TestFlow.objects.create(document=doc, company=OIL)

        flow, task = engine.start(module=self.module, flow=flow,
                                  document_key=doc.pk, company=OIL,
                                  user=self.u1)
        self.assertEqual(flow.workflow_id, self.workflow.pk)
        self.assertIsNotNone(flow.matched_query_id)
        self.assertEqual(task.stage_user_id, self.u1.pk)
        self.assertEqual(task.status, TaskStatus.PENDING)

        self.assertEqual(engine.inbox_for(self.u1).count(), 1)
        self.assertEqual(engine.inbox_for(self.u2).count(), 0)

        flow, next_task = engine.approve(task_id=task.pk, user=self.u1)
        self.assertEqual(flow.current_sequence, 2)
        self.assertEqual(next_task.stage_user_id, self.u2.pk)

        flow, done = engine.approve(task_id=next_task.pk, user=self.u2)
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertIsNone(done)

    def test_flow_model_resolves_from_the_registry_row(self):
        """The engine finds the concrete model without importing the module."""
        self.assertIs(engine.flow_model_for(self.module), TestFlow)

    def test_module_owns_no_workflow_engine_tables(self):
        """A registered module must not fork the engine's own tables."""
        from django.db import connection
        forbidden = ('budget_workflow', 'budget_workflow_stage',
                     'budget_workflow_query', 'budget_approver')
        existing = set(connection.introspection.table_names())
        for name in forbidden:
            self.assertNotIn(name, existing)
