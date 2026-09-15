"""BackDate (BKDT) — the rules worth holding down.

The cases here are the ones where a silent failure would be expensive, and
most of them are things the JSAP predecessor got wrong:

* approval needs BOTH a permission and the stage assignment — JSAP took the
  approver's id from the request body;
* identity always comes from the session — JSAP trusted `createdBy`;
* a request that cannot be routed is not stored — JSAP left it orphaned;
* a temporary replacement changes who may act without touching configuration;
* changing a stage's user re-routes requests already waiting there, with
  nothing migrated;
* an approved request whose SAP write failed is NOT reported as fully done;
* SAP receives the REQUESTER's id and the REQUEST's timestamp, never the
  approver's;
* an expiry is mandatory, because SAP ignores a grant without one.

Plus the shape of the cleaned data model itself: three tables, no task table,
and a log that duplicates nothing the workflow engine already holds.
"""
import datetime
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.companies import BEVERAGES, MART, OIL
from workflow.models import (
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowUserReplacement,
)
from workflow.services import conditions

from backdate.models import (
    BackDate,
    BackDateActionLog,
    BackDateFlow,
    FlowStatus,
    HanaStatus,
    LogAction,
    RequestAction,
)
from backdate.services import flow as flow_service

User = get_user_model()

#: The real table a condition query reads — the same relation the module
#: writes. This mirrors JSAP, whose queries selected from its own request table.
DOC_TABLE = 'backdate.backdate'

#: Sentinel for "leave this key out of the payload entirely".
_OMIT = object()


def make_user(username, *keys):
    user = User.objects.create_user(username=username, password='test-pass-12345')
    if keys:
        user.extra_pages = list(keys)
        user.save(update_fields=['extra_pages'])
    return user


#: Every request carries an expiry, because SAP ignores one that does not —
#: see `backdate/migrations/0002_require_time_limit.py`.
TIME_LIMIT = datetime.datetime(2026, 12, 31, 18, 30, tzinfo=datetime.timezone.utc)


def make_request(creator, *, company=OIL, to_date=datetime.date(2026, 5, 20),
                 time_limit=TIME_LIMIT):
    return BackDate.objects.create(
        company=company,
        sap_username='USER12',
        document_type=13,
        from_date=datetime.date(2026, 5, 1),
        to_date=to_date,
        time_limit=time_limit,
        action=RequestAction.ADD,
        created_by=creator,
        remarks='month-end close',
    )


class _Base(TestCase):
    """One module, one workflow, two stages — the shape template 452 has."""

    @classmethod
    def setUpTestData(cls):
        cls.module, _ = WorkflowModule.objects.get_or_create(
            code='BKDT', defaults={'name': 'BackDate'})
        cls.requester = make_user('bk-requester', 'BackDate')
        cls.approver1 = make_user('bk-approver1', 'BackDate', 'BackDate_Approval')
        cls.approver2 = make_user('bk-approver2', 'BackDate', 'BackDate_Approval')
        cls.outsider = make_user('bk-outsider', 'BackDate')

        cls.workflow = Workflow.objects.create(
            module=cls.module, code='BKDT_OIL', name='OIL', company=OIL)
        cls.query = WorkflowQuery.objects.create(
            workflow=cls.workflow, name='oil', company=OIL,
            query_text=(f"SELECT * FROM {DOC_TABLE} "
                        f"WHERE ',' || company || ',' LIKE '%,OIL,%'"))
        conditions.validate_and_stamp(cls.query)
        cls.stage1 = WorkflowStage.objects.create(
            workflow=cls.workflow, name='Manager', sequence=1, user=cls.approver1)
        cls.stage2 = WorkflowStage.objects.create(
            workflow=cls.workflow, name='Finance', sequence=2, user=cls.approver2)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def approve_url(self, backdate):
        return reverse('backdate-request-approve', args=[backdate.pk])

    def reject_url(self, backdate):
        return reverse('backdate-request-reject', args=[backdate.pk])


# ---------------------------------------------------------------------------
# The cleaned data model
# ---------------------------------------------------------------------------

