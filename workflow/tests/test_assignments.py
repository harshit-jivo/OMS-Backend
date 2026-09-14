"""User assignment management — the four operations, and what must NOT happen.

The rule these tests exist to hold down: THE STAGE IS THE STABLE STEP.
`workflow_stages.user_id` is the current responsibility for that step, not a
value copied out to each entry passing through it. So changing it is one UPDATE
of one column, and there is no entry to migrate, no workflow to duplicate and
no history to rewrite.

A business module's runtime is simulated here by holding a `stage_id` — which
is exactly the integration contract modules are given. Nothing in this file
creates a task table, because the engine does not have one.
"""
import datetime

from django.test import TestCase

from core.companies import MART, OIL
from workflow.models import (
    Workflow, WorkflowModule, WorkflowQuery, WorkflowStage,
    WorkflowUserReplacement,
)
from workflow.services import assignments, replacements
from workflow.tests.factories import (
    make_module, make_query, make_stage, make_user, make_workflow,
)


class ChangeOneStageUserTests(TestCase):
    """Operation 1 — change a single stage's configured user."""

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.mukesh = make_user('mukesh')
        cls.ravi = make_user('ravi')
        cls.workflow = make_workflow(cls.module, code='WF_A')
        make_query(cls.workflow)
        cls.stage1 = make_stage(cls.workflow, 1, cls.mukesh, name='Manager')
        cls.stage2 = make_stage(cls.workflow, 2, cls.mukesh, name='Finance')

    def test_only_that_stage_changes(self):
        self.stage2.user = self.ravi
        self.stage2.save(update_fields=['user'])

        self.stage1.refresh_from_db()
        self.stage2.refresh_from_db()
        self.assertEqual(self.stage2.user_id, self.ravi.pk)
        # The neighbouring stage is untouched.
        self.assertEqual(self.stage1.user_id, self.mukesh.pk)

    def test_nothing_is_duplicated(self):
        """The critical assertion: one stage edit creates no second anything."""
        before = (Workflow.objects.count(), WorkflowStage.objects.count(),
                  WorkflowQuery.objects.count(), WorkflowModule.objects.count())

        self.stage2.user = self.ravi
        self.stage2.save(update_fields=['user'])

        after = (Workflow.objects.count(), WorkflowStage.objects.count(),
                 WorkflowQuery.objects.count(), WorkflowModule.objects.count())
        self.assertEqual(before, after)

    def test_the_stage_keeps_its_identity(self):
        """Same row, same sequence, same workflow — only the user differs.

        This is what makes entry migration unnecessary: a module's task points
        at `stage_id`, and that id does not change.
        """
        stage_id, sequence, workflow_id = (
            self.stage2.pk, self.stage2.sequence, self.stage2.workflow_id)

        self.stage2.user = self.ravi
        self.stage2.save(update_fields=['user'])

        self.stage2.refresh_from_db()
        self.assertEqual(self.stage2.pk, stage_id)
        self.assertEqual(self.stage2.sequence, sequence)
        self.assertEqual(self.stage2.workflow_id, workflow_id)


class ExistingEntriesFollowTheStageTests(TestCase):
    """Entries waiting at a stage need no reassignment. §38 of the brief.

    A module's task row is simulated by the `stage_id` it would hold — that is
    the whole of the integration contract. Responsibility is resolved through
    `get_stage_assignment` every time it is asked, so it follows configuration
    without anything being written to the module's rows.
    """

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.mukesh = make_user('mukesh')
        cls.ravi = make_user('ravi')
        cls.workflow = make_workflow(cls.module, code='WF_A')
        cls.stage = make_stage(cls.workflow, 2, cls.mukesh, name='Finance')

    def test_three_waiting_entries_all_follow_without_being_touched(self):
        # Three module-owned entries, each remembering only which stage it is
        # parked at — no copied user anywhere.
        entries = [
            {'ref': 'BUD-1001', 'stage_id': self.stage.pk},
            {'ref': 'BUD-1002', 'stage_id': self.stage.pk},
            {'ref': 'BUD-1003', 'stage_id': self.stage.pk},
        ]
        before = [assignments.get_stage_assignment(e['stage_id'])
                  .effective_user_id for e in entries]
        self.assertEqual(before, [self.mukesh.pk] * 3)

        self.stage.user = self.ravi
        self.stage.save(update_fields=['user'])

        after = [assignments.get_stage_assignment(e['stage_id'])
                 .effective_user_id for e in entries]
        self.assertEqual(after, [self.ravi.pk] * 3)
        # The entries themselves were never written to.
        self.assertTrue(all(e['stage_id'] == self.stage.pk for e in entries))

    def test_a_module_resolving_a_missing_stage_gets_none(self):
        """Deleted configuration is distinguishable from "nobody assigned"."""
        self.assertIsNone(assignments.get_stage_assignment(99_999_999))


