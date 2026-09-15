"""The module-registration contract — `docs/architecture/WORKFLOW_MODULE_INTEGRATION.md`.

Proves that registering a module is enough to configure a workflow and have
the generic engine SELECT it, with no module-specific engine code. The engine
stops at configuration: what a module does with the result is its own.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.companies import OIL
from workflow.exceptions import WorkflowNotConfigured
from workflow.models import (
    Workflow, WorkflowModule, WorkflowQuery, WorkflowStage,
)
from workflow.registry import (
    is_registered, register_module, registration_for,
)
from workflow.tests.factories import (
    make_document, make_query, make_stage, make_user, make_workflow,
)
from workflow.services import selection

#: The WHOLE registration. `code` + `name`, and nothing else is accepted.
REG = dict(code='REGTEST', name='Registry Contract Harness')


class RegistrationTests(TestCase):
    def test_register_creates_exactly_one_row(self):
        module, created = register_module(**REG)
        self.assertTrue(created)
        self.assertEqual(WorkflowModule.objects.filter(code='REGTEST').count(), 1)
        self.assertEqual(module.code, 'REGTEST')
        self.assertEqual(module.name, 'Registry Contract Harness')

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
    def test_blank_code_is_refused(self):
        with self.assertRaises(ValidationError):
            register_module(**{**REG, 'code': '  '})

    def test_blank_name_is_refused(self):
        with self.assertRaises(ValidationError):
            register_module(**{**REG, 'name': '   '})

    def test_technical_metadata_is_not_accepted(self):
        """The old arguments must FAIL, not be quietly ignored.

        A deploy script still passing `business_table=` has to break loudly:
        silently dropping it would leave the author believing the engine still
        holds a value it no longer has.
        """
        for dead in ('business_table', 'business_key_column',
                     'flow_table', 'flow_model'):
            with self.subTest(argument=dead):
                with self.assertRaises(TypeError):
                    register_module(**{**REG, dead: 'anything'})


class RegistryHoldsIdentityOnlyTests(TestCase):
    """`workflow_modules` is code + name. Enforced at the model AND in SQL."""

    EXPECTED = ['id', 'created_at', 'updated_at', 'code', 'name']
    REMOVED = ['business_table', 'business_key_column', 'flow_table',
               'flow_model', 'is_active']

    def test_model_exposes_only_identity_fields(self):
        names = {f.name for f in WorkflowModule._meta.get_fields()
                 if getattr(f, 'concrete', False)}
        self.assertEqual(names, set(self.EXPECTED))

    def test_removed_fields_are_gone_from_the_model(self):
        concrete = {f.name for f in WorkflowModule._meta.get_fields()}
        for dead in self.REMOVED:
            with self.subTest(field=dead):
                self.assertNotIn(dead, concrete)
                self.assertFalse(hasattr(WorkflowModule(), dead))

    def test_postgres_schema_matches_the_model(self):
        """The real table, not just Django's idea of it.

        A RemoveField that silently no-ops (a faked migration, a hand-edited
        history) leaves the model clean and the column still there, so this
        asks the database directly.
        """
        from django.db import connection
        if connection.vendor != 'postgresql':
            self.skipTest('schema assertion is PostgreSQL-specific')
        with connection.cursor() as cur:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'workflow'
                  AND table_name = 'workflow_modules'
                ORDER BY ordinal_position
            """)
            columns = [row[0] for row in cur.fetchall()]
        self.assertEqual(sorted(columns), sorted(self.EXPECTED))
        for dead in self.REMOVED:
            self.assertNotIn(dead, columns)


class RegisteredModuleDrivesSelectionTests(TestCase):
    """registration -> workflow -> query -> stage -> selection result.

    The whole point of the registry: a module that has registered can be
    driven by the generic engine with no module-specific engine code. It stops
    at the CONFIGURATION the engine returns — the module builds its own task
    and history from that, which is why nothing here asserts on an engine task.
    """

    @classmethod
    def setUpTestData(cls):
        cls.module, _ = register_module(code='REGFLOW', name='Registry Flow')
        cls.u1 = make_user('reg-stage1')
        cls.u2 = make_user('reg-stage2')
        cls.workflow = make_workflow(cls.module, code='WF_REG', company=None)
        make_query(cls.workflow, name='all', company=None)
        make_stage(cls.workflow, 1, cls.u1)
        make_stage(cls.workflow, 2, cls.u2)

    def test_select_for_module_returns_the_configuration(self):
        doc = make_document()

        result = selection.select_for_module(
            module_code='REGFLOW', document_id=doc.pk, company=OIL)

        self.assertEqual(result.workflow.pk, self.workflow.pk)
        self.assertIsNotNone(result.matched_query)
        self.assertEqual([s.sequence for s in result.stages], [1, 2])
        self.assertEqual([s.user_id for s in result.stages],
                         [self.u1.pk, self.u2.pk])

    def test_result_serialises_to_the_documented_contract(self):
        doc = make_document()
        payload = selection.select_for_module(
            module_code='REGFLOW', document_id=doc.pk, company=OIL).as_dict()

        self.assertEqual(
            set(payload), {'workflow', 'matched_query', 'stages'})
        self.assertEqual(
            set(payload['workflow']),
            {'id', 'code', 'name', 'company', 'module_code'})
        self.assertEqual(
            set(payload['stages'][0]),
            {'id', 'sequence', 'name', 'user_id', 'effective_user_id'})

    def test_unregistered_module_is_reported(self):
        with self.assertRaises(WorkflowNotConfigured):
            selection.select_for_module(
                module_code='NOPE', document_id=1, company=OIL)

    def test_engine_creates_no_runtime_rows(self):
        """Selection is a read. It must not write anything anywhere."""
        doc = make_document()
        before = {
            'workflows': Workflow.objects.count(),
            'queries': WorkflowQuery.objects.count(),
            'stages': WorkflowStage.objects.count(),
        }
        selection.select_for_module(
            module_code='REGFLOW', document_id=doc.pk, company=OIL)
        self.assertEqual(before, {
            'workflows': Workflow.objects.count(),
            'queries': WorkflowQuery.objects.count(),
            'stages': WorkflowStage.objects.count(),
        })