class DataModelTests(_Base):
    """What the schema must and must not contain after the clean-up."""

    def _columns(self, table):
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'backdate' AND table_name = %s""",
                [table])
            return {row[0] for row in cursor.fetchall()}

    def _tables(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'backdate'""")
            return {row[0] for row in cursor.fetchall()}

    def test_the_schema_holds_exactly_three_tables(self):
        self.assertEqual(
            self._tables(),
            {'backdate', 'backdate_flow', 'backdate_action_logs'})

    def test_there_is_no_task_table(self):
        """And no replacement for it under another name."""
        tables = self._tables()
        for banned in ('backdate_approval_task', 'backdate_task',
                       'approval_task', 'workflow_task'):
            self.assertNotIn(banned, tables)

    def test_the_flow_holds_no_duplicated_stage_state(self):
        columns = self._columns('backdate_flow')
        self.assertEqual(
            columns,
            {'id', 'backdate_id', 'status', 'hana_status', 'sap_payload',
             'hana_status_text', 'workflow_id', 'current_user_id',
             'current_stage', 'total_stage', 'created_at', 'updated_at'})
        for banned in ('current_sequence', 'hana_applied_at',
                       'matched_query_id', 'request_id',
                       'sequence_no', 'applied_at', 'query_match_id'):
            self.assertNotIn(banned, columns)

    def test_the_log_holds_no_duplicated_workflow_data(self):
        columns = self._columns('backdate_action_logs')
        self.assertEqual(
            columns,
            {'id', 'backdate_id', 'action', 'acted_by_id', 'stage_id',
             'remarks', 'action_data', 'acted_at'})
        for banned in ('sequence', 'stage_name', 'on_behalf_of_id', 'flow_id'):
            self.assertNotIn(banned, columns)

    def test_action_data_is_jsonb(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT data_type FROM information_schema.columns
                   WHERE table_schema='backdate'
                     AND table_name='backdate_action_logs'
                     AND column_name='action_data'""")
            self.assertEqual(cursor.fetchone()[0], 'jsonb')

    def test_hana_status_offers_only_success_and_failed(self):
        self.assertEqual(list(HanaStatus.values), ['SUCCESS', 'FAILED'])

    def test_the_log_vocabulary_is_the_business_lifecycle(self):
        self.assertEqual(list(LogAction.values),
                         ['CREATE', 'UPDATE', 'APPROVE', 'REJECT'])


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

class SubmissionTests(_Base):
    def test_submission_selects_a_workflow_and_points_at_stage_one(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)

        self.assertEqual(flow.workflow_id, self.workflow.pk)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertEqual(flow.current_stage_id, self.stage1.pk)
        self.assertEqual(flow.current_user_id, self.approver1.pk)
        self.assertEqual(flow.total_stage, 2)
        self.assertIsNone(flow.hana_status)

    def test_the_flow_stores_the_stage_not_a_copy_of_its_configuration(self):
        """The whole reason a stage-user change needs no data migration."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)

        names = {f.name for f in BackDateFlow._meta.get_fields()}
        for forbidden in ('stage_name', 'stage_sequence', 'current_sequence',
                          'matched_query', 'user_name'):
            self.assertNotIn(forbidden, names)
        self.assertEqual(flow.current_stage_id, self.stage1.pk)

    def test_creation_writes_one_create_log_with_no_stage(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)

        logs = list(req.action_logs.all())
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action, LogAction.CREATE)
        self.assertIsNone(logs[0].stage_id)
        self.assertEqual(logs[0].acted_by_id, self.requester.pk)
        self.assertIsNone(logs[0].action_data)

    def test_a_request_that_cannot_be_routed_is_not_stored(self):
        """JSAP created the row and left it with no flow, silently."""
        before = BackDate.objects.count()
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'), {
                # No workflow is configured for BEVERAGES.
                'company': BEVERAGES, 'sap_username': 'USER12',
                'document_type': 13, 'from_date': '2026-05-01',
                'to_date': '2026-05-20', 'action': 'A',
                'time_limit': '2026-12-31T18:30:00Z',
            }, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(BackDate.objects.count(), before)

    def test_created_by_comes_from_the_session_not_the_payload(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'), {
                'company': OIL, 'sap_username': 'USER12', 'document_type': 13,
                'from_date': '2026-05-01', 'to_date': '2026-05-20',
                'action': 'A', 'time_limit': '2026-12-31T18:30:00Z',
                'created_by': self.outsider.pk,
            }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            BackDate.objects.get(pk=response.data['data']['id']).created_by_id,
            self.requester.pk)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