class AssignmentsForUserTests(TestCase):
    """Operation 4 — view every stage assigned to a user."""

    @classmethod
    def setUpTestData(cls):
        cls.mukesh = make_user('mukesh')
        cls.ravi = make_user('ravi')
        cls.budget = make_module(code='BUDGET', name='Budget Approval')
        cls.orders = make_module(code='ORDER', name='Order Approval')

        cls.wf_a = make_workflow(cls.budget, code='BUDGET_STANDARD')
        cls.wf_b = make_workflow(cls.budget, code='BUDGET_OIL', company=OIL)
        cls.wf_c = make_workflow(cls.orders, code='ORDER_STANDARD',
                                 company=MART)
        cls.s_a = make_stage(cls.wf_a, 1, cls.mukesh, name='Manager')
        cls.s_b = make_stage(cls.wf_b, 2, cls.mukesh, name='Finance')
        cls.s_c = make_stage(cls.wf_c, 3, cls.mukesh, name='Director')

    def test_lists_exactly_that_users_stages(self):
        rows = assignments.assignments_for_user(self.mukesh.pk)
        self.assertEqual({r.stage_id for r in rows},
                         {self.s_a.pk, self.s_b.pk, self.s_c.pk})
        self.assertEqual(assignments.assignments_for_user(self.ravi.pk), [])

    def test_carries_the_module_workflow_and_company_context(self):
        row = next(r for r in assignments.assignments_for_user(self.mukesh.pk)
                   if r.stage_id == self.s_c.pk)
        self.assertEqual(row.module_code, 'ORDER')
        self.assertEqual(row.workflow_code, 'ORDER_STANDARD')
        self.assertEqual(row.stage_name, 'Director')
        self.assertEqual(row.company, MART)

    def test_moving_one_stage_moves_exactly_one_assignment(self):
        """§36: Mukesh loses one, Ravi gains one, nothing is duplicated."""
        workflows_before = Workflow.objects.count()

        self.s_b.user = self.ravi
        self.s_b.save(update_fields=['user'])

        self.assertEqual(len(assignments.assignments_for_user(self.mukesh.pk)), 2)
        self.assertEqual(len(assignments.assignments_for_user(self.ravi.pk)), 1)
        self.assertEqual(Workflow.objects.count(), workflows_before)

    def test_counts_come_from_the_stage_table(self):
        counts = assignments.assignment_counts([self.mukesh.pk, self.ravi.pk])
        self.assertEqual(counts.get(self.mukesh.pk), 3)
        self.assertIsNone(counts.get(self.ravi.pk))

    def test_inactive_stages_are_excluded_unless_asked_for(self):
        WorkflowStage.objects.filter(pk=self.s_a.pk).update(is_active=False)
        self.assertEqual(len(assignments.assignments_for_user(self.mukesh.pk)), 2)
        self.assertEqual(
            len(assignments.assignments_for_user(self.mukesh.pk,
                                                 include_inactive=True)), 3)


class NoBulkReassignmentTests(TestCase):
    """There is no bulk "move everything from A to B", by design.

    One existed and was removed. A single call rewriting every stage a person
    owns, across every module, is an organisation-wide change with no natural
    review step and no undo — so moving somebody's work is done one stage at a
    time, where the edit is the same size as the thing being looked at.
    """

    def test_the_service_exposes_no_bulk_reassignment(self):
        for gone in ('reassign_user', 'replace_assignments', 'bulk_reassign'):
            self.assertFalse(hasattr(assignments, gone), gone)

    def test_no_bulk_reassignment_route_exists(self):
        from django.urls import NoReverseMatch, reverse
        with self.assertRaises(NoReverseMatch):
            reverse('workflow-stage-reassign')

    def test_moving_one_stage_leaves_the_users_other_stages_alone(self):
        """The single-stage edit is deliberately NOT a global one."""
        mukesh, ravi = make_user('mukesh'), make_user('ravi')
        module = make_module()
        wf_a = make_workflow(module, code='WF_A')
        wf_b = make_workflow(module, code='WF_B')
        s1 = make_stage(wf_a, 1, mukesh)
        s2 = make_stage(wf_b, 1, mukesh)

        s1.user = ravi
        s1.save(update_fields=['user'])

        s2.refresh_from_db()
        self.assertEqual(s2.user_id, mukesh.pk)
        self.assertEqual(len(assignments.assignments_for_user(mukesh.pk)), 1)
        self.assertEqual(len(assignments.assignments_for_user(ravi.pk)), 1)


