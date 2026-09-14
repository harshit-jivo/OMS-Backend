"""API surface: auth, permission keys, error codes, and the TestFlow flow."""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.companies import BEVERAGES, OIL
from workflow.models import (
    CompanyScope,
    TestDocument,
    TestDocumentLog,
    TestFlow,
    WorkflowTask,
)
from workflow.tests.factories import (
    TESTDOC_TABLE,
    make_module,
    make_query,
    make_stage,
    make_user,
    make_workflow,
)


def grant(user, *keys):
    """Give a user permission keys the way the project's own model does.

    `core.permissions.effective_keys` reads the user's `extra_pages`, so a
    test grants by writing there rather than by inventing a second mechanism.
    """
    existing = list(getattr(user, 'extra_pages', None) or [])
    user.extra_pages = existing + list(keys)
    user.save(update_fields=['extra_pages'])
    return user


class _ApiBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.admin = grant(make_user('wf-admin'),
                          'workflow.config.manage', 'workflow.task.act',
                          'workflow.state.view')
        cls.approver = grant(make_user('wf-approver'), 'workflow.task.act')
        cls.nobody = make_user('wf-nobody')

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client


class AuthenticationTests(_ApiBase):
    def test_anonymous_is_refused(self):
        response = APIClient().get('/api/workflow/modules/')
        self.assertIn(response.status_code, (401, 403))

    def test_authenticated_without_the_key_is_refused(self):
        response = self.client_for(self.nobody).get('/api/workflow/modules/')
        self.assertEqual(response.status_code, 403)

    def test_key_holder_is_allowed(self):
        response = self.client_for(self.admin).get('/api/workflow/modules/')
        self.assertEqual(response.status_code, 200)

    def test_inbox_needs_only_authentication(self):
        response = self.client_for(self.nobody).get('/api/workflow/inbox/')
        self.assertEqual(response.status_code, 200)


class ConfigurationApiTests(_ApiBase):
    def test_create_workflow_with_all_scope(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'WF_ALL', 'name': 'All',
             'company_scope': 'ALL'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['data']['company_scope'], 'ALL')
        self.assertIsNone(response.data['data']['company'])

    def test_create_workflow_with_specific_scope(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'WF_OIL', 'name': 'Oil',
             'company_scope': 'SPECIFIC', 'company': OIL},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['data']['company'], OIL)

    def test_all_scope_with_a_company_is_a_field_error_not_a_500(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'BAD', 'name': 'bad',
             'company_scope': 'ALL', 'company': OIL},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_specific_scope_without_a_company_is_a_field_error(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'BAD', 'name': 'bad',
             'company_scope': 'SPECIFIC'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_query_validation_runs_on_create(self):
        wf = make_workflow(self.module, company=None)
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'q1', 'company_scope': 'ALL',
             'query_text': f'SELECT * FROM {TESTDOC_TABLE}'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(response.data['data']['validated_at'])

    def test_invalid_query_is_stored_unvalidated_with_problems(self):
        wf = make_workflow(self.module, company=None)
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'bad', 'company_scope': 'ALL',
             'query_text': 'SELECT id FROM users_user'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data['data']['validated_at'])
        self.assertTrue(response.data['data']['validation_problems'])

    def test_query_contradicting_its_workflow_scope_is_refused(self):
        wf = make_workflow(self.module, code='WF_OIL', company=OIL)
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'bev', 'company_scope': 'SPECIFIC',
             'company': BEVERAGES,
             'query_text': f'SELECT * FROM {TESTDOC_TABLE}'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_replacement_overlap_returns_409_not_500(self):
        u1, u2, u3 = (make_user('r1'), make_user('r2'), make_user('r3'))
        client = self.client_for(self.admin)
        first = client.post('/api/workflow/replacements/',
                            {'old_user': u1.pk, 'new_user': u2.pk,
                             'start_date': '2026-09-15',
                             'end_date': '2026-09-22'}, format='json')
        self.assertEqual(first.status_code, 201, first.data)

        clash = client.post('/api/workflow/replacements/',
                            {'old_user': u1.pk, 'new_user': u3.pk,
                             'start_date': '2026-09-20',
                             'end_date': '2026-09-25'}, format='json')
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.data['errors']['code'], 'ReplacementOverlap')


