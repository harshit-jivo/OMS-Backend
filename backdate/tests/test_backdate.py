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
        document_type_name='A/R Invoice',
        from_date=datetime.date(2026, 5, 1),
        to_date=to_date,
        time_limit=time_limit,
        action=RequestAction.ADD,
        created_by=creator,
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

    def test_the_request_holds_one_document_identity_and_no_remarks(self):
        columns = self._columns('backdate')
        self.assertEqual(
            columns,
            {'id', 'company', 'sap_username', 'document_type_name',
             'from_date', 'to_date', 'time_limit', 'action',
             'created_by_id', 'created_at', 'updated_at'})
        # `document_type` held the same fact as `document_type_name` in a
        # second spelling; SAP's number is resolved from the name at call
        # time. `remarks` could only ever hold the LAST of four different
        # people's statements — each is a row in the action log instead.
        for banned in ('document_type', 'remarks', 'document_name',
                       'object_type', 'reason'):
            self.assertNotIn(banned, columns)

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
                'document_type_name': 'A/R Invoice', 'from_date': '2026-05-01',
                'to_date': '2026-05-20', 'action': 'A',
                'time_limit': '2026-12-31T18:30:00Z',
            }, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(BackDate.objects.count(), before)

    def test_created_by_comes_from_the_session_not_the_payload(self):
        response = self.client_for(self.requester).post(
            reverse('backdate-request-list'), {
                'company': OIL, 'sap_username': 'USER12', 'document_type_name': 'A/R Invoice',
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
            {'sap_username': 'USER99', 'document_type_name': 'Delivery',
             'remarks': 'corrected'},
            format='json')
        log = self.req.action_logs.get(action=LogAction.UPDATE)
        # `remarks` is NOT one of them: it is the log row's own reason, not a
        # field of the request, so recording it as a changed value would state
        # the same sentence twice.
        self.assertEqual(set(log.action_data),
                         {'sap_username', 'document_type_name'})
        self.assertEqual(log.action_data['sap_username'],
                         {'old': 'USER12', 'new': 'USER99'})
        self.assertEqual(log.action_data['document_type_name'],
                         {'old': 'A/R Invoice', 'new': 'Delivery'})
        self.assertEqual(log.remarks, 'corrected')

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

    def test_can_edit_says_what_the_endpoint_will_do(self):
        """The UI offers Edit off this flag, so it must not disagree."""
        response = self.client_for(self.requester).get(self.url)
        self.assertTrue(response.data['data']['can_edit'])

        self.client_for(self.approver1).post(
            self.approve_url(self.req), {}, format='json')
        response = self.client_for(self.requester).get(self.url)
        self.assertFalse(response.data['data']['can_edit'])
        # And the endpoint agrees — one rule, asked twice.
        self.assertEqual(
            self.client_for(self.requester).patch(
                self.url, {'remarks': 'too late'}, format='json').status_code,
            409)

    def test_can_edit_is_false_for_somebody_else(self):
        response = self.client_for(self.approver1).get(self.url)
        # An approver may READ what they are deciding, and may not edit it.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['data']['can_edit'])


class SapRefusedEditTests(_Base):
    """SAP refuses on the last stage: nothing is approved, and it is fixable.

    That is the point of calling SAP first. The error is almost always a value
    SAP would not take, and the person reading it is the approver holding the
    request — so they may correct it and approve again rather than relay it.
    """

    def setUp(self):
        self.req = make_request(self.requester)
        self.flow = flow_service.submit(self.req, user=self.requester)
        self.url = reverse('backdate-request-detail', args=[self.req.pk])
        # Walk to the last stage, then have SAP refuse it.
        self.client_for(self.approver1).post(
            reverse('backdate-request-approve', args=[self.req.pk]),
            {}, format='json')
        from backdate.services.hana import HanaWriteError
        with mock.patch('backdate.services.hana.apply_grant',
                        side_effect=HanaWriteError(
                            'SAP refused the rights.',
                            payload={'calls': []},
                            status_text='{"results": []}')):
            self.response = self.client_for(self.approver2).post(
                reverse('backdate-request-approve', args=[self.req.pk]),
                {}, format='json')

    def test_nothing_was_approved(self):
        self.assertEqual(self.response.status_code, 502)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.status, FlowStatus.PENDING)
        self.assertEqual(self.flow.hana_status, HanaStatus.FAILED)

    def test_the_holding_approver_may_now_edit(self):
        response = self.client_for(self.approver2).patch(
            self.url, {'sap_username': 'FIXED'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.req.refresh_from_db()
        self.assertEqual(self.req.sap_username, 'FIXED')

    def test_the_fix_is_logged_as_an_update(self):
        self.client_for(self.approver2).patch(
            self.url, {'sap_username': 'FIXED'}, format='json')
        log = self.req.action_logs.filter(action=LogAction.UPDATE).last()
        self.assertIsNotNone(log)
        self.assertEqual(log.acted_by_id, self.approver2.pk)
        self.assertEqual(log.action_data['sap_username']['new'], 'FIXED')

    def test_an_approver_who_is_NOT_holding_it_still_cannot_edit(self):
        response = self.client_for(self.approver1).patch(
            self.url, {'sap_username': 'NOPE'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_can_edit_is_true_for_the_holder_and_false_for_the_rest(self):
        self.assertTrue(
            self.client_for(self.approver2).get(self.url)
            .data['data']['can_edit'])
        self.assertFalse(
            self.client_for(self.approver1).get(self.url)
            .data['data']['can_edit'])

    def test_history_is_append_only_across_create_update_approve(self):
        self.client_for(self.requester).patch(
            self.url, {'sap_username': 'USER99', 'remarks': 'revised'},
            format='json')
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
                         {'sap_username': {'old': 'USER12',
                                           'new': 'USER99'}})
        self.assertEqual(logs[1].remarks, 'revised')
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
    def test_a_refused_sap_write_approves_nothing(self, apply_grant):
        """JSAP returned 200/Success=true here even with no SAP write.

        SAP is the gate on the last stage now, so a refusal is a refusal: the
        request stays exactly where it was, and the approver is told so.
        """
        from backdate.services.hana import HanaWriteError
        apply_grant.side_effect = HanaWriteError(
            'SAP refused the rights.',
            payload={'calls': []}, status_text='{"results": []}')

        req, response = self._approve_to_final()
        self.assertEqual(response.status_code, 502)
        flow = BackDateFlow.objects.get(backdate=req)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertIsNotNone(flow.current_stage_id)

    @mock.patch('backdate.services.hana.apply_grant')
    def test_a_refused_sap_write_is_still_recorded(self, apply_grant):
        """The approver is told to correct it, so they must be able to see WHY."""
        from backdate.services.hana import HanaWriteError
        apply_grant.side_effect = HanaWriteError(
            'SAP refused the rights.',
            payload={'calls': [{'branch': OIL}]},
            status_text='{"results": [{"response": "invalid userid"}]}')

        req, _ = self._approve_to_final()
        flow = BackDateFlow.objects.get(backdate=req)
        # Written AFTER the approval transaction rolled back, so it survives.
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)
        self.assertIn('invalid userid', flow.hana_status_text)
        self.assertEqual(flow.sap_payload, {'calls': [{'branch': OIL}]})

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
            with self.assertRaises(hana.HanaWriteError) as caught:
                hana.apply_grant(flow)
        # `apply_grant` raises WITHOUT writing: it runs inside the approval's
        # own transaction, which is about to be rolled back. The handler stores
        # the evidence once that transaction is gone.
        hana.record_failure(flow, caught.exception)
        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)
        self.assertTrue(flow.hana_status_text)

    def test_a_refusal_carries_what_was_sent_and_what_sap_said(self):
        """The evidence rides on the exception, not on a rolled-back row."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        from backdate.services import hana
        with mock.patch.object(hana, 'HANAConnection',
                               side_effect=RuntimeError('down')):
            with self.assertRaises(hana.HanaWriteError) as caught:
                hana.apply_grant(flow)

        error = caught.exception
        self.assertEqual([c['branch'] for c in error.payload['calls']], [OIL])
        self.assertIn('down', error.status_text)
        # And nothing was written behind the caller's back.
        flow.refresh_from_db()
        self.assertNotEqual(flow.hana_status, HanaStatus.FAILED)

    def test_the_sap_outcome_is_not_duplicated_into_the_log(self):
        """Two copies of one fact is how the two come to disagree."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        from backdate.services import hana
        with mock.patch.object(hana, 'HANAConnection',
                               side_effect=RuntimeError('down')):
            with self.assertRaises(hana.HanaWriteError) as caught:
                hana.apply_grant(flow)
        hana.record_failure(flow, caught.exception)
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
            'company': OIL, 'sap_username': 'USER12', 'document_type_name': 'A/R Invoice',
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


class CompanySetTests(_Base):
    """A request may name several companies. It is still ONE request.

    The rights asked for are the same rights in each named SAP database,
    decided once by the same approvers — so one row, one flow, one workflow,
    one stage chain, one history. Only the SAP write fans out.

    This replaces `OneCompanyTests`, which asserted the opposite. The reason
    that rule existed was real: JSAP accepted a comma-separated branch list
    and looped the SAP writes keeping no per-branch record, so a mid-loop
    failure granted rights in one company, not another, and reported success.
    The answer here is to record every call and every answer — see
    `PartialSapFailureTests` — rather than to forbid the request.

    `action` stays combined for a different reason: `OPEN_BKDT` has no action
    parameter, so splitting `A,U` would write SAP rows identical in every
    column SAP reads.
    """

    def _create(self, company, action='A'):
        return self.client_for(self.requester).post(
            reverse('backdate-request-list'), {
                'company': company, 'sap_username': 'USER12',
                'document_type_name': 'A/R Invoice', 'from_date': '2026-05-01',
                'to_date': '2026-05-20', 'action': action,
                'time_limit': '2026-12-31T18:30:00Z',
            }, format='json')

    # --- TEST 1 / 2 / 3: one, two and three companies ------------------

    def test_one_company_still_works_exactly_as_before(self):
        response = self._create([OIL])
        self.assertEqual(response.status_code, 201, response.data)
        obj = BackDate.objects.get(pk=response.data['data']['id'])
        self.assertEqual(obj.company, OIL)
        self.assertEqual(obj.companies, [OIL])

    def test_two_companies_make_one_request(self):
        response = self._create([OIL, MART], action='U,A')
        self.assertEqual(response.status_code, 201, response.data)
        obj = BackDate.objects.get(pk=response.data['data']['id'])
        self.assertEqual(obj.company, 'OIL,MART')
        self.assertEqual(obj.companies, [OIL, MART])
        # Both actions on the ONE request, normalised.
        self.assertEqual(obj.action, 'A,U')
        # ONE row, not one per company.
        self.assertEqual(
            BackDate.objects.filter(sap_username='USER12',
                                    from_date='2026-05-01').count(), 1)

    def test_three_companies_make_one_request(self):
        response = self._create([MART, OIL, BEVERAGES])
        self.assertEqual(response.status_code, 201, response.data)
        obj = BackDate.objects.get(pk=response.data['data']['id'])
        self.assertEqual(obj.company, 'OIL,BEVERAGES,MART')
        self.assertEqual(obj.companies, [OIL, BEVERAGES, MART])

    # --- canonical form ------------------------------------------------

    def test_the_stored_set_is_canonical_however_it_was_sent(self):
        """One set, one spelling. `MART,OIL` and `OIL,MART` are one request."""
        for sent in ([MART, OIL], 'MART,OIL', 'mart, oil', [OIL, MART, OIL]):
            with self.subTest(sent=sent):
                response = self._create(sent)
                self.assertEqual(response.status_code, 201, response.data)
                obj = BackDate.objects.get(pk=response.data['data']['id'])
                self.assertEqual(obj.company, 'OIL,MART')

    def test_no_company_is_refused(self):
        for empty in ([], '', '   '):
            with self.subTest(empty=empty):
                self.assertEqual(self._create(empty).status_code, 400)

    def test_an_unknown_company_is_refused_not_dropped(self):
        response = self._create([OIL, 'ATLANTIS'])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('ATLANTIS', str(response.data))

    def test_the_label_reads_as_a_list_of_names(self):
        req = make_request(self.requester)
        req.company = 'OIL,MART'
        req.save(update_fields=['company'])
        self.assertEqual(req.company_label, 'Oil, Mart')

    # --- TEST 5: the API shape a screen renders ------------------------

    def test_the_api_offers_the_set_three_ways_so_no_screen_splits_it(self):
        response = self._create([OIL, MART])
        data = response.data['data']
        self.assertEqual(data['company'], 'OIL,MART')
        self.assertEqual(data['companies'], [OIL, MART])
        self.assertEqual(data['company_label'], 'Oil, Mart')

    # --- TEST 9: one history, not one per company ----------------------

    def test_one_create_log_however_many_companies(self):
        response = self._create([OIL, BEVERAGES, MART])
        obj = BackDate.objects.get(pk=response.data['data']['id'])
        self.assertEqual(
            list(obj.action_logs.values_list('action', flat=True)),
            [LogAction.CREATE])

    # --- TEST 4: one flow, one workflow, one approval per stage -------

    def test_one_flow_and_one_approval_per_stage(self):
        req = make_request(self.requester)
        req.company = 'OIL,MART'
        req.save(update_fields=['company'])
        flow = flow_service.submit(req, user=self.requester)

        self.assertEqual(BackDateFlow.objects.filter(backdate=req).count(), 1)
        self.assertIsNotNone(flow.workflow_id)

        flow_service.approve(flow, user=self.approver1)
        stages = list(req.action_logs.filter(action=LogAction.APPROVE)
                      .values_list('stage_id', flat=True))
        self.assertEqual(stages, [self.stage1.pk], stages)

    # --- the database's own opinion ------------------------------------

    def test_the_database_refuses_a_non_canonical_set(self):
        """The CHECK enumerates the legal sets, so a bug cannot write a
        spelling the application would never produce."""
        from django.db import IntegrityError, transaction

        for illegal in ['MART,OIL', 'OIL,OIL', 'OIL,ATLANTIS', 'OIL,']:
            with self.subTest(illegal=illegal), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    BackDate.objects.create(
                        company=illegal, sap_username='USER12',
                        document_type_name='A/R Invoice',
                        from_date=datetime.date(2026, 5, 1),
                        to_date=datetime.date(2026, 5, 20),
                        time_limit=TIME_LIMIT, action=RequestAction.ADD,
                        created_by=self.requester)

    # --- TEST 7: one SAP call per company, same details ---------------

    def test_the_action_never_reaches_the_sap_payload(self):
        """`OPEN_BKDT` has no action parameter; neither may the payload."""
        from backdate.services.hana import build_payload

        add = make_request(self.requester)
        upd = make_request(self.requester)
        upd.action = 'A,U'
        upd.save(update_fields=['action'])

        self.assertEqual(build_payload(add), build_payload(upd))

    def test_the_sap_call_count_follows_the_companies(self):
        from backdate.services import hana

        for companies, expected in [('OIL', 1), ('OIL,MART', 2),
                                    ('OIL,BEVERAGES,MART', 3)]:
            with self.subTest(companies=companies):
                req = make_request(self.requester)
                req.company = companies
                req.save(update_fields=['company'])
                flow = flow_service.submit(req, user=self.requester)
                self.assertEqual(self._sap_calls(hana, flow), expected)

    def test_every_sap_call_carries_the_same_details_but_its_own_branch(self):
        """Only the company's SAP context changes — §10 of the requirement."""
        from backdate.services.hana import build_payload

        req = make_request(self.requester)
        req.company = 'OIL,MART'
        req.save(update_fields=['company'])

        payloads = {c: build_payload(req, c) for c in req.companies}
        self.assertEqual(len(payloads), 2)
        # BRANCH (parameter 1) differs; the other ten are identical.
        differing = {i for i in range(11)
                     if len({p[i] for p in payloads.values()}) > 1}
        self.assertEqual(differing, {0}, differing)

    def test_a_company_not_on_the_request_cannot_be_written_to(self):
        from backdate.services.hana import HanaWriteError, build_payload

        req = make_request(self.requester)
        req.company = 'OIL'
        req.save(update_fields=['company'])
        with self.assertRaises(HanaWriteError):
            build_payload(req, MART)

    def _sap_calls(self, hana, flow):
        with mock.patch.object(hana, 'HANAConnection') as conn, \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)
            # One connection per company — `_stamp_request_id` reuses it.
            return conn.return_value.__enter__.call_count