class UpdateTests(_Base):
    """Editing a pending request records EXACTLY what changed."""

    def setUp(self):
        self.req = make_request(self.requester)
        flow_service.submit(self.req, user=self.requester)
        self.url = reverse('backdate-request-detail', args=[self.req.pk])

    def test_an_edit_logs_only_the_changed_fields(self):
        response = self.client_for(self.requester).patch(
            self.url, {'from_date': '2026-05-18'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

        log = self.req.action_logs.get(action=LogAction.UPDATE)
        self.assertEqual(log.action_data, {
            'from_date': {'old': '2026-05-01', 'new': '2026-05-18'},
        })
        # `to_date` did not change, so it is not in the payload.
        self.assertNotIn('to_date', log.action_data)
        self.assertIsNone(log.stage_id)
        self.assertEqual(log.acted_by_id, self.requester.pk)

    def test_several_changed_fields_are_all_described(self):
        self.client_for(self.requester).patch(
            self.url,
            {'sap_username': 'USER99', 'document_type': 15,
             'remarks': 'corrected'},
            format='json')
        data = self.req.action_logs.get(action=LogAction.UPDATE).action_data
        self.assertEqual(set(data), {'sap_username', 'document_type',
                                     'remarks'})
        self.assertEqual(data['sap_username'],
                         {'old': 'USER12', 'new': 'USER99'})
        # Numbers stay numbers; only dates become strings.
        self.assertEqual(data['document_type'], {'old': 13, 'new': 15})

    def test_dates_and_timestamps_are_iso_strings(self):
        self.client_for(self.requester).patch(
            self.url, {'time_limit': '2026-11-30T12:00:00Z'}, format='json')
        data = self.req.action_logs.get(action=LogAction.UPDATE).action_data
        self.assertTrue(data['time_limit']['old'].startswith('2026-12-31T'))
        self.assertTrue(data['time_limit']['new'].startswith('2026-11-30T'))

    def test_an_edit_that_changes_nothing_writes_no_log(self):
        self.client_for(self.requester).patch(
            self.url, {'sap_username': 'USER12'}, format='json')
        self.assertFalse(
            self.req.action_logs.filter(action=LogAction.UPDATE).exists())

    def test_only_the_requester_may_edit(self):
        response = self.client_for(self.outsider).patch(
            self.url, {'remarks': 'not mine'}, format='json')
        self.assertIn(response.status_code, (403, 404))

    def test_a_decided_request_cannot_be_edited(self):
        self.client_for(self.approver1).post(
            self.approve_url(self.req), {}, format='json')
        response = self.client_for(self.requester).patch(
            self.url, {'remarks': 'too late'}, format='json')
        # Stage 1 has approved: the terms an approver agreed to must not move
        # underneath them.
        self.assertEqual(response.status_code, 409, response.data)

    def test_history_is_append_only_across_create_update_approve(self):
        self.client_for(self.requester).patch(
            self.url, {'remarks': 'revised'}, format='json')
        self.client_for(self.approver1).post(
            self.approve_url(self.req), {}, format='json')

        logs = list(self.req.action_logs.order_by('acted_at', 'id'))
        self.assertEqual([entry.action for entry in logs],
                         [LogAction.CREATE, LogAction.UPDATE,
                          LogAction.APPROVE])
        # The earlier rows are untouched by the later ones.
        self.assertIsNone(logs[0].action_data)
        self.assertIsNone(logs[0].stage_id)
        self.assertEqual(logs[1].action_data,
                         {'remarks': {'old': 'month-end close',
                                      'new': 'revised'}})
        self.assertEqual(logs[2].stage_id, self.stage1.pk)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

class ApprovalPermissionTests(_Base):
    """The A/B/C matrix. BOTH conditions, enforced server-side."""

    def setUp(self):
        self.req = make_request(self.requester)
        self.flow = flow_service.submit(self.req, user=self.requester)
        self.url = self.approve_url(self.req)

    def test_permission_without_stage_assignment_is_refused(self):
        """User B: holds the key, is not this stage's approver."""
        response = self.client_for(self.approver2).post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 403)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.current_stage_id, self.stage1.pk)

    def test_stage_assignment_without_permission_is_refused(self):
        """User A: is the stage's approver, lacks the key."""
        WorkflowStage.objects.filter(pk=self.stage1.pk).update(user=self.outsider)
        response = self.client_for(self.outsider).post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 403)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.status, FlowStatus.PENDING)

    def test_both_together_are_allowed(self):
        """User C."""
        response = self.client_for(self.approver1).post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.current_stage_id, self.stage2.pk)

    def test_anonymous_is_refused(self):
        self.assertIn(APIClient().post(self.url, {}, format='json').status_code,
                      (401, 403))

    def test_the_two_refusals_say_different_things(self):
        """They send the user to different places, so they must differ."""
        no_key = self.client_for(self.outsider).post(self.url, {}, format='json')
        WorkflowStage.objects.filter(pk=self.stage1.pk).update(user=self.approver1)
        wrong_stage = self.client_for(self.approver2).post(self.url, {}, format='json')
        self.assertNotEqual(no_key.data['message'], wrong_stage.data['message'])


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------

