"""Selection, stages, approve/reject, and the module-owned resubmission rule."""
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.companies import OIL
from workflow.exceptions import (
    AmbiguousWorkflowSelection,
    InvalidWorkflowAction,
    InvalidWorkflowConfiguration,
    StageUserUnavailable,
    UnauthorizedWorkflowAction,
    WorkflowAlreadyRunning,
    WorkflowNotConfigured,
)
from workflow.models import (
    ActionType,
    FlowStatus,
    TaskStatus,
    TestFlow,
    WorkflowAction,
    WorkflowTask,
)
from workflow.services import engine
from workflow.tests.factories import (
    make_document,
    make_module,
    make_query,
    make_stage,
    make_user,
    make_workflow,
)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.u1 = make_user('stage1-user')
        cls.u2 = make_user('stage2-user')
        cls.outsider = make_user('outsider')

    def _configure(self, stages=1, where=''):
        wf = make_workflow(self.module, company=None)
        make_query(wf, where=where, company=None)
        users = [self.u1, self.u2]
        for seq in range(1, stages + 1):
            make_stage(wf, seq, users[(seq - 1) % 2])
        return wf

    def _start(self, doc=None, company=OIL):
        doc = doc or make_document(company=company)
        flow = TestFlow.objects.create(document=doc, company=company)
        return doc, engine.start(module=self.module, flow=flow,
                                document_key=doc.pk, company=company,
                                user=self.u1)


class SelectionTests(_Base):
    def test_zero_matches_raises_not_configured(self):
        doc = make_document()
        flow = TestFlow.objects.create(document=doc, company=OIL)
        with self.assertRaises(WorkflowNotConfigured):
            engine.start(module=self.module, flow=flow, document_key=doc.pk,
                         company=OIL, user=self.u1)

    def test_one_match_selects_and_records_why(self):
        wf = self._configure()
        doc, (flow, task) = self._start()
        self.assertEqual(flow.workflow_id, wf.pk)
        self.assertIsNotNone(flow.matched_query_id)
        self.assertEqual(flow.context_snapshot['selected_workflow'], wf.code)
        self.assertEqual(task.status, TaskStatus.PENDING)

    def test_two_matches_roll_back_completely(self):
        a = self._configure()
        b = make_workflow(self.module, code='WF_B', company=None)
        make_query(b, name='b', company=None)
        make_stage(b, 1, self.u1)

        doc = make_document()
        flow = TestFlow.objects.create(document=doc, company=OIL)
        with self.assertRaises(AmbiguousWorkflowSelection):
            engine.start(module=self.module, flow=flow, document_key=doc.pk,
                         company=OIL, user=self.u1)

        # Nothing partial: no task, no history, flow still unstarted.
        flow.refresh_from_db()
        self.assertIsNone(flow.workflow_id)
        self.assertIsNone(flow.current_stage_id)
        self.assertEqual(WorkflowTask.objects.count(), 0)
        self.assertEqual(WorkflowAction.objects.count(), 0)

    def test_every_query_is_evaluated_not_just_the_first(self):
        """A second matching workflow must be FOUND, not short-circuited past."""
        self._configure()
        b = make_workflow(self.module, code='WF_B', company=None)
        make_query(b, name='b', company=None)
        make_stage(b, 1, self.u1)
        doc = make_document()
        matched = engine.selection.evaluate(self.module, doc.pk, OIL)
        self.assertEqual(len(matched), 2)

    def test_workflow_without_stages_is_refused(self):
        wf = make_workflow(self.module, company=None)
        make_query(wf, company=None)
        doc = make_document()
        flow = TestFlow.objects.create(document=doc, company=OIL)
        with self.assertRaises(InvalidWorkflowConfiguration):
            engine.start(module=self.module, flow=flow, document_key=doc.pk,
                         company=OIL, user=self.u1)

    def test_query_binds_the_document_key(self):
        """A query selecting a narrower set must not match other documents."""
        self._configure(where="amount > 5000")
        rich = make_document(amount=9000)
        poor = make_document(amount=10)

        _flow, _task = self._start(doc=rich)[1]
        flow2 = TestFlow.objects.create(document=poor, company=OIL)
        with self.assertRaises(WorkflowNotConfigured):
            engine.start(module=self.module, flow=flow2,
                         document_key=poor.pk, company=OIL, user=self.u1)


class StageTests(_Base):
    def test_exactly_one_task_per_open_stage(self):
        self._configure(stages=2)
        _doc, (flow, task) = self._start()
        self.assertEqual(
            WorkflowTask.objects.filter(flow_id=flow.pk,
                                        status=TaskStatus.PENDING).count(), 1)
        self.assertEqual(task.stage_user_id, self.u1.pk)

    def test_second_open_task_for_a_stage_is_refused_by_the_database(self):
        self._configure()
        _doc, (flow, task) = self._start()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkflowTask.objects.create(
                    module=self.module, flow_id=flow.pk, stage=task.stage,
                    sequence=1, stage_user=self.u1,
                    status=TaskStatus.PENDING,
                )

    def test_inactive_stage_user_is_refused_at_open(self):
        wf = make_workflow(self.module, company=None)
        make_query(wf, company=None)
        dormant = make_user('dormant', is_active=False)
        make_stage(wf, 1, dormant)
        doc = make_document()
        flow = TestFlow.objects.create(document=doc, company=OIL)
        with self.assertRaises(StageUserUnavailable):
            engine.start(module=self.module, flow=flow, document_key=doc.pk,
                         company=OIL, user=self.u1)