class PartialSapFailureTests(_Base):
    """TEST 8 — one company lands, another does not.

    THIS IS THE CASE THE OLD ONE-COMPANY RULE EXISTED TO PREVENT, and the
    reason it can be allowed now. SAP gives no cross-schema transaction, so a
    partial outcome is possible and must be RECORDED rather than smoothed
    over: the requester does not have what they asked for until every company
    has it, and an administrator needs to know exactly which one is missing.
    """

    def _apply(self, flow, failing):
        from backdate.services import hana

        def schema_for(company):
            if company == failing:
                raise hana.HanaSchemaError(f'no schema for {company}')
            return f'TEST_{company}'

        with mock.patch.object(hana, 'HANAConnection'), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = schema_for
            try:
                hana.apply_grant(flow)
            except hana.HanaWriteError:
                pass
        flow.refresh_from_db()
        return flow

    def _multi_flow(self):
        req = make_request(self.requester)
        req.company = 'OIL,MART'
        req.save(update_fields=['company'])
        return flow_service.submit(req, user=self.requester)

    def test_one_failure_makes_the_whole_request_failed(self):
        flow = self._apply(self._multi_flow(), failing=MART)
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)

    def test_both_companies_answers_are_kept(self):
        """The success must not be lost because the other one failed."""
        flow = self._apply(self._multi_flow(), failing=MART)
        results = json.loads(flow.hana_status_text)
        by_branch = {r['branch']: r['status'] for r in results}
        self.assertEqual(len(by_branch), 2, results)
        self.assertEqual(by_branch[OIL], 'SUCCESS', results)
        self.assertEqual(by_branch[MART], 'FAILED', results)

    def test_the_payload_of_every_company_is_kept(self):
        flow = self._apply(self._multi_flow(), failing=MART)
        branches = [c['branch'] for c in flow.sap_payload['calls']]
        self.assertEqual(sorted(branches), sorted([OIL, MART]), branches)

    def test_a_partial_failure_creates_no_second_request_or_flow(self):
        flow = self._apply(self._multi_flow(), failing=MART)
        self.assertEqual(
            BackDate.objects.filter(pk=flow.backdate_id).count(), 1)
        self.assertEqual(
            BackDateFlow.objects.filter(backdate_id=flow.backdate_id).count(),
            1)


