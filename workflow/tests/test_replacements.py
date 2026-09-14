"""Date-based replacement: resolution, overlap constraint, account untouched."""
import datetime

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.companies import OIL
from workflow.exceptions import UnauthorizedWorkflowAction
from workflow.models import (
    TestFlow,
    WorkflowUserReplacement,
)
from workflow.services import engine, replacements
from workflow.tests.factories import (
    make_document,
    make_module,
    make_query,
    make_stage,
    make_user,
    make_workflow,
)

User = get_user_model()

D = datetime.date
START = D(2026, 9, 15)
END = D(2026, 9, 22)


class ResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.u5 = make_user('user5')
        cls.u8 = make_user('user8')
        WorkflowUserReplacement.objects.create(
            old_user=cls.u5, new_user=cls.u8,
            reason='leave', start_date=START, end_date=END,
        )

    def test_before_start_date_is_the_original_user(self):
        self.assertEqual(
            replacements.effective_user_id(self.u5.pk, D(2026, 9, 14)),
            self.u5.pk)

    def test_inside_the_window_is_the_replacement(self):
        self.assertEqual(
            replacements.effective_user_id(self.u5.pk, D(2026, 9, 18)),
            self.u8.pk)

    def test_both_boundary_dates_are_inclusive(self):
        for day in (START, END):
            with self.subTest(day=day):
                self.assertEqual(
                    replacements.effective_user_id(self.u5.pk, day),
                    self.u8.pk)

    def test_after_end_date_the_original_user_resumes(self):
        self.assertEqual(
            replacements.effective_user_id(self.u5.pk, D(2026, 9, 23)),
            self.u5.pk)

    def test_resolution_is_single_hop_not_transitive(self):
        u12 = make_user('user12')
        WorkflowUserReplacement.objects.create(
            old_user=self.u8, new_user=u12,
            start_date=START, end_date=END,
        )
        # 5 -> 8, and NOT onward to 12.
        self.assertEqual(
            replacements.effective_user_id(self.u5.pk, D(2026, 9, 18)),
            self.u8.pk)

    def test_unreplaced_user_resolves_to_themselves(self):
        other = make_user('untouched')
        self.assertEqual(
            replacements.effective_user_id(other.pk, D(2026, 9, 18)),
            other.pk)

    def test_the_user_account_is_never_modified(self):
        """The whole point: a replacement changes routing, not the account."""
        before = User.objects.filter(pk=self.u5.pk).values().first()
        replacements.effective_user_id(self.u5.pk, D(2026, 9, 18))
        replacements.effective_user_id(self.u5.pk, D(2026, 9, 30))
        after = User.objects.filter(pk=self.u5.pk).values().first()
        self.assertEqual(before, after)
        self.assertTrue(User.objects.get(pk=self.u5.pk).is_active)


class OverlapConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.u5 = make_user('ov-user5')
        cls.u8 = make_user('ov-user8')
        cls.u9 = make_user('ov-user9')

    def _make(self, start, end, new_user=None):
        return WorkflowUserReplacement.objects.create(
            old_user=self.u5, new_user=new_user or self.u8,
            start_date=start, end_date=end,
        )

    def test_adjacent_windows_are_allowed(self):
        self._make(D(2026, 9, 15), D(2026, 9, 22))
        self._make(D(2026, 9, 23), D(2026, 9, 30), new_user=self.u9)
        self.assertEqual(
            WorkflowUserReplacement.objects.filter(old_user=self.u5).count(), 2)

    def test_overlapping_windows_are_rejected_by_the_database(self):
        self._make(D(2026, 9, 15), D(2026, 9, 22))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make(D(2026, 9, 20), D(2026, 9, 25), new_user=self.u9)

    def test_touching_boundary_counts_as_overlap(self):
        """Bounds are inclusive, so 22->22 collides with a window ending 22."""
        self._make(D(2026, 9, 15), D(2026, 9, 22))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make(D(2026, 9, 22), D(2026, 9, 30), new_user=self.u9)

    def test_different_users_may_share_identical_dates(self):
        other = make_user('ov-other')
        self._make(D(2026, 9, 15), D(2026, 9, 22))
        WorkflowUserReplacement.objects.create(
            old_user=other, new_user=self.u9,
            start_date=D(2026, 9, 15), end_date=D(2026, 9, 22),
        )
        self.assertEqual(WorkflowUserReplacement.objects.count(), 2)

    def test_self_replacement_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkflowUserReplacement.objects.create(
                    old_user=self.u5, new_user=self.u5,
                    start_date=D(2026, 9, 15), end_date=D(2026, 9, 22),
                )

    def test_reversed_dates_are_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make(D(2026, 9, 22), D(2026, 9, 15))


class ReplacementAuthorisationTests(TestCase):
    """A stand-in may act during the window; the original may not."""

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.u5 = make_user('auth-user5')
        cls.u8 = make_user('auth-user8')
        wf = make_workflow(cls.module, company=None)
        make_query(wf, company=None)
        make_stage(wf, 1, cls.u5)
        cls.workflow = wf

    def _open_task(self):
        doc = make_document(company=OIL)
        flow = TestFlow.objects.create(document=doc, company=OIL)
        _flow, task = engine.start(module=self.module, flow=flow,
                                   document_key=doc.pk, company=OIL,
                                   user=self.u5)
        return task

    def test_task_records_the_configured_user_not_the_effective_one(self):
        WorkflowUserReplacement.objects.create(
            old_user=self.u5, new_user=self.u8,
            start_date=replacements.today(), end_date=replacements.today(),
        )
        task = self._open_task()
        # Configured user is stored; the stand-in is resolved dynamically.
        self.assertEqual(task.stage_user_id, self.u5.pk)

    def test_standin_can_act_and_original_cannot_during_the_window(self):
        today = replacements.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.u5, new_user=self.u8,
            start_date=today, end_date=today,
        )
        task = self._open_task()

        with self.assertRaises(UnauthorizedWorkflowAction):
            engine.approve(task_id=task.pk, user=self.u5)

        flow, _ = engine.approve(task_id=task.pk, user=self.u8)
        self.assertEqual(flow.status, 'APPROVED')

    def test_action_history_records_who_acted_for_whom(self):
        today = replacements.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.u5, new_user=self.u8,
            start_date=today, end_date=today,
        )
        task = self._open_task()
        engine.approve(task_id=task.pk, user=self.u8)

        action = (engine.history_for(self.module, task.flow_id)
                  .filter(action='APPROVE').first())
        self.assertEqual(action.acted_by_id, self.u8.pk)
        self.assertEqual(action.on_behalf_of_id, self.u5.pk)

    def test_original_resumes_on_an_already_open_task(self):
        """The window closes and the ALREADY OPEN task returns to user 5.

        This is why the configured user is stored rather than the effective
        one — nothing about the task row changes.
        """
        today = replacements.today()
        yesterday = today - datetime.timedelta(days=1)
        WorkflowUserReplacement.objects.create(
            old_user=self.u5, new_user=self.u8,
            start_date=yesterday - datetime.timedelta(days=1),
            end_date=yesterday,
        )
        task = self._open_task()
        # The window ended yesterday, so today the original user acts.
        flow, _ = engine.approve(task_id=task.pk, user=self.u5)
        self.assertEqual(flow.status, 'APPROVED')

    def test_inbox_follows_the_replacement(self):
        today = replacements.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.u5, new_user=self.u8,
            start_date=today, end_date=today,
        )
        self._open_task()
        self.assertEqual(engine.inbox_for(self.u8).count(), 1)
        self.assertEqual(engine.inbox_for(self.u5).count(), 0)
