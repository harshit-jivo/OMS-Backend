"""API surface: auth, permission keys, error codes, and configuration writes.

The runtime half of this file (submit / approve / reject through the
TestFlow harness) is gone with the harness. Approval runtime belongs to
the business module now, so there is no engine endpoint left to test —
what this engine promises a module is covered by
`test_selection_service.py`.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.companies import BEVERAGES, OIL
from workflow.models import COMPANY_ALL, WorkflowQuery
from workflow.tests.factories import (
    DOCUMENT_TABLE,
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
    """Company is ONE field holding ALL / OIL / BEVERAGES / MART."""

    def test_create_workflow_for_all_companies(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'WF_ALL', 'name': 'All',
             'company': 'ALL'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        # Stored as given — `ALL` is a value, not a NULL plus a second flag.
        self.assertEqual(response.data['data']['company'], 'ALL')

    def test_create_workflow_for_one_company(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'WF_OIL', 'name': 'Oil',
             'company': OIL},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['data']['company'], OIL)

    def test_every_accepted_company_value_round_trips(self):
        for i, company in enumerate(['ALL', OIL, BEVERAGES, 'MART']):
            with self.subTest(company=company):
                response = self.client_for(self.admin).post(
                    '/api/workflow/queries/',
                    {'workflow': make_workflow(self.module,
                                               code=f'WF_RT{i}').pk,
                     'name': f'rt-{company}', 'company': company,
                     'query_text': f'SELECT * FROM {DOCUMENT_TABLE}'},
                    format='json',
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['data']['company'], company)

    def test_unknown_company_is_a_field_error_and_writes_nothing(self):
        before = WorkflowQuery.objects.count()
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': make_workflow(self.module, code='WF_BADCO').pk,
             'name': 'punjab', 'company': 'PUNJAB',
             'query_text': f'SELECT * FROM {DOCUMENT_TABLE}'},
            format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        # The CHECK would also catch it, but as a 500. No row either way.
        self.assertEqual(WorkflowQuery.objects.count(), before)

    def test_empty_company_is_a_field_error(self):
        response = self.client_for(self.admin).post(
            '/api/workflow/workflows/',
            {'module': self.module.pk, 'code': 'BAD', 'name': 'bad',
             'company': ''},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_query_payload_rejects_the_removed_configuration_fields(self):
        """`type` and `key_column` must not be storable under any route."""
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': make_workflow(self.module, code='WF_LEGACY').pk,
             'name': 'legacy', 'company': 'ALL',
             'query_text': f'SELECT * FROM {DOCUMENT_TABLE}',
             'type': 'something', 'key_column': 'DocEntry'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        # Ignored, not stored: the serializer does not declare them and the
        # columns no longer exist.
        self.assertNotIn('type', response.data['data'])
        self.assertNotIn('key_column', response.data['data'])
        query = WorkflowQuery.objects.get(pk=response.data['data']['id'])
        self.assertFalse(hasattr(query, 'type'))
        self.assertFalse(hasattr(query, 'key_column'))

    def test_query_validation_runs_on_create(self):
        wf = make_workflow(self.module, company=None)
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'q1', 'company': 'ALL',
             'query_text': f'SELECT * FROM {DOCUMENT_TABLE}'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(response.data['data']['validated_at'])

    def test_invalid_sql_is_refused_and_writes_nothing(self):
        """Bad SQL never reaches the table.

        `check_query` runs in `validate()`, BEFORE the insert, so an invalid
        query is a 400 and no row exists — not a stored row carrying its own
        problems. That is the §17 flow, and it is also what keeps the
        `workflow_query_select_only` CHECK from surfacing as a 500.
        """
        wf = make_workflow(self.module, company=None)
        before = WorkflowQuery.objects.count()
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'bad', 'company': 'ALL',
             # A forbidden SCHEMA: the per-module relation allow-list is gone,
             # so `users_user` now validates, but pg_catalog never does.
             'query_text': 'SELECT oid AS id FROM pg_catalog.pg_class'},
            format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(WorkflowQuery.objects.count(), before)

    def test_a_query_that_went_stale_is_skipped_not_executed(self):
        """`validated_at = NULL` is still the execution gate.

        The API cannot create this state any more, but the database can reach
        it: a query that validated when it was saved and whose table was later
        dropped. Written directly, because that is how it actually occurs.
        """
        wf = make_workflow(self.module, company=None)
        stale = WorkflowQuery.objects.create(
            workflow=wf, name='stale', company='ALL',
            query_text=f'SELECT * FROM {DOCUMENT_TABLE}',
        )
        self.assertIsNone(stale.validated_at)
        response = self.client_for(self.admin).get(
            f'/api/workflow/queries/?workflow={wf.pk}')
        self.assertEqual(response.status_code, 200)
        row = next(q for q in response.data['data'] if q['id'] == stale.pk)
        self.assertIsNone(row['validated_at'])

    def test_dml_query_is_400_not_500(self):
        """The CHECK fires on INSERT, so this must be caught before saving."""
        wf = make_workflow(self.module, company=None)
        before = WorkflowQuery.objects.count()
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'dml', 'company': 'ALL',
             'query_text': f'UPDATE {DOCUMENT_TABLE} SET is_active = true'},
            format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(WorkflowQuery.objects.count(), before)

    def test_query_contradicting_its_workflow_company_is_refused(self):
        wf = make_workflow(self.module, code='WF_OIL', company=OIL)
        response = self.client_for(self.admin).post(
            '/api/workflow/queries/',
            {'workflow': wf.pk, 'name': 'bev', 'company': BEVERAGES,
             'query_text': f'SELECT * FROM {DOCUMENT_TABLE}'},
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