class SapPayloadTests(_Base):
    """What was sent and what SAP said, kept per company."""

    def _flow(self, company=OIL):
        req = make_request(self.requester, company=company)
        return flow_service.submit(req, user=self.requester)

    def test_the_payload_records_the_one_call_that_was_made(self):
        from backdate.services import hana

        flow = self._flow(BEVERAGES)
        with mock.patch.object(hana, 'HANAConnection'), \
                mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        calls = flow.sap_payload['calls']
        # One company per request, so one call — never a branch loop.
        self.assertEqual([c['branch'] for c in calls], [BEVERAGES])

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
                with self.assertRaises(hana.HanaWriteError) as caught:
                    hana.apply_grant(flow)
        hana.record_failure(flow, caught.exception)

        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.FAILED)
        results = json.loads(flow.hana_status_text)['results']
        # The DATABASE's own words, not an application summary.
        self.assertIn('invalid table name: BKDT', results[0]['response'])
        self.assertIn('RuntimeError', results[0]['response'])

    def test_the_expiry_is_sent_on_saps_own_clock(self):
        """SAP compares timeLimit against ITS clock, which runs at UTC+05:30.

        Sending UTC made every grant lapse 5h30m early — an approved request
        that stops working before the user thinks it has. JSAP has always
        written local time here; its own record and the HANA row it produced
        are identical to the second on live rows.
        """
        import datetime
        import zoneinfo
        from backdate.services.hana import build_payload, to_sap_clock

        ist = zoneinfo.ZoneInfo('Asia/Kolkata')
        req = make_request(self.requester)
        # 13:00 UTC is 18:30 in Indian time.
        req.time_limit = datetime.datetime(
            2026, 12, 31, 13, 0, tzinfo=datetime.timezone.utc)
        req.save(update_fields=['time_limit'])

        params = build_payload(req)
        expiry = params[5]
        self.assertEqual(
            expiry,
            req.time_limit.astimezone(ist).replace(tzinfo=None))
        self.assertEqual((expiry.hour, expiry.minute), (18, 30))
        # Naive on the way out: BKDT."timeLimit" carries no zone.
        self.assertIsNone(expiry.tzinfo)

        # createdOn moves with it, so the two read consistently.
        self.assertEqual(
            params[8],
            req.created_at.astimezone(ist).replace(tzinfo=None))

    def test_a_naive_timestamp_is_left_alone(self):
        """It is already somebody's local time; guessing whose is worse."""
        import datetime
        from backdate.services.hana import to_sap_clock

        naive = datetime.datetime(2026, 12, 31, 18, 30)
        self.assertEqual(to_sap_clock(naive), naive)
        self.assertIsNone(to_sap_clock(None))

    def test_the_sap_row_is_tagged_with_the_request_id(self):
        """`OPEN_BKDT` leaves `id` NULL, so nothing ties a row to a request."""
        from backdate.services import hana
        from backdate.services.hana import build_payload

        flow = self._flow(OIL)
        with mock.patch.object(hana, 'HANAConnection') as conn,                 mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)
            calls = conn.return_value.__enter__.return_value.execute.call_args_list

        # Two statements: the grant, then the label.
        self.assertEqual(len(calls), 2)
        self.assertIn('OPEN_BKDT', calls[0].args[0])
        sql, params = calls[1].args[0], calls[1].args[1]
        self.assertIn('UPDATE', sql)
        self.assertIn('"id" = ?', sql)
        # Only ever fills a blank — it cannot overwrite somebody else's value.
        self.assertIn('"id" IS NULL', sql)
        self.assertEqual(params[0], flow.backdate_id)

        # IT MUST MATCH THE WHOLE ROW. ("userid", "createdOn") alone is not
        # unique in BKDT: JSAP truncates createdOn to the whole second and
        # live OIL already holds 44 duplicated pairs, so a narrower match
        # could put this request's id on somebody else's row.
        for column in ('"branch"', '"userid"', '"transtype"', '"fromDate"',
                       '"toDate"', '"timeLimit"', '"rights"', '"createdBy"',
                       '"createdOn"'):
            self.assertIn(f'{column} = ?', sql, column)
        # Every one of them bound, in the order OPEN_BKDT was given them.
        self.assertEqual(params[1:], list(build_payload(flow.backdate)[:9]))

    def test_a_tagged_row_reports_its_id_as_a_field(self):
        """The page shows it as "SAP row id", not buried in a sentence."""
        from backdate.services import hana

        hana._TAGGING_REFUSED.clear()
        self.addCleanup(hana._TAGGING_REFUSED.clear)

        flow = self._flow(OIL)
        with mock.patch.object(hana, 'HANAConnection'),                 mock.patch.object(hana, 'Queries') as queries:
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        result = json.loads(flow.hana_status_text)['results'][0]
        self.assertEqual(result['sap_row_id'], flow.backdate_id)
        self.assertIn('OPEN_BKDT accepted', result['response'])
        self.assertNotIn('SELECT', result['response'])

    def test_a_schema_that_refuses_the_tag_is_not_retried(self):
        """Live BEVERAGES will never carry UPDATE — that grant is refused.

        Without this, every BEVERAGES approval would run a statement that
        cannot succeed and log a warning about a settled fact.
        """
        from backdate.services import hana

        hana._TAGGING_REFUSED.clear()
        self.addCleanup(hana._TAGGING_REFUSED.clear)

        def denied(sql, params=None):
            if str(sql).lstrip().upper().startswith('UPDATE'):
                raise RuntimeError('insufficient privilege')
            return []

        for attempt in (1, 2):
            flow = self._flow(BEVERAGES)
            with mock.patch.object(hana, 'HANAConnection') as conn,                     mock.patch.object(hana, 'Queries') as queries:
                queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
                conn.return_value.__enter__.return_value.execute.side_effect =                     denied
                hana.apply_grant(flow)
                statements = [
                    str(c.args[0]) for c in
                    conn.return_value.__enter__.return_value
                    .execute.call_args_list]

            if attempt == 1:
                # Tried once, refused, remembered.
                self.assertEqual(len(statements), 2)
                self.assertIn(f'TEST_{BEVERAGES}', hana._TAGGING_REFUSED)
            else:
                # Second time it does not even try — only the grant runs.
                self.assertEqual(len(statements), 1)
                self.assertIn('OPEN_BKDT', statements[0])

            flow.refresh_from_db()
            self.assertEqual(flow.hana_status, HanaStatus.SUCCESS)

    def test_an_untagged_row_says_so_in_a_field_not_a_paragraph(self):
        """A row that could not be tagged still reads as a plain success."""
        from backdate.services import hana

        hana._TAGGING_REFUSED.clear()
        self.addCleanup(hana._TAGGING_REFUSED.clear)

        flow = self._flow(OIL)
        with mock.patch.object(hana, 'HANAConnection'),                 mock.patch.object(hana, 'Queries') as queries,                 mock.patch.object(hana, '_stamp_request_id',
                                  return_value=False):
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            hana.apply_grant(flow)

        flow.refresh_from_db()
        result = json.loads(flow.hana_status_text)['results'][0]
        # ONE sentence — what SAP accepted. No SQL, no prose about tagging:
        # a query pasted into the middle of it buried the only thing an
        # approver is reading for.
        self.assertIn('OPEN_BKDT accepted', result['response'])
        self.assertNotIn('SELECT', result['response'])
        self.assertNotIn('WHERE', result['response'])
        # Where the row is, is a FIELD. None says "not tagged" without a
        # paragraph explaining it.
        self.assertIsNone(result['sap_row_id'])

    def test_a_failure_to_tag_never_unsays_a_real_grant(self):
        """Past the CALL the rights EXIST. Labelling must not undo that."""
        from backdate.services import hana

        flow = self._flow(OIL)
        with mock.patch.object(hana, 'HANAConnection'),                 mock.patch.object(hana, 'Queries') as queries,                 mock.patch.object(hana, '_stamp_request_id',
                                  side_effect=RuntimeError('no UPDATE right')):
            queries._schema_for_branch.side_effect = lambda c: f'TEST_{c}'
            # No raise: the approval stands.
            hana.apply_grant(flow)

        flow.refresh_from_db()
        self.assertEqual(flow.hana_status, HanaStatus.SUCCESS)
        results = json.loads(flow.hana_status_text)['results']
        # And it does not claim a tag it does not have.
        self.assertIsNone(results[0]['sap_row_id'])

    def test_success_records_success(self):
        from backdate.services import hana

        flow = self._flow()
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