class ProgressionTests(_Base):
    def test_two_stages_then_approved(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)

        flow = flow_service.approve(flow, user=self.approver1)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertEqual(flow.current_stage_id, self.stage2.pk)
        self.assertEqual(flow.current_user_id, self.approver2.pk)

        flow = flow_service.approve(flow, user=self.approver2)
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        # Nothing is waiting any more, so nothing should claim to be.
        self.assertIsNone(flow.current_stage_id)
        self.assertIsNone(flow.current_user_id)

    def test_the_stage_and_its_user_move_together(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        pairs = [(flow.current_stage_id, flow.current_user_id)]
        flow = flow_service.approve(flow, user=self.approver1)
        pairs.append((flow.current_stage_id, flow.current_user_id))
        self.assertEqual(pairs, [(self.stage1.pk, self.approver1.pk),
                                 (self.stage2.pk, self.approver2.pk)])

    def test_one_rejection_ends_the_flow(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)

        flow = flow_service.reject(flow, user=self.approver1,
                                   remarks='too far back')
        self.assertEqual(flow.status, FlowStatus.REJECTED)
        # The second stage never opens, and nothing is left pointing at one.
        self.assertIsNone(flow.current_stage_id)
        self.assertIsNone(flow.current_user_id)

    def test_a_rejection_records_the_stage_it_happened_at(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow_service.reject(flow, user=self.approver1, remarks='no')

        log = req.action_logs.get(action=LogAction.REJECT)
        self.assertEqual(log.stage_id, self.stage1.pk)
        self.assertEqual(log.acted_by_id, self.approver1.pk)
        self.assertEqual(log.remarks, 'no')

    def test_rejection_requires_a_reason(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        with self.assertRaises(flow_service.BackDateError):
            flow_service.reject(flow, user=self.approver1, remarks='   ')

    def test_a_decided_request_cannot_be_decided_twice(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow_service.approve(flow, user=self.approver1)
        flow_service.approve(flow, user=self.approver2)
        with self.assertRaises(flow_service.BackDateError):
            flow_service.approve(flow, user=self.approver2)

    def test_the_queue_is_a_filter_on_the_flow(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)

        queue = self.client_for(self.approver1).get(
            reverse('backdate-approval-queue')).data['data']
        self.assertEqual([row['id'] for row in queue], [req.pk])
        # Stage 2's user has nothing yet.
        self.assertEqual(
            self.client_for(self.approver2).get(
                reverse('backdate-approval-queue')).data['data'], [])


# ---------------------------------------------------------------------------
# Who is responsible
# ---------------------------------------------------------------------------

class StageUserChangeTests(_Base):
    """Changing who works a stage re-routes work already waiting there."""

    def test_existing_requests_follow_the_new_stage_user(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        url = self.approve_url(req)

        # The original approver can act.
        self.assertEqual(
            self.client_for(self.approver1).get(
                reverse('backdate-approval-queue')).data['data'][0]['id'], req.pk)

        # An administrator moves the stage to somebody else. ONE column.
        new_user = make_user('bk-new-approver', 'BackDate_Approval')
        WorkflowStage.objects.filter(pk=self.stage1.pk).update(user=new_user)

        # Nothing about the flow changed...
        flow.refresh_from_db()
        self.assertEqual(flow.current_stage_id, self.stage1.pk)
        self.assertEqual(flow.status, FlowStatus.PENDING)

        # ...but responsibility did.
        self.assertEqual(
            self.client_for(self.approver1).post(url, {}, format='json').status_code, 403)
        self.assertEqual(
            self.client_for(new_user).post(url, {}, format='json').status_code, 200)

    def test_the_queue_follows_the_new_user_with_no_write(self):
        """`current_user` is stale until the flow moves — the queue is not."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        new_user = make_user('bk-successor', 'BackDate_Approval')
        WorkflowStage.objects.filter(pk=self.stage1.pk).update(user=new_user)

        flow.refresh_from_db()
        self.assertEqual(flow.current_user_id, self.approver1.pk)
        queue = self.client_for(new_user).get(
            reverse('backdate-approval-queue')).data['data']
        self.assertEqual([row['id'] for row in queue], [req.pk])

    def test_changing_a_stage_user_creates_no_workflow(self):
        before = (Workflow.objects.count(), WorkflowStage.objects.count(),
                  WorkflowQuery.objects.count(), BackDateFlow.objects.count())
        WorkflowStage.objects.filter(pk=self.stage1.pk).update(user=self.approver2)
        after = (Workflow.objects.count(), WorkflowStage.objects.count(),
                 WorkflowQuery.objects.count(), BackDateFlow.objects.count())
        self.assertEqual(before, after)


class ReplacementTests(_Base):
    """A dated stand-in acts; the configured user does not, and is unchanged."""

    def test_the_stand_in_acts_during_the_window(self):
        stand_in = make_user('bk-standin', 'BackDate_Approval')
        today = datetime.date.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.approver1, new_user=stand_in,
            reason='leave', start_date=today, end_date=today)

        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        url = self.approve_url(req)

        # The configured user may NOT act while covered...
        self.assertEqual(
            self.client_for(self.approver1).post(url, {}, format='json').status_code, 403)
        # ...the stand-in may.
        self.assertEqual(
            self.client_for(stand_in).post(url, {}, format='json').status_code, 200)

        # Configuration is untouched, and the log says who actually acted.
        self.stage1.refresh_from_db()
        self.assertEqual(self.stage1.user_id, self.approver1.pk)
        log = req.action_logs.get(action=LogAction.APPROVE)
        self.assertEqual(log.acted_by_id, stand_in.pk)

    def test_outside_the_window_the_configured_user_acts(self):
        stand_in = make_user('bk-standin2', 'BackDate_Approval')
        past = datetime.date.today() - datetime.timedelta(days=10)
        WorkflowUserReplacement.objects.create(
            old_user=self.approver1, new_user=stand_in, reason='leave',
            start_date=past, end_date=past + datetime.timedelta(days=1))

        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        url = self.approve_url(req)

        self.assertEqual(
            self.client_for(stand_in).post(url, {}, format='json').status_code, 403)
        self.assertEqual(
            self.client_for(self.approver1).post(url, {}, format='json').status_code, 200)


# ---------------------------------------------------------------------------
# SAP
# ---------------------------------------------------------------------------

class HanaTests(_Base):
    """SAP is written on final approval, and the outcome is reported honestly."""

    def _approve_to_final(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        self.client_for(self.approver1).post(
            self.approve_url(req), {}, format='json')
        response = self.client_for(self.approver2).post(
            self.approve_url(req), {}, format='json')
        return req, response

    @mock.patch('backdate.services.hana.apply_grant')
    def test_final_approval_writes_to_sap(self, apply_grant):
        apply_grant.return_value = 'Rights applied in SAP.'
        _req, response = self._approve_to_final()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(apply_grant.called)
        self.assertIs(response.data['data']['hana_applied'], True)

    @mock.patch('backdate.services.hana.apply_grant')
    def test_a_non_final_approval_does_not_write_to_sap(self, apply_grant):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        self.client_for(self.approver1).post(
            self.approve_url(req), {}, format='json')
        self.assertFalse(apply_grant.called)

    @mock.patch('backdate.services.hana.apply_grant')
    def test_a_failed_sap_write_is_not_reported_as_success(self, apply_grant):
        """JSAP returned 200/Success=true here even with no SAP write."""
        from backdate.services.hana import HanaWriteError
        apply_grant.side_effect = HanaWriteError('SAP refused the rights.')

        req, response = self._approve_to_final()
        self.assertIs(response.data['data']['hana_applied'], False)
        self.assertIn('could not be applied', response.data['message'])
        # The approval itself stands — it was committed before SAP was called.
        self.assertEqual(BackDateFlow.objects.get(backdate=req).status,
                         FlowStatus.APPROVED)

    def test_a_successful_write_records_success(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        with mock.patch('backdate.services.hana.HANAConnection'), \
                mock.patch('backdate.services.hana.Queries'):
            from backdate.services import hana
            hana.apply_grant(flow)
        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.SUCCESS)
        self.assertTrue(flow.hana_status_text)

    def test_a_failed_write_records_failed(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        from backdate.services import hana
        with mock.patch.object(hana, 'HANAConnection',
                               side_effect=RuntimeError('down')):
            with self.assertRaises(hana.HanaWriteError):
                hana.apply_grant(flow)
        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)
        self.assertTrue(flow.hana_status_text)

    def test_the_sap_outcome_is_not_duplicated_into_the_log(self):
        """Two copies of one fact is how the two come to disagree."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        from backdate.services import hana
        with mock.patch.object(hana, 'HANAConnection',
                               side_effect=RuntimeError('down')):
            with self.assertRaises(hana.HanaWriteError):
                hana.apply_grant(flow)
        self.assertEqual(
            set(req.action_logs.values_list('action', flat=True)),
            {LogAction.CREATE})

    def test_the_payload_matches_the_procedure_signature(self):
        """`OPEN_BKDT` takes 11 IN parameters, in this order."""
        from backdate.services.hana import build_payload

        req = make_request(self.requester)
        params = build_payload(req)

        self.assertEqual(len(params), 11)
        self.assertEqual(params[0], 'OIL')                    # BRANCH
        self.assertEqual(params[1], 'USER12')                 # USERID
        self.assertEqual(params[2], 13)                       # TRANSTYPE int
        self.assertEqual(params[3], req.from_date)            # FROMDATE date
        self.assertEqual(params[4], req.to_date)              # TODATE date
        self.assertEqual(params[5], req.time_limit)           # TIMELIMIT
        self.assertEqual(params[6], 'NO')                     # RIGHTS
        self.assertIsNone(params[9])                          # DELETEDBY
        self.assertIsNone(params[10])                         # DELETEDON
        # Real date objects, never `dd-MM-yyyy` strings — the JSAP round-trip
        # re-parsed them with the server's culture.
        self.assertIsInstance(params[3], datetime.date)
        self.assertIsInstance(params[4], datetime.date)


class HanaIdentityTests(_Base):
    """CREATEDBY and CREATEDON describe the REQUEST, never the approval.

    Verified against JSAP rather than assumed: `BackDateSaveInHana` builds its
    payload from `jsGetDocumentDetailUsingFlowId`, which returns
    `userDocument.createdBy` (the requester's numeric id) and the request's own
    `createdOn`. Live HANA rows carry `createdBy='92'` for requests raised by
    jsUser 92 while a different user did the approving.
    """

    def _approve_to_final(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        self.client_for(self.approver1).post(
            self.approve_url(req), {}, format='json')
        self.client_for(self.approver2).post(
            self.approve_url(req), {}, format='json')
        return req

    def test_createdby_is_the_requester_id_not_the_approver(self):
        from backdate.services.hana import build_payload

        req = make_request(self.requester)
        params = build_payload(req)

        self.assertEqual(params[7], str(self.requester.pk))
        for other in (self.approver1, self.approver2):
            self.assertNotEqual(params[7], str(other.pk))
            self.assertNotEqual(params[7], other.username)
        self.assertNotEqual(params[7], self.requester.username)

    def test_createdon_is_the_request_time_not_the_approval_time(self):
        from backdate.services.hana import build_payload

        req = self._approve_to_final()
        req.refresh_from_db()
        params = build_payload(req)

        self.assertEqual(params[8], req.created_at)
        decision = req.action_logs.filter(action=LogAction.APPROVE).last()
        self.assertGreater(decision.acted_at, params[8])

    def test_the_approver_is_still_recorded_where_it_belongs(self):
        """Dropping the approver from SAP must not lose them from OMS."""
        req = self._approve_to_final()
        approvers = list(
            req.action_logs.filter(action=LogAction.APPROVE)
            .order_by('acted_at', 'id')
            .values_list('acted_by__username', flat=True))
        self.assertEqual(approvers,
                         [self.approver1.username, self.approver2.username])

    def test_a_request_with_no_expiry_is_never_written_to_sap(self):
        """Belt and braces: the column refuses it, and so does the payload."""
        from backdate.services.hana import HanaWriteError, build_payload

        req = make_request(self.requester)
        req.time_limit = None
        with self.assertRaises(HanaWriteError) as caught:
            build_payload(req)
        self.assertIn('expiry', str(caught.exception))


class TimeLimitTests(_Base):
    """An expiry is required, because SAP ignores a grant without one."""

    def _body(self, **overrides):
        body = {
            'company': OIL, 'sap_username': 'USER12', 'document_type': 13,
            'from_date': '2026-05-01', 'to_date': '2026-05-20',
            'time_limit': '2026-12-31T18:30:00Z', 'action': 'A',
        }
        body.update(overrides)
        return {k: v for k, v in body.items() if v is not _OMIT}

    def test_a_missing_expiry_is_refused(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'),
            self._body(time_limit=_OMIT), format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('time_limit', response.data.get('errors', {}))

    def test_a_null_expiry_is_refused(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'),
            self._body(time_limit=None), format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_valid_expiry_is_accepted(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'), self._body(), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(
            BackDate.objects.get(pk=response.data['data']['id']).time_limit)

    def test_nothing_is_stored_when_the_expiry_is_missing(self):
        before = BackDate.objects.count()
        self.client_for(self.requester).post(
            reverse('backdate-request-list'),
            self._body(time_limit=_OMIT), format='json')
        self.assertEqual(BackDate.objects.count(), before)

    def test_an_expiry_already_past_is_still_refused(self):
        """The pre-existing rule, kept: `required` must not replace it."""
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'),
            self._body(time_limit='2020-01-01T10:00:00Z'), format='json')
        self.assertEqual(response.status_code, 400, response.data)


class OneRequestTests(_Base):
    """Several companies is ONE request, and N SAP calls.

    The business object is the decision, not the company: asking for the same
    rights in OIL and BEVERAGES is one thing to approve. JSAP stored it that
    way too (`branch = '1,2'`). The fan-out belongs to SAP, where `OPEN_BKDT`
    takes one branch per call — and `action` never fans out at all, because the
    procedure has no action parameter.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # An ALL-scoped workflow, because a request naming several companies is
        # not any one company's business. Its condition matches anything this
        # module stores, so multi-company requests route without ambiguity.
        cls.any_workflow = Workflow.objects.create(
            module=cls.module, code='BKDT_ANY', name='Any', company='ALL')
        query = WorkflowQuery.objects.create(
            workflow=cls.any_workflow, name='multi', company='ALL',
            query_text=(f"SELECT * FROM {DOC_TABLE} "
                        f"WHERE company LIKE '%,%'"))
        conditions.validate_and_stamp(query)
        WorkflowStage.objects.create(
            workflow=cls.any_workflow, name='Manager', sequence=1,
            user=cls.approver1)

    def _submit(self, company, action='A'):
        req = make_request(self.requester, company=company)
        req.action = action
        req.save(update_fields=['action'])
        flow_service.submit(req, user=self.requester)
        return req

    def _approve_fully(self, req):
        with mock.patch('backdate.services.hana.HANAConnection') as conn, \
                mock.patch('backdate.services.hana.Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            for _ in range(5):
                flow = BackDateFlow.objects.get(backdate=req)
                if flow.status != FlowStatus.PENDING:
                    break
                approver = User.objects.get(
                    pk=flow_service.effective_user_id(flow.current_stage_id))
                self.client_for(approver).post(
                    self.approve_url(req), {}, format='json')
            # One `execute` per company: that is the SAP fan-out.
            return conn.return_value.__enter__.return_value.execute.call_count

    def test_one_company_is_one_request_one_flow_one_call(self):
        req = self._submit(OIL)
        self.assertEqual(BackDate.objects.filter(pk=req.pk).count(), 1)
        self.assertEqual(BackDateFlow.objects.filter(backdate=req).count(), 1)
        self.assertEqual(self._approve_fully(req), 1)

    def test_two_companies_are_one_request_one_flow_two_calls(self):
        req = self._submit(f'{OIL},{BEVERAGES}', action='A,U')
        self.assertEqual(req.companies, [OIL, BEVERAGES])
        self.assertEqual(BackDateFlow.objects.filter(backdate=req).count(), 1)
        self.assertEqual(self._approve_fully(req), 2)

    def test_three_companies_are_one_request_one_flow_three_calls(self):
        req = self._submit(f'{OIL},{BEVERAGES},{MART}', action='A,U')
        self.assertEqual(BackDateFlow.objects.filter(backdate=req).count(), 1)
        self.assertEqual(self._approve_fully(req), 3)

    def test_both_actions_do_not_multiply_the_calls(self):
        """`A,U` across two companies is 2 calls, not 4."""
        both = self._submit(f'{OIL},{BEVERAGES}', action='A,U')
        self.assertEqual(self._approve_fully(both), 2)

    def test_the_action_never_reaches_the_sap_payload(self):
        """`OPEN_BKDT` has no action parameter; neither may the payload."""
        from backdate.services.hana import build_payload

        add = make_request(self.requester)
        upd = make_request(self.requester)
        upd.action = RequestAction.UPDATE
        upd.save(update_fields=['action'])

        self.assertEqual(build_payload(add), build_payload(upd))

    def test_the_api_accepts_a_list_of_companies(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'), {
                'company': [BEVERAGES, OIL], 'sap_username': 'USER12',
                'document_type': 13, 'from_date': '2026-05-01',
                'to_date': '2026-05-20', 'action': 'U,A',
                'time_limit': '2026-12-31T18:30:00Z',
            }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        obj = BackDate.objects.get(pk=response.data['data']['id'])
        # Canonical order, so one selection has one spelling.
        self.assertEqual(obj.company, f'{OIL},{BEVERAGES}')
        self.assertEqual(obj.action, 'A,U')
        self.assertEqual(BackDate.objects.filter(
            sap_username='USER12', created_at=obj.created_at).count(), 1)

    def test_an_unknown_company_is_refused(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'), {
                'company': [OIL, 'ATLANTIS'], 'sap_username': 'USER12',
                'document_type': 13, 'from_date': '2026-05-01',
                'to_date': '2026-05-20', 'action': 'A',
                'time_limit': '2026-12-31T18:30:00Z',
            }, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_the_company_filter_matches_membership(self):
        both = self._submit(f'{OIL},{BEVERAGES}')
        response = self.client_for(self.requester).get(
            reverse('backdate-request-list'), {'company': BEVERAGES})
        self.assertIn(both.pk, [row['id'] for row in response.data['data']])


class SapPayloadTests(_Base):
    """What was sent and what SAP said, kept per company."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.any_workflow = Workflow.objects.create(
            module=cls.module, code='BKDT_ANY', name='Any', company='ALL')
        query = WorkflowQuery.objects.create(
            workflow=cls.any_workflow, name='multi', company='ALL',
            query_text=(f"SELECT * FROM {DOC_TABLE} "
                        f"WHERE company LIKE '%,%'"))
        conditions.validate_and_stamp(query)
        WorkflowStage.objects.create(
            workflow=cls.any_workflow, name='Manager', sequence=1,
            user=cls.approver1)

    def _flow(self, company):
        req = make_request(self.requester, company=company)
        return flow_service.submit(req, user=self.requester)

    def test_the_payload_records_one_call_per_company(self):
        from backdate.services import hana

        flow = self._flow(f'{OIL},{BEVERAGES}')
        with mock.patch.object(hana, 'HANAConnection'), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        calls = flow.sap_payload['calls']
        self.assertEqual([c['branch'] for c in calls], [OIL, BEVERAGES])

    def test_the_payload_holds_the_actual_parameters(self):
        from backdate.services import hana

        flow = self._flow(OIL)
        with mock.patch.object(hana, 'HANAConnection'), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        params = flow.sap_payload['calls'][0]['parameters']
        self.assertEqual(sorted(params), sorted(hana.PARAMETER_NAMES))
        self.assertEqual(params['BRANCH'], OIL)
        self.assertEqual(params['USERID'], 'USER12')
        self.assertEqual(params['TRANSTYPE'], 13)
        self.assertEqual(params['RIGHTS'], 'NO')
        self.assertEqual(params['CREATEDBY'], str(self.requester.pk))
        self.assertEqual(params['CREATEDON'],
                         flow.backdate.created_at.isoformat())
        self.assertIsNotNone(params['TIMELIMIT'])
        self.assertIsNone(params['DELETEDBY'])
        self.assertIsNone(params['DELETEDON'])
        # There is no ACTION parameter, and none is invented.
        self.assertNotIn('ACTION', params)

    def test_the_payload_carries_no_connection_detail(self):
        from backdate.services import hana

        flow = self._flow(OIL)
        with mock.patch.object(hana, 'HANAConnection'), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        blob = json.dumps(flow.sap_payload).lower()
        for secret in ('password', 'schema', 'host', 'port', 'token',
                       'dsn', 'user=', 'pwd'):
            self.assertNotIn(secret, blob)

    def test_the_exact_sap_error_is_kept(self):
        from backdate.services import hana

        flow = self._flow(OIL)
        with mock.patch.object(
                hana, 'HANAConnection',
                side_effect=RuntimeError('(259, "invalid table name: BKDT")')):
            with mock.patch.object(hana, 'Queries') as queries:
                queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
                with self.assertRaises(hana.HanaWriteError):
                    hana.apply_grant(flow)

        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)
        results = json.loads(flow.hana_status_text)['results']
        # The DATABASE's own words, not an application summary.
        self.assertIn('invalid table name: BKDT', results[0]['response'])
        self.assertIn('RuntimeError', results[0]['response'])

    def test_a_partial_failure_says_which_company_failed(self):
        """OIL lands, BEVERAGES does not — and the record says so."""
        from backdate.services import hana

        flow = self._flow(f'{OIL},{BEVERAGES}')
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('SAP says no')
            return mock.MagicMock()

        with mock.patch.object(hana, 'HANAConnection', side_effect=flaky), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            with self.assertRaises(hana.HanaWriteError):
                hana.apply_grant(flow)

        flow.refresh_from_db()
        # One failure makes the REQUEST failed: the user does not have what
        # they asked for. There is no PARTIAL status.
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)
        results = json.loads(flow.hana_status_text)['results']
        self.assertEqual([(r['branch'], r['status']) for r in results],
                         [(OIL, 'SUCCESS'), (BEVERAGES, 'FAILED')])
        self.assertIn('SAP says no', results[1]['response'])
        # Both payloads are kept, so the successful one can be identified.
        self.assertEqual(len(flow.sap_payload['calls']), 2)

    def test_success_records_success_for_every_company(self):
        from backdate.services import hana

        flow = self._flow(f'{OIL},{BEVERAGES}')
        with mock.patch.object(hana, 'HANAConnection'), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.SUCCESS)
        results = json.loads(flow.hana_status_text)['results']
        self.assertEqual({r['status'] for r in results}, {'SUCCESS'})


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class HistoryReadTests(_Base):
    """The history endpoint resolves what it does not store."""

    def test_the_stage_name_is_resolved_not_stored(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        self.client_for(self.approver1).post(
            self.approve_url(req), {}, format='json')

        # Renaming the stage changes what history shows, because history holds
        # the id and reads the name.
        WorkflowStage.objects.filter(pk=self.stage1.pk).update(
            name='Renamed Manager')
        rows = self.client_for(self.requester).get(
            reverse('backdate-request-history', args=[req.pk])
        ).data['data']['actions']

        approve = next(r for r in rows if r['action'] == 'APPROVE')
        self.assertEqual(approve['stage_name'], 'Renamed Manager')
        self.assertEqual(approve['stage'], self.stage1.pk)

    def test_a_create_row_carries_no_stage(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        rows = self.client_for(self.requester).get(
            reverse('backdate-request-history', args=[req.pk])
        ).data['data']['actions']
        create = next(r for r in rows if r['action'] == 'CREATE')
        self.assertIsNone(create['stage'])
        self.assertEqual(create['stage_name'], '')

    def test_the_detail_reports_the_stage_and_total(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        data = self.client_for(self.requester).get(
            reverse('backdate-request-detail', args=[req.pk])).data['data']
        self.assertEqual(data['flow']['current_stage'], self.stage1.pk)
        self.assertEqual(data['flow']['current_stage_name'], 'Manager')
        self.assertEqual(data['flow']['total_stage'], 2)
        self.assertEqual(data['flow']['current_user_username'],
                         self.approver1.username)


class ScopingTests(_Base):
    def test_a_requester_sees_only_their_own_requests(self):
        mine = make_request(self.requester)
        flow_service.submit(mine, user=self.requester)
        theirs = make_request(self.outsider)
        flow_service.submit(theirs, user=self.outsider)

        response = self.client_for(self.requester).get(
            reverse('backdate-request-list'))
        ids = {row['id'] for row in response.data['data']}
        self.assertEqual(ids, {mine.pk})

    def test_the_request_page_is_refused_without_the_key(self):
        nobody = make_user('bk-nobody')
        response = self.client_for(nobody).get(reverse('backdate-request-list'))
        self.assertEqual(response.status_code, 403)

    def test_the_approval_desk_is_refused_without_the_key(self):
        response = self.client_for(self.requester).get(
            reverse('backdate-approval-queue'))
        self.assertEqual(response.status_code, 403)


class CompanyFilterTests(_Base):
    """`?company=` on the four list/count endpoints."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bev_workflow = Workflow.objects.create(
            module=cls.module, code='BKDT_BEV', name='BEV', company=BEVERAGES)
        bev_query = WorkflowQuery.objects.create(
            workflow=cls.bev_workflow, name='bev', company=BEVERAGES,
            query_text=(f"SELECT * FROM {DOC_TABLE} "
                        f"WHERE ',' || company || ',' LIKE '%,BEVERAGES,%'"))
        conditions.validate_and_stamp(bev_query)
        WorkflowStage.objects.create(
            workflow=cls.bev_workflow, name='Manager', sequence=1,
            user=cls.approver1)

    def setUp(self):
        self.oil = make_request(self.requester)
        flow_service.submit(self.oil, user=self.requester)
        self.bev = make_request(self.requester, company=BEVERAGES)
        flow_service.submit(self.bev, user=self.requester)
        self.client = self.client_for(self.requester)

    def test_the_request_list_is_narrowed(self):
        response = self.client.get(
            reverse('backdate-request-list'), {'company': BEVERAGES})
        self.assertEqual([row['id'] for row in response.data['data']],
                         [self.bev.pk])

    def test_the_counts_follow_the_same_filter(self):
        """Otherwise the card says 2 over a table showing 1."""
        response = self.client.get(reverse('backdate-insights'),
                                   {'company': BEVERAGES})
        self.assertEqual(response.data['data']['total'], 1)
        self.assertEqual(
            self.client.get(reverse('backdate-insights')).data['data']['total'],
            2)

    def test_an_unknown_company_is_refused_not_ignored(self):
        for name in ('backdate-request-list', 'backdate-insights'):
            response = self.client.get(reverse(name), {'company': 'OILL'})
            self.assertEqual(response.status_code, 400, name)

    def test_the_filter_is_case_insensitive(self):
        response = self.client.get(
            reverse('backdate-request-list'), {'company': 'beverages'})
        self.assertEqual([row['id'] for row in response.data['data']],
                         [self.bev.pk])

    def test_the_approval_queue_and_its_counts_are_narrowed(self):
        approver = self.client_for(self.approver1)
        queue = approver.get(reverse('backdate-approval-queue'),
                             {'company': BEVERAGES})
        self.assertEqual([row['id'] for row in queue.data['data']],
                         [self.bev.pk])
        counts = approver.get(reverse('backdate-approval-insights'),
                              {'company': BEVERAGES}).data['data']
        self.assertEqual((counts['pending'], counts['total']), (1, 1))


class ApprovalInsightsTests(_Base):
    """The desk's KPI counts: pending is the queue, the rest is history."""

    def setUp(self):
        self.req = make_request(self.requester)
        self.flow = flow_service.submit(self.req, user=self.requester)

    def test_a_waiting_request_counts_as_pending_for_its_approver_only(self):
        counts = self.client_for(self.approver1).get(
            reverse('backdate-approval-insights')).data['data']
        self.assertEqual(counts, {'pending': 1, 'approved': 0, 'rejected': 0,
                                  'total': 1})
        other = self.client_for(self.approver2).get(
            reverse('backdate-approval-insights')).data['data']
        self.assertEqual(other['total'], 0)

    def test_a_decision_moves_the_count_from_pending_to_decided(self):
        self.client_for(self.approver1).post(
            self.approve_url(self.req), {}, format='json')
        counts = self.client_for(self.approver1).get(
            reverse('backdate-approval-insights')).data['data']
        # Approved at stage 1 of two: the flow is still pending, so it is not
        # yet counted as approved, and it no longer waits on this user.
        self.assertEqual(counts, {'pending': 0, 'approved': 0, 'rejected': 0,
                                  'total': 0})

    def test_the_desk_counts_are_refused_without_the_key(self):
        response = self.client_for(self.requester).get(
            reverse('backdate-approval-insights'))
        self.assertEqual(response.status_code, 403)


class RemovedEndpointTests(_Base):
    """Paths JSAP exposed that must not exist here."""

    def test_there_is_no_unapproved_path_to_sap(self):
        from django.urls.exceptions import NoReverseMatch
        for gone in ('backdate-save-bkdt', 'backdate-update-hana-status',
                     'backdate-task-approve', 'backdate-task-reject'):
            with self.assertRaises(NoReverseMatch):
                reverse(gone)

    def test_retry_refuses_a_request_that_is_not_approved(self):
        req = make_request(self.requester)
        flow_service.submit(req, user=self.requester)
        response = self.client_for(self.approver1).post(
            reverse('backdate-retry-hana', args=[req.pk]), {}, format='json')
        self.assertEqual(response.status_code, 409)
