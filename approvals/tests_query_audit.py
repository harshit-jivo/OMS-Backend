"""Phase 4.2 -- query-count audit for `approvals` list endpoints.

Same technique as `orders.tests_query_audit`: seed N approval requests,
capture the query count via `django.test.utils.CaptureQueriesContext`, seed
more, capture again, and assert the count did not scale.

Run with::

    python manage.py test approvals --settings=OMS.test_settings
"""
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User

from .models import (
    ApprovalLevel, ApprovalLevelApprover, ApprovalRequest, ApprovalWorkflow,
)
from .views import (
    ApprovalRequestListView, InboxView, LevelListCreateView, WorkflowListCreateView,
)


def _query_count(view_callable, request):
    with CaptureQueriesContext(connection) as ctx:
        response = view_callable(request)
        assert response.status_code == 200, (response.status_code, getattr(response, 'data', None))
    return len(ctx.captured_queries)


class ApprovalsListQueryAuditTests(TestCase):
    """`InboxView` and `ApprovalRequestListView` both select_related their FKs
    and resolve "what can this user act on" ONCE per request (a fixed query
    over levels/workflows, not one per `ApprovalRequest` row) -- measured here
    rather than assumed."""

    @classmethod
    def setUpTestData(cls):
        cls.approver = User.objects.create_user(
            username='qa-approver', password='pw', name='QA Approver')
        cls.submitter = User.objects.create_user(
            username='qa-submitter', password='pw', name='QA Submitter')
        cls.admin = User.objects.create_user(
            username='qa-approvals-admin', password='pw', name='QA Admin', is_staff=True)

        cls.workflow = ApprovalWorkflow.objects.create(
            code='QA_WF', name='QA Workflow', document_type='PAYMENT', company='OIL')
        cls.level = ApprovalLevel.objects.create(
            workflow=cls.workflow, sequence=1, name='L1')
        ApprovalLevelApprover.objects.create(level=cls.level, user=cls.approver)
        cls.content_type = ContentType.objects.get_for_model(User)

    def _add_requests(self, count):
        base = ApprovalRequest.objects.count()
        for i in range(count):
            # object_id does not need to reference a real row -- neither view
            # under test ever dereferences the GenericForeignKey.
            ApprovalRequest.objects.create(
                workflow=self.workflow,
                content_type=self.content_type,
                object_id=base + i + 1,
                company='OIL',
                amount=100,
                document_number=f'DOC-{base + i}',
                status=ApprovalRequest.Status.PENDING,
                current_level=1,
                total_levels=1,
                submitted_by=self.submitter,
            )

    def _request(self, path, user):
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=user)
        return request

    def test_inbox_view_query_count_does_not_scale(self):
        self._add_requests(3)
        count_at_3 = _query_count(
            InboxView.as_view(), self._request('/api/approvals/inbox/', self.approver))

        self._add_requests(3)  # 6 requests total
        count_at_6 = _query_count(
            InboxView.as_view(), self._request('/api/approvals/inbox/', self.approver))

        self.assertEqual(
            count_at_3, count_at_6,
            f'InboxView issued {count_at_3} queries for 3 requests but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )

    def test_approval_request_list_view_query_count_does_not_scale(self):
        self._add_requests(3)
        count_at_3 = _query_count(
            ApprovalRequestListView.as_view(),
            self._request('/api/approvals/requests/?scope=all', self.approver))

        self._add_requests(3)
        count_at_6 = _query_count(
            ApprovalRequestListView.as_view(),
            self._request('/api/approvals/requests/?scope=all', self.approver))

        self.assertEqual(
            count_at_3, count_at_6,
            f'ApprovalRequestListView issued {count_at_3} queries for 3 '
            f'requests but {count_at_6} for 6 -- query count scales with row '
            f'count (N+1).',
        )

    def test_level_list_view_query_count_does_not_scale_with_approver_count(self):
        """`LevelListCreateView` select_related('role') + prefetch_related
        ('approvers__user') -- confirms that holds as named approvers pile up
        on one level, not just as levels are added."""
        level = ApprovalLevel.objects.create(
            workflow=self.workflow, sequence=2, name='Named-approver level')
        for i in range(3):
            user = User.objects.create_user(
                username=f'qa-lvl-approver-{i}', password='pw', name=f'Approver {i}')
            ApprovalLevelApprover.objects.create(level=level, user=user)
        count_at_3 = _query_count(
            LevelListCreateView.as_view(),
            self._request(f'/api/approvals/levels/?workflow={self.workflow.id}', self.admin))

        for i in range(3, 6):
            user = User.objects.create_user(
                username=f'qa-lvl-approver-{i}', password='pw', name=f'Approver {i}')
            ApprovalLevelApprover.objects.create(level=level, user=user)
        count_at_6 = _query_count(
            LevelListCreateView.as_view(),
            self._request(f'/api/approvals/levels/?workflow={self.workflow.id}', self.admin))

        self.assertEqual(
            count_at_3, count_at_6,
            f'LevelListCreateView issued {count_at_3} queries for 3 '
            f'approvers but {count_at_6} for 6 -- query count scales with '
            f'row count (N+1).',
        )

    def test_workflow_list_view_query_count_does_not_scale_with_level_count(self):
        """`WorkflowListCreateView` prefetches `levels__approvers__user` and
        `levels__role` -- confirms that holds as levels/approvers are added,
        not just as workflows are added."""
        for i in range(3):
            level = ApprovalLevel.objects.create(
                workflow=self.workflow, sequence=10 + i, name=f'Extra L{i}')
            ApprovalLevelApprover.objects.create(level=level, user=self.approver)
        count_at_a_few_levels = _query_count(
            WorkflowListCreateView.as_view(),
            self._request('/api/approvals/workflows/', self.admin))

        for i in range(3, 6):
            level = ApprovalLevel.objects.create(
                workflow=self.workflow, sequence=10 + i, name=f'Extra L{i}')
            ApprovalLevelApprover.objects.create(level=level, user=self.approver)
        count_at_more_levels = _query_count(
            WorkflowListCreateView.as_view(),
            self._request('/api/approvals/workflows/', self.admin))

        self.assertEqual(
            count_at_a_few_levels, count_at_more_levels,
            f'WorkflowListCreateView issued {count_at_a_few_levels} queries '
            f'with fewer levels/approvers but {count_at_more_levels} with '
            f'more -- query count scales with row count (N+1).',
        )
