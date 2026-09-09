"""Approval API. Views stay thin — every transition goes through services.py."""
import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.pagination import StandardPagination
from core.responses import created, fail, ok

from . import services
from .models import (
    ApprovalLevel,
    ApprovalLevelApprover,
    ApprovalRequest,
    ApprovalWorkflow,
)
from .permissions import IsApprovalAdmin
from .serializers import (
    ApprovalDecisionSerializer,
    ApprovalLevelApproverSerializer,
    ApprovalLevelSerializer,
    ApprovalRequestDetailSerializer,
    ApprovalRequestSerializer,
    ApprovalWorkflowSerializer,
)

logger = logging.getLogger(__name__)


def request_context(request):
    """Forensic context for the action log — the audit app captures neither."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')
    return {'ip': ip, 'user_agent': request.META.get('HTTP_USER_AGENT', '')}


# ---------------------------------------------------------------------------
# Admin: workflow configuration
# ---------------------------------------------------------------------------

class WorkflowListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = ApprovalWorkflowSerializer
    pagination_class = StandardPagination
    queryset = ApprovalWorkflow.objects.prefetch_related(
        'levels__approvers__user', 'levels__role').all()


class WorkflowDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = ApprovalWorkflowSerializer
    queryset = ApprovalWorkflow.objects.prefetch_related(
        'levels__approvers__user', 'levels__role').all()


class LevelListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = ApprovalLevelSerializer

    def get_queryset(self):
        qs = ApprovalLevel.objects.select_related('role').prefetch_related(
            'approvers__user')
        workflow_id = self.request.query_params.get('workflow')
        return qs.filter(workflow_id=workflow_id) if workflow_id else qs


class LevelDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = ApprovalLevelSerializer
    queryset = ApprovalLevel.objects.select_related('role').all()


class LevelApproverListCreateView(ListCreateAPIView):
    """Grant / list named approvers on a level."""

    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = ApprovalLevelApproverSerializer

    def get_queryset(self):
        return ApprovalLevelApprover.objects.select_related('user').filter(
            level_id=self.kwargs['level_id'])

    def get_serializer_context(self):
        # The serializer needs the URL's level to check for a duplicate grant,
        # since `level` is read-only and never appears in the request body.
        return {**super().get_serializer_context(),
                'level_id': self.kwargs['level_id']}

    def perform_create(self, serializer):
        serializer.save(level_id=self.kwargs['level_id'],
                        assigned_by=self.request.user)


class LevelApproverDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = ApprovalLevelApproverSerializer
    queryset = ApprovalLevelApprover.objects.select_related('user').all()

    def get_serializer_context(self):
        # Same duplicate check as the create view — an edit that changes the
        # company scope can collide with an existing grant just as easily.
        ctx = super().get_serializer_context()
        if self.kwargs.get('pk'):
            obj = ApprovalLevelApprover.objects.filter(pk=self.kwargs['pk']).first()
            if obj:
                ctx['level_id'] = obj.level_id
        return ctx


class WorkflowPreviewView(APIView):
    """Render the ladder a document would take, with resolved approver names.

    Without this, configuring levels is guesswork — an admin cannot otherwise
    tell whether a level has anyone able to approve it until a document
    deadlocks there.
    """

    permission_classes = [IsAuthenticated, IsApprovalAdmin]

    def get(self, request, pk):
        from users.models import User

        workflow = get_object_or_404(ApprovalWorkflow, pk=pk)
        company = (request.query_params.get('company') or workflow.company or '').upper()

        levels = workflow.levels.filter(is_active=True).select_related('role').order_by('sequence')
        out = []
        for level in levels:
            ids = services.eligible_approver_ids(level, company)
            names = list(
                User.objects.filter(id__in=ids).values_list('name', flat=True))
            out.append({
                'sequence': level.sequence,
                'name': level.name,
                'role': getattr(level.role, 'name', None),
                'min_approvals': level.min_approvals,
                'eligible_approvers': names,
                'eligible_count': len(ids),
                'blocked': len(ids) == 0,
            })
        return ok({
            'workflow': workflow.code,
            'company': company,
            'total_levels': len(out),
            'levels': out,
        })


# ---------------------------------------------------------------------------
# Approver-facing
# ---------------------------------------------------------------------------

class InboxView(APIView):
    """Requests awaiting THIS user's decision."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = services.inbox_for(request.user)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = ApprovalRequestSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


class ApprovalRequestListView(APIView):
    """Requests visible to the caller, filterable by status/company/type.

    A PENDING request is shown only to the approvers of the level it is
    *currently* sitting on. Without that, a level-2 approver saw a document
    still waiting on level 1 — two people looking at the same entry at once,
    with no way to tell whose turn it was. Decided requests (approved,
    rejected, cancelled) stay visible to everyone as history.

    `?scope=all` opts out, for an admin overview.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ApprovalRequest.objects.select_related(
            'workflow', 'content_type', 'submitted_by')

        if value := request.query_params.get('status'):
            qs = qs.filter(status=value.upper())
        if value := request.query_params.get('company'):
            qs = qs.filter(company=value.upper())
        if value := request.query_params.get('document_type'):
            qs = qs.filter(workflow__document_type=value.upper())
        if request.query_params.get('mine') == 'true':
            qs = qs.filter(submitted_by=request.user)
        elif request.query_params.get('scope') != 'all':
            # Hide PENDING rows whose current level this user cannot act on.
            # Everything already decided is left alone, so history stays whole.
            actionable = services.actionable_request_ids(request.user)
            qs = qs.filter(
                ~Q(status=ApprovalRequest.Status.PENDING) | Q(id__in=actionable))

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs.order_by('-created_at'), request, view=self)
        return paginator.get_paginated_response(
            ApprovalRequestSerializer(page, many=True).data)


class ApprovalRequestDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        obj = get_object_or_404(
            ApprovalRequest.objects
            .select_related('workflow', 'content_type', 'submitted_by')
            .prefetch_related('actions__approver'),
            pk=pk,
        )
        data = ApprovalRequestDetailSerializer(obj).data
        data['can_act'] = services.can_act(request.user, obj)
        return ok(data)


class ApprovalActView(APIView):
    """Approve / reject / cancel one request."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = ApprovalDecisionSerializer(data=request.data)
        if not serializer.is_valid():
            return fail('Invalid decision payload.', errors=serializer.errors)

        decision = serializer.validated_data['decision']
        remarks = serializer.validated_data.get('remarks', '')
        ctx = request_context(request)

        try:
            if decision == 'APPROVE':
                obj = services.approve(request_id=pk, user=request.user,
                                       remarks=remarks, ctx=ctx)
            elif decision == 'REJECT':
                obj = services.reject(request_id=pk, user=request.user,
                                      remarks=remarks, ctx=ctx)
            else:
                obj = services.cancel(request_id=pk, user=request.user,
                                      remarks=remarks, ctx=ctx)
        except ApprovalRequest.DoesNotExist:
            return fail('Approval request not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            return fail(str(exc), status=http_status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return fail('; '.join(exc.messages))

        return ok(ApprovalRequestDetailSerializer(obj).data,
                  message=f'Request {obj.get_status_display().lower()}.')