class TemporaryReplacementTests(TestCase):
    """Operation 3 — a dated stand-in that never edits configuration."""

    @classmethod
    def setUpTestData(cls):
        cls.mukesh = make_user('mukesh')
        cls.ravi = make_user('ravi')
        cls.module = make_module()
        cls.workflow = make_workflow(cls.module, code='WF_A')
        cls.stage = make_stage(cls.workflow, 2, cls.mukesh, name='Finance')
        WorkflowUserReplacement.objects.create(
            old_user=cls.mukesh, new_user=cls.ravi, reason='Annual leave',
            start_date=datetime.date(2026, 9, 15),
            end_date=datetime.date(2026, 9, 30),
        )

    def test_effective_user_across_the_window(self):
        """§37, spelled out per date rather than derived from the same rule."""
        cases = [
            (datetime.date(2026, 9, 14), self.mukesh.pk),   # before
            (datetime.date(2026, 9, 15), self.ravi.pk),     # first day
            (datetime.date(2026, 9, 22), self.ravi.pk),     # inside
            (datetime.date(2026, 9, 30), self.ravi.pk),     # last day
            (datetime.date(2026, 10, 1), self.mukesh.pk),   # after
        ]
        for on_date, expected in cases:
            with self.subTest(date=on_date):
                row = assignments.get_stage_assignment(self.stage.pk,
                                                       effective_date=on_date)
                self.assertEqual(row.effective_user_id, expected)

    def test_the_configured_user_never_changes(self):
        for on_date in (datetime.date(2026, 9, 14), datetime.date(2026, 9, 22),
                        datetime.date(2026, 10, 1)):
            row = assignments.get_stage_assignment(self.stage.pk,
                                                   effective_date=on_date)
            self.assertEqual(row.configured_user_id, self.mukesh.pk)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.user_id, self.mukesh.pk)

    def test_the_view_flags_an_active_replacement(self):
        inside = assignments.get_stage_assignment(
            self.stage.pk, effective_date=datetime.date(2026, 9, 22))
        self.assertTrue(inside.has_active_replacement)
        self.assertEqual(inside.effective_username, 'ravi')
        outside = assignments.get_stage_assignment(
            self.stage.pk, effective_date=datetime.date(2026, 10, 1))
        self.assertFalse(outside.has_active_replacement)

    def test_a_replacement_is_not_a_reassignment(self):
        """The two operations must not be confusable in the data.

        A replacement leaves `workflow_stages` alone; only editing the stage
        writes it. If a replacement ever edited the stage, the original user
        would never come back when the window closed.
        """
        rows = assignments.assignments_for_user(
            self.mukesh.pk, on_date=datetime.date(2026, 9, 22))
        # Still HIS assignment, even while somebody else is covering it.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].configured_user_id, self.mukesh.pk)
        self.assertEqual(rows[0].effective_user_id, self.ravi.pk)
        # And the stand-in has none of their own.
        self.assertEqual(
            assignments.assignments_for_user(
                self.ravi.pk, on_date=datetime.date(2026, 9, 22)), [])


class NoAssignmentTableTests(TestCase):
    """§24/§41 — the relationship is the stage column, not a second table."""

    def test_the_engine_has_only_the_five_configuration_tables(self):
        from django.apps import apps
        tables = {m._meta.db_table.replace('"."', '.')
                  for m in apps.get_app_config('workflow').get_models()}
        self.assertEqual(tables, {
            'workflow.workflow_modules',
            'workflow.workflows',
            'workflow.workflow_queries',
            'workflow.workflow_stages',
            'workflow.workflow_user_replacements',
        })

    def test_no_model_is_named_like_an_assignment_table(self):
        from django.apps import apps
        names = {m.__name__.lower()
                 for m in apps.get_app_config('workflow').get_models()}
        for forbidden in ('workflowuserassignment', 'workflowstageassignment',
                          'workflowuserworkflow', 'workflowtask',
                          'workflowaction', 'workflowpendingentry'):
            self.assertNotIn(forbidden, names)