class ApprovalTests(_Base):
    def test_approve_completes_stage_and_opens_the_next(self):
        self._configure(stages=2)
        _doc, (flow, task) = self._start()

        flow, next_task = engine.approve(task_id=task.pk, user=self.u1)
        task.refresh_from_db()

        self.assertEqual(task.status, TaskStatus.APPROVED)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertEqual(flow.current_sequence, 2)
        self.assertEqual(next_task.stage_user_id, self.u2.pk)
        # One approve was enough — no counter, no quorum.
        self.assertEqual(
            WorkflowTask.objects.filter(flow_id=flow.pk,
                                        status=TaskStatus.PENDING).count(), 1)

    def test_approving_the_last_stage_completes_the_workflow(self):
        self._configure(stages=1)
        _doc, (flow, task) = self._start()
        flow, next_task = engine.approve(task_id=task.pk, user=self.u1)
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertIsNone(next_task)
        self.assertIsNone(flow.current_stage_id)

    def test_unassigned_user_cannot_approve(self):
        self._configure()
        _doc, (_flow, task) = self._start()
        with self.assertRaises(UnauthorizedWorkflowAction):
            engine.approve(task_id=task.pk, user=self.outsider)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.PENDING)

    def test_second_action_on_a_decided_task_is_refused(self):
        self._configure()
        _doc, (_flow, task) = self._start()
        engine.approve(task_id=task.pk, user=self.u1)
        with self.assertRaises(InvalidWorkflowAction):
            engine.approve(task_id=task.pk, user=self.u1)

    def test_history_is_append_only_and_ordered(self):
        self._configure(stages=2)
        _doc, (flow, task) = self._start()
        _flow, next_task = engine.approve(task_id=task.pk, user=self.u1)
        engine.approve(task_id=next_task.pk, user=self.u2)

        actions = list(engine.history_for(self.module, flow.pk)
                       .values_list('sequence', 'action'))
        self.assertEqual(
            actions,
            [(1, ActionType.SUBMIT), (2, ActionType.APPROVE),
             (3, ActionType.APPROVE)],
        )


class RejectionTests(_Base):
    def test_reject_ends_the_workflow_execution(self):
        self._configure(stages=2)
        _doc, (flow, task) = self._start()
        flow, _ = engine.reject(task_id=task.pk, user=self.u1,
                                remarks='not this time')
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.REJECTED)
        self.assertEqual(flow.status, FlowStatus.REJECTED)
        # Stage 2 was never opened — one reject ends it.
        self.assertEqual(WorkflowTask.objects.filter(
            flow_id=flow.pk, status=TaskStatus.PENDING).count(), 0)

    def test_no_further_action_after_rejection(self):
        self._configure()
        _doc, (_flow, task) = self._start()
        engine.reject(task_id=task.pk, user=self.u1)
        with self.assertRaises(InvalidWorkflowAction):
            engine.approve(task_id=task.pk, user=self.u1)


class ExecutionStateTests(_Base):
    """One flow row per document; re-invocation reuses it."""

    def test_second_start_while_running_is_refused(self):
        self._configure()
        doc, (flow, _task) = self._start()
        with self.assertRaises(WorkflowAlreadyRunning):
            engine.start(module=self.module, flow=flow, document_key=doc.pk,
                         company=OIL, user=self.u1)

    def test_one_flow_row_per_document_enforced_by_database(self):
        self._configure()
        doc, _ = self._start()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TestFlow.objects.create(document=doc, company=OIL)

    def test_reinvocation_after_rejection_reuses_the_same_row(self):
        """The engine must NOT create a second flow row (plan §4.9.1)."""
        self._configure(stages=2)
        doc, (flow, task) = self._start()
        original_pk = flow.pk
        original_created = flow.created_at

        engine.reject(task_id=task.pk, user=self.u1)

        # The module decides to resubmit and calls the engine again.
        flow2, task2 = engine.start(module=self.module, flow=flow,
                                    document_key=doc.pk, company=OIL,
                                    user=self.u1)

        self.assertEqual(flow2.pk, original_pk)
        self.assertEqual(flow2.created_at, original_created)
        self.assertEqual(TestFlow.objects.filter(document=doc).count(), 1)
        self.assertEqual(flow2.status, FlowStatus.PENDING)
        self.assertEqual(flow2.current_sequence, 1)
        self.assertEqual(task2.stage_user_id, self.u1.pk)

    def test_history_continues_across_executions_with_no_attempt_counter(self):
        self._configure()
        doc, (flow, task) = self._start()
        engine.reject(task_id=task.pk, user=self.u1)
        before = list(engine.history_for(self.module, flow.pk)
                      .values_list('sequence', 'action'))

        engine.start(module=self.module, flow=flow, document_key=doc.pk,
                     company=OIL, user=self.u1)
        after = list(engine.history_for(self.module, flow.pk)
                     .values_list('sequence', 'action'))

        # Earlier rows untouched; the new SUBMIT continues the sequence.
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after[-1], (3, ActionType.SUBMIT))

    def test_engine_has_no_resubmitted_action(self):
        self.assertNotIn('RESUBMITTED', ActionType.values)

    def test_terminal_tasks_survive_reexecution(self):
        self._configure()
        doc, (flow, task) = self._start()
        engine.reject(task_id=task.pk, user=self.u1)
        engine.start(module=self.module, flow=flow, document_key=doc.pk,
                     company=OIL, user=self.u1)

        statuses = sorted(WorkflowTask.objects.filter(flow_id=flow.pk)
                          .values_list('status', flat=True))
        self.assertEqual(statuses, [TaskStatus.PENDING, TaskStatus.REJECTED])


class InboxTests(_Base):
    def test_inbox_returns_only_the_users_pending_tasks(self):
        self._configure(stages=2)
        self._start()
        self.assertEqual(engine.inbox_for(self.u1).count(), 1)
        self.assertEqual(engine.inbox_for(self.u2).count(), 0)
        self.assertEqual(engine.inbox_for(self.outsider).count(), 0)