class TestFlowApiTests(_ApiBase):
    """End-to-end through the HTTP surface."""

    def setUp(self):
        super().setUp()
        self.wf = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(self.wf, company=None)
        make_stage(self.wf, 1, self.approver, name='Manager')
        make_stage(self.wf, 2, self.admin, name='Finance')

    def _create_doc(self, company=OIL):
        response = self.client_for(self.admin).post(
            '/api/workflow/testflow/documents/',
            {'company': company, 'amount': '1000.00',
             'document_number': 'TD-1'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data['data']['id']

    def test_full_approve_path(self):
        doc_id = self._create_doc()

        submit = self.client_for(self.admin).post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        self.assertEqual(submit.status_code, 201, submit.data)
        self.assertFalse(submit.data['data']['resubmission'])
        task_id = submit.data['data']['task']['id']

        # Stage 1 belongs to the approver, not the admin.
        wrong = self.client_for(self.admin).post(
            f'/api/workflow/tasks/{task_id}/approve/', {}, format='json')
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.data['errors']['code'],
                         'UnauthorizedWorkflowAction')

        first = self.client_for(self.approver).post(
            f'/api/workflow/tasks/{task_id}/approve/',
            {'remarks': 'ok'}, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data['data']['flow_status'], 'PENDING')
        next_id = first.data['data']['next_task']['id']

        second = self.client_for(self.admin).post(
            f'/api/workflow/tasks/{next_id}/approve/', {}, format='json')
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data['data']['flow_status'], 'APPROVED')
        self.assertIsNone(second.data['data']['next_task'])

    def test_reject_path_and_state_endpoint(self):
        doc_id = self._create_doc()
        submit = self.client_for(self.admin).post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        task_id = submit.data['data']['task']['id']
        flow_id = submit.data['data']['flow']['id']

        rejected = self.client_for(self.approver).post(
            f'/api/workflow/tasks/{task_id}/reject/',
            {'remarks': 'no'}, format='json')
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data['data']['flow_status'], 'REJECTED')

        state = self.client_for(self.admin).get(
            f'/api/workflow/flows/TESTFLOW/{flow_id}/state/')
        self.assertEqual(state.status_code, 200, state.data)
        self.assertEqual(state.data['data']['flow']['status'], 'REJECTED')
        actions = [a['action'] for a in state.data['data']['history']]
        self.assertEqual(actions, ['SUBMIT', 'REJECT'])

    def test_resubmission_reuses_the_same_flow_row(self):
        doc_id = self._create_doc()
        client = self.client_for(self.admin)

        first = client.post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        flow_id = first.data['data']['flow']['id']
        task_id = first.data['data']['task']['id']

        self.client_for(self.approver).post(
            f'/api/workflow/tasks/{task_id}/reject/', {}, format='json')

        again = client.post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        self.assertEqual(again.status_code, 201, again.data)
        self.assertTrue(again.data['data']['resubmission'])
        # SAME flow row, and only one exists.
        self.assertEqual(again.data['data']['flow']['id'], flow_id)
        self.assertEqual(TestFlow.objects.filter(document_id=doc_id).count(), 1)

        # RESUBMITTED is a MODULE event, never a workflow_action.
        events = list(TestDocumentLog.objects
                      .filter(document_id=doc_id)
                      .order_by('sequence')
                      .values_list('event', flat=True))
        self.assertEqual(events, ['SUBMITTED', 'RESUBMITTED'])

    def test_resubmission_while_running_is_refused_by_the_module(self):
        doc_id = self._create_doc()
        client = self.client_for(self.admin)
        client.post(f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
                    format='json')
        again = client.post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data['errors']['code'], 'WorkflowAlreadyRunning')

    def test_not_configured_returns_409_with_a_code(self):
        # A company no configured workflow applies to.
        self.wf.company_scope = CompanyScope.SPECIFIC
        self.wf.company = OIL
        self.wf.save()
        for q in self.wf.queries.all():
            q.company_scope = CompanyScope.SPECIFIC
            q.company = OIL
            q.save()

        doc_id = self._create_doc(company=BEVERAGES)
        response = self.client_for(self.admin).post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['errors']['code'],
                         'WorkflowNotConfigured')

    def test_ambiguous_selection_returns_409_and_creates_nothing(self):
        other = make_workflow(self.module, code='WF_B', company=None)
        make_query(other, name='b', company=None)
        make_stage(other, 1, self.approver)

        doc_id = self._create_doc()
        response = self.client_for(self.admin).post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['errors']['code'],
                         'AmbiguousWorkflowSelection')
        # Full rollback: no flow, no task, and no module log either.
        self.assertEqual(TestFlow.objects.filter(document_id=doc_id).count(), 0)
        self.assertEqual(WorkflowTask.objects.count(), 0)
        self.assertEqual(
            TestDocumentLog.objects.filter(document_id=doc_id).count(), 0)

    def test_inbox_shows_the_open_task_to_its_owner_only(self):
        doc_id = self._create_doc()
        self.client_for(self.admin).post(
            f'/api/workflow/testflow/documents/{doc_id}/submit/', {},
            format='json')

        mine = self.client_for(self.approver).get('/api/workflow/inbox/')
        self.assertEqual(len(mine.data['data']), 1)
        theirs = self.client_for(self.nobody).get('/api/workflow/inbox/')
        self.assertEqual(len(theirs.data['data']), 0)