class ApproverReadAccessTests(_Base):
    """An approver can open what they are deciding.

    They hold `BackDate_Approval` and usually NOT `BackDate`, because they
    never raise a request of their own. Gating the detail and the history on
    the requester key left them approving documents they could not look at —
    the dialog showed "You do not have permission for this action" where the
    approval history should have been.
    """

    def setUp(self):
        self.req = make_request(self.requester)
        flow_service.submit(self.req, user=self.requester)
        self.approver_only = make_user('bk-approver-only', 'BackDate_Approval')

    def test_an_approver_without_the_requester_key_can_read_the_detail(self):
        response = self.client_for(self.approver_only).get(
            reverse('backdate-request-detail', args=[self.req.pk]))
        self.assertEqual(response.status_code, 200, response.data)

    def test_an_approver_without_the_requester_key_can_read_the_history(self):
        response = self.client_for(self.approver_only).get(
            reverse('backdate-request-history', args=[self.req.pk]))
        self.assertEqual(response.status_code, 200, response.data)

    def test_reading_is_still_refused_without_either_key(self):
        nobody = make_user('bk-reads-nothing')
        for name in ('backdate-request-detail', 'backdate-request-history'):
            response = self.client_for(nobody).get(
                reverse(name, args=[self.req.pk]))
            self.assertEqual(response.status_code, 403, name)

    def test_a_requester_still_cannot_read_somebody_elses(self):
        other = make_user('bk-other-requester', 'BackDate')
        response = self.client_for(other).get(
            reverse('backdate-request-detail', args=[self.req.pk]))
        self.assertEqual(response.status_code, 404)

    def test_an_approver_who_is_not_reviewing_it_cannot_edit(self):
        """Holding the key is not holding the request."""
        response = self.client_for(self.approver_only).patch(
            reverse('backdate-request-detail', args=[self.req.pk]),
            {'remarks': 'not mine to change'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_the_approver_reviewing_it_MAY_edit(self):
        """They are the one who can see what is wrong with it.

        A document type SAP will not take, or a window off by a day, used to
        mean a rejection and a re-raise. Now it is a correction, recorded
        against the approver who made it.
        """
        url = reverse('backdate-request-detail', args=[self.req.pk])
        response = self.client_for(self.approver1).patch(
            url, {'sap_username': 'USER99', 'remarks': 'corrected'},
            format='json')
        self.assertEqual(response.status_code, 200, response.data)

        self.req.refresh_from_db()
        self.assertEqual(self.req.sap_username, 'USER99')
        log = self.req.action_logs.filter(action=LogAction.UPDATE).last()
        # Attributed to the APPROVER, never to the requester.
        self.assertEqual(log.acted_by_id, self.approver1.pk)
        self.assertEqual(log.remarks, 'corrected')

    def test_a_later_approver_cannot_rewrite_what_an_earlier_one_agreed_to(self):
        url = reverse('backdate-request-detail', args=[self.req.pk])
        self.client_for(self.approver1).post(
            self.approve_url(self.req), {}, format='json')

        # Stage 2 now holds it, but stage 1 has already said yes to the
        # request AS IT READ THEN.
        response = self.client_for(self.approver2).patch(
            url, {'sap_username': 'TOOLATE'}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(
            self.client_for(self.approver2).get(url).data['data']['can_edit'])


class ProgressTests(_Base):
    """The history endpoint also answers "how far has this got?".

    The action log alone cannot: it records what HAPPENED, so a request
    waiting at stage 1 of 2 has one row and says nothing about the stage
    ahead. The stages come from the engine and are joined to the log.
    """

    def setUp(self):
        self.req = make_request(self.requester)
        flow_service.submit(self.req, user=self.requester)

    def _stages(self, user=None):
        response = self.client_for(user or self.requester).get(
            reverse('backdate-request-history', args=[self.req.pk]))
        return response.data['data']['stages']

    def test_every_stage_is_listed_including_the_ones_ahead(self):
        stages = self._stages()
        self.assertEqual([s['sequence'] for s in stages], [1, 2])
        self.assertEqual([s['status'] for s in stages],
                         ['AWAITING', 'UPCOMING'])

    def test_a_decided_stage_reports_who_acted_and_when(self):
        self.client_for(self.approver1).post(
            self.approve_url(self.req), {'remarks': 'looks right'},
            format='json')
        stages = self._stages()
        self.assertEqual(stages[0]['status'], 'APPROVED')
        self.assertEqual(stages[0]['acted_by'], self.approver1.username)
        self.assertEqual(stages[0]['remarks'], 'looks right')
        self.assertIsNotNone(stages[0]['acted_at'])
        # The next stage is now the one waiting.
        self.assertEqual(stages[1]['status'], 'AWAITING')

    def test_a_stage_that_never_got_its_turn_is_not_called_upcoming(self):
        self.client_for(self.approver1).post(
            self.reject_url(self.req), {'remarks': 'too far back'},
            format='json')
        stages = self._stages()
        self.assertEqual(stages[0]['status'], 'REJECTED')
        # "Upcoming" would imply it is still going to happen.
        self.assertEqual(stages[1]['status'], 'SKIPPED')

    def test_the_reviewer_is_resolved_not_stored(self):
        """A stage reassigned today changes who the progress names."""
        successor = make_user('bk-progress-successor', 'BackDate_Approval')
        WorkflowStage.objects.filter(pk=self.stage2.pk).update(user=successor)
        stages = self._stages()
        self.assertEqual(stages[1]['reviewer'], successor.username)

    def test_a_replacement_shows_who_is_covering(self):
        stand_in = make_user('bk-progress-standin', 'BackDate_Approval')
        today = datetime.date.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.approver1, new_user=stand_in, reason='leave',
            start_date=today, end_date=today)
        stages = self._stages()
        self.assertEqual(stages[0]['reviewer'], stand_in.username)
        self.assertEqual(stages[0]['configured_reviewer'],
                         self.approver1.username)
        self.assertTrue(stages[0]['has_active_replacement'])


class SearchTests(_Base):
    """`?search=` finds one request by its id or its SAP user."""

    def setUp(self):
        self.one = make_request(self.requester)
        flow_service.submit(self.one, user=self.requester)
        self.two = make_request(self.requester)
        self.two.sap_username = 'FINANCE9'
        self.two.save(update_fields=['sap_username'])
        flow_service.submit(self.two, user=self.requester)

    def _ids(self, term, user=None, name='backdate-request-list'):
        response = self.client_for(user or self.requester).get(
            reverse(name), {'search': term})
        return {row['id'] for row in response.data['data']}

    def test_searching_by_sap_user(self):
        self.assertEqual(self._ids('FINANCE9'), {self.two.pk})

    def test_searching_by_sap_user_is_case_insensitive(self):
        self.assertEqual(self._ids('finance9'), {self.two.pk})

    def test_searching_by_id_matches_that_id_exactly(self):
        """Not a substring: searching "12" must not bury request 12."""
        self.assertIn(self.one.pk, self._ids(str(self.one.pk)))
        self.assertNotIn(self.two.pk, self._ids(str(self.one.pk)))

    def test_an_empty_search_narrows_nothing(self):
        self.assertEqual(self._ids(''), {self.one.pk, self.two.pk})

    def test_the_approval_queue_is_searchable(self):
        self.assertEqual(
            self._ids('FINANCE9', user=self.approver1,
                      name='backdate-approval-queue'),
            {self.two.pk})

    def test_the_counts_follow_the_search(self):
        """Otherwise the card says 2 over a table showing 1."""
        response = self.client_for(self.approver1).get(
            reverse('backdate-approval-insights'), {'search': 'FINANCE9'})
        self.assertEqual(response.data['data']['total'], 1)

    def test_search_combines_with_the_company_filter(self):
        response = self.client_for(self.requester).get(
            reverse('backdate-request-list'),
            {'search': 'FINANCE9', 'company': BEVERAGES})
        self.assertEqual(response.data['data'], [])


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


class CompletedFilterTests(_Base):
    """`COMPLETED` = approved AND the grant actually reached SAP."""

    def setUp(self):
        self.req = make_request(self.requester)
        self.flow = flow_service.submit(self.req, user=self.requester)
        for approver in (self.approver1, self.approver2):
            self.client_for(approver).post(
                self.approve_url(self.req), {}, format='json')
        self.flow.refresh_from_db()

    def _ids(self, status):
        response = self.client_for(self.requester).get(
            reverse('backdate-request-list'), {'status': status})
        return [row['id'] for row in response.data['data']]

    def _counts(self):
        return self.client_for(self.requester).get(
            reverse('backdate-insights')).data['data']

    def test_a_grant_in_sap_is_completed(self):
        self.flow.hana_status = HanaStatus.SUCCESS
        self.flow.save(update_fields=['hana_status'])
        self.assertEqual(self._ids('COMPLETED'), [self.req.pk])
        self.assertEqual(self._counts()['completed'], 1)

    def test_a_grant_sap_refused_is_approved_but_not_completed(self):
        self.flow.hana_status = HanaStatus.FAILED
        self.flow.save(update_fields=['hana_status'])
        # It WAS approved — a person said yes — and the rights do not exist.
        # Both facts are true, and the two filters say so separately.
        self.assertEqual(self._ids('APPROVED'), [self.req.pk])
        self.assertEqual(self._ids('COMPLETED'), [])
        counts = self._counts()
        self.assertEqual((counts['approved'], counts['completed']), (1, 0))

    def test_a_grant_never_attempted_is_not_completed(self):
        self.assertIsNone(self.flow.hana_status)
        self.assertEqual(self._ids('COMPLETED'), [])

    def test_the_total_never_double_counts_a_completed_request(self):
        self.flow.hana_status = HanaStatus.SUCCESS
        self.flow.save(update_fields=['hana_status'])
        counts = self._counts()
        self.assertEqual(
            counts['total'],
            counts['pending'] + counts['approved'] + counts['rejected'])
        self.assertEqual(counts['total'], 1)

    def test_an_unknown_status_is_no_filter_rather_than_an_error(self):
        """A stale bookmark should show everything, not nothing or a 500."""
        self.assertEqual(self._ids('BANANAS'), [self.req.pk])


class ApprovalInsightsTests(_Base):
    """The desk's KPI counts: pending is the queue, the rest is history."""

    def setUp(self):
        self.req = make_request(self.requester)
        self.flow = flow_service.submit(self.req, user=self.requester)

    def test_a_waiting_request_counts_as_pending_for_its_approver_only(self):
        counts = self.client_for(self.approver1).get(
            reverse('backdate-approval-insights')).data['data']
        self.assertEqual(counts, {'pending': 1, 'approved': 0, 'rejected': 0,
                                  'completed': 0, 'total': 1})
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
                                  'completed': 0, 'total': 0})

    def test_completed_is_a_subset_of_approved_not_a_fourth_state(self):
        """An approved request whose SAP write never landed grants nothing."""
        for approver in (self.approver1, self.approver2):
            self.client_for(approver).post(
                self.approve_url(self.req), {}, format='json')

        flow = BackDateFlow.objects.get(backdate=self.req)
        self.assertEqual(flow.status, FlowStatus.APPROVED)

        # SAP accepted: approved AND completed, the same one request.
        flow.hana_status = HanaStatus.SUCCESS
        flow.save(update_fields=['hana_status'])
        counts = self.client_for(self.approver2).get(
            reverse('backdate-approval-insights')).data['data']
        self.assertEqual(counts['approved'], 1)
        self.assertEqual(counts['completed'], 1)
        # `completed` is inside `approved`, so the total must not count it
        # twice — the card and the list below it would stop agreeing.
        self.assertEqual(counts['total'], 1)

        # SAP refused: still approved by a person, but nothing is in SAP.
        flow.hana_status = HanaStatus.FAILED
        flow.save(update_fields=['hana_status'])
        counts = self.client_for(self.approver2).get(
            reverse('backdate-approval-insights')).data['data']
        self.assertEqual(counts['approved'], 1)
        self.assertEqual(counts['completed'], 0)
        self.assertEqual(counts['total'], 1)

    def test_the_desk_counts_are_refused_without_the_key(self):
        response = self.client_for(self.requester).get(
            reverse('backdate-approval-insights'))
        self.assertEqual(response.status_code, 403)


class StageApproverSeesOwnDecisionTests(_Base):
    """"I approved it, where did it go?"

    A stage-1 approver approves; the request moves to stage 2 and stays
    PENDING. The approval desk used to narrow its Approved view by the FLOW's
    status, so that request was:

      * not in Approved   — the flow is not approved, stage 2 has not decided
      * not in the queue  — it is no longer awaiting this user

    which is nowhere. The person who had just approved it could not find it
    again, and had no way to confirm their own decision had registered.
    """

    def _history(self, user, status=None):
        url = reverse('backdate-approval-history')
        response = self.client_for(user).get(
            url, {'status': status} if status else {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        return [row['id'] for row in response.json()['data']]

    def _queue(self, user):
        response = self.client_for(user).get(
            reverse('backdate-approval-queue'))
        self.assertEqual(response.status_code, 200, response.content[:300])
        return [row['id'] for row in response.json()['data']]

    def _insights(self, user):
        response = self.client_for(user).get(
            reverse('backdate-approval-insights'))
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()['data']

    def test_a_stage_one_approval_is_visible_to_its_approver_immediately(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow = flow_service.approve(flow, user=self.approver1)

        # Still moving: stage 2 has not decided.
        self.assertEqual(flow.status, FlowStatus.PENDING)
        # THE BUG. It must be findable under what THIS user approved.
        self.assertIn(req.pk, self._history(self.approver1, 'APPROVED'))
        # And it has correctly left their queue.
        self.assertNotIn(req.pk, self._queue(self.approver1))

    def test_it_is_not_claimed_by_an_approver_who_has_not_decided(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow_service.approve(flow, user=self.approver1)

        # Stage 2 holds it now — awaiting, not approved by them.
        self.assertNotIn(req.pk, self._history(self.approver2, 'APPROVED'))
        self.assertIn(req.pk, self._queue(self.approver2))

    def test_both_approvers_keep_it_once_the_flow_is_approved(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow = flow_service.approve(flow, user=self.approver1)
        flow_service.approve(flow, user=self.approver2)

        self.assertIn(req.pk, self._history(self.approver1, 'APPROVED'))
        self.assertIn(req.pk, self._history(self.approver2, 'APPROVED'))

    def test_an_approval_later_rejected_stays_under_what_I_approved(self):
        """Each answer is about its own decision, and both are true."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow = flow_service.approve(flow, user=self.approver1)
        flow_service.reject(flow, user=self.approver2, remarks='too far back')

        # approver1 approved it. That happened, whatever came after.
        self.assertIn(req.pk, self._history(self.approver1, 'APPROVED'))
        self.assertNotIn(req.pk, self._history(self.approver1, 'REJECTED'))
        # approver2 rejected it.
        self.assertIn(req.pk, self._history(self.approver2, 'REJECTED'))
        self.assertNotIn(req.pk, self._history(self.approver2, 'APPROVED'))

    def test_rejecting_at_stage_one_shows_under_what_I_rejected(self):
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow_service.reject(flow, user=self.approver1, remarks='no')

        self.assertIn(req.pk, self._history(self.approver1, 'REJECTED'))
        self.assertNotIn(req.pk, self._history(self.approver1, 'APPROVED'))

    def test_the_All_view_holds_everything_this_user_acted_on(self):
        approved = make_request(self.requester)
        rejected = make_request(self.requester)
        flow_service.approve(
            flow_service.submit(approved, user=self.requester),
            user=self.approver1)
        flow_service.reject(
            flow_service.submit(rejected, user=self.requester),
            user=self.approver1, remarks='no')

        everything = self._history(self.approver1)
        self.assertIn(approved.pk, everything)
        self.assertIn(rejected.pk, everything)

    def test_completed_still_asks_whether_SAP_took_it(self):
        """One approver's decision cannot settle whether the grant landed."""
        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow = flow_service.approve(flow, user=self.approver1)
        flow = flow_service.approve(flow, user=self.approver2)
        flow.hana_status = HanaStatus.FAILED
        flow.save(update_fields=['hana_status'])

        # Approved by this user, but the rights never reached SAP.
        self.assertIn(req.pk, self._history(self.approver1, 'APPROVED'))
        self.assertNotIn(req.pk, self._history(self.approver1, 'COMPLETED'))

        flow.hana_status = HanaStatus.SUCCESS
        flow.save(update_fields=['hana_status'])
        self.assertIn(req.pk, self._history(self.approver1, 'COMPLETED'))

    def test_the_counts_agree_with_the_lists(self):
        approved = make_request(self.requester)
        flow_service.approve(
            flow_service.submit(approved, user=self.requester),
            user=self.approver1)
        awaiting = make_request(self.requester)
        flow_service.submit(awaiting, user=self.requester)

        counts = self._insights(self.approver1)
        self.assertEqual(
            counts['approved'], len(self._history(self.approver1, 'APPROVED')))
        self.assertEqual(counts['pending'], len(self._queue(self.approver1)))

    def test_total_is_the_union_when_one_user_holds_two_stages(self):
        """Approve at stage 1 and it comes straight back to you at stage 2.

        `total` used to be pending + approved + rejected, which counted that
        request twice and made the Total card larger than the list it opens.
        """
        self.stage2.user = self.approver1
        self.stage2.save(update_fields=['user'])

        req = make_request(self.requester)
        flow = flow_service.submit(req, user=self.requester)
        flow_service.approve(flow, user=self.approver1)

        counts = self._insights(self.approver1)
        self.assertEqual(counts['pending'], 1, 'awaiting them at stage 2')
        self.assertEqual(counts['approved'], 1, 'they approved stage 1')
        self.assertEqual(counts['total'], 1, 'it is ONE request, not two')


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
