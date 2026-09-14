"""Workflow engine API.

Authentication is the project default (JWT via `DEFAULT_AUTHENTICATION_CLASSES`);
nothing here defines its own. Authorisation uses `core.permissions.HasKey` with
keys registered in `core.permission_registry` — configuration endpoints require
`workflow.config.manage`, runtime endpoints require `workflow.task.act`, and
acting on a task additionally requires being that task's EFFECTIVE actor, which
the engine enforces (`UnauthorizedWorkflowAction`).

Responses use the `core.responses` envelope ({success, message, data}), the
convention the newer apps standardise on.
"""
import logging

from django.db import IntegrityError
from rest_framework import status as http_status
from rest_framework.generics import (
    ListCreateAPIView,
    RetrieveUpdateDestroyAPIView,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.permissions import HasKey
from core.responses import created, fail, ok
from workflow import exceptions as wf_exc
from workflow.models import (
    TestDocument,
    TestDocumentLog,
    TestFlow,
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowTask,
    WorkflowUserReplacement,
)
from workflow.serializers import (
    TestDocumentSerializer,
    TestFlowSerializer,
    WorkflowActionSerializer,
    WorkflowModuleSerializer,
    WorkflowQuerySerializer,
    WorkflowSerializer,
    WorkflowStageSerializer,
    WorkflowTaskSerializer,
    WorkflowUserReplacementSerializer,
)
from workflow.services import conditions, engine

logger = logging.getLogger(__name__)

CONFIG_KEY = 'workflow.config.manage'
ACT_KEY = 'workflow.task.act'
VIEW_KEY = 'workflow.state.view'

#: Engine error -> HTTP status. Each error is explicit rather than collapsing
#: into a generic 400, so a client can branch on it.
ERROR_STATUS = {
    wf_exc.WorkflowNotConfigured: http_status.HTTP_409_CONFLICT,
    wf_exc.AmbiguousWorkflowSelection: http_status.HTTP_409_CONFLICT,
    wf_exc.WorkflowAlreadyRunning: http_status.HTTP_409_CONFLICT,
    wf_exc.InvalidWorkflowAction: http_status.HTTP_400_BAD_REQUEST,
    wf_exc.UnauthorizedWorkflowAction: http_status.HTTP_403_FORBIDDEN,
    wf_exc.StageUserUnavailable: http_status.HTTP_409_CONFLICT,
    wf_exc.InvalidWorkflowConfiguration: http_status.HTTP_409_CONFLICT,
    wf_exc.ConditionExecutionError: http_status.HTTP_400_BAD_REQUEST,
    wf_exc.QueryValidationError: http_status.HTTP_400_BAD_REQUEST,
}


def _engine_error(exc):
    """Translate an engine exception into the project's error envelope.

    `code` is the exception class name so clients branch on a stable token
    rather than on message text. No database or SQL text is ever included —
    `ConditionExecutionError` already carries a sanitised message and logs the
    original.
    """
    for klass, code in ERROR_STATUS.items():
        if isinstance(exc, klass):
            status_code = code
            break
    else:
        status_code = http_status.HTTP_400_BAD_REQUEST
    return fail(
        exc.message,
        errors={'code': type(exc).__name__, **(exc.context or {})},
        status=status_code,
    )


def _request_ctx(request):
    """IP and user agent for the action history."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return {
        'ip_address': (forwarded.split(',')[0].strip()
                       or request.META.get('REMOTE_ADDR') or None),
        'user_agent': request.META.get('HTTP_USER_AGENT', ''),
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class EnvelopeMixin:
    """Put generic-view responses into the project's `{success, message, data}`.

    DRF's generic views return the serializer payload bare, while the custom
    APIViews in this module already answer through `core.responses`. That left
    the workflow API with two shapes, so a client had to handle both. This
    normalises the generic half onto the convention `core/responses.py`
    defines for new apps.

    Deliberately narrow:

    * **Errors are untouched.** `core.exception_handler` already sets
      `success`, `message`, `detail` and `error` on every error body, so
      wrapping again would nest a validation error one level deeper and change
      the shape clients read. Anything >= 400 passes straight through.
    * **204 is untouched**, because a No Content response must not grow a body.
    * A payload that already carries `success` is left alone, so this is safe
      if a view is ever switched to `core.responses` directly.

    Status codes, permissions, JWT and pagination are unaffected: only the
    body of an already-successful response is re-shaped.
    """

    #: Per-method message; overridden where a view wants something specific.
    envelope_messages = {
        'POST': 'Created.',
        'PUT': 'Updated.',
        'PATCH': 'Updated.',
    }

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code >= 400 or response.status_code == 204:
            return response
        data = response.data
        if isinstance(data, dict) and 'success' in data:
            return response
        response.data = {
            'success': True,
            'message': self.envelope_messages.get(request.method, ''),
            'data': data,
        }
        return response


class _ConfigView(EnvelopeMixin):
    def get_permissions(self):
        return [IsAuthenticated(), HasKey(CONFIG_KEY)]


class ModuleListCreateView(_ConfigView, ListCreateAPIView):
    queryset = WorkflowModule.objects.all()
    serializer_class = WorkflowModuleSerializer


class ModuleDetailView(_ConfigView, RetrieveUpdateDestroyAPIView):
    queryset = WorkflowModule.objects.all()
    serializer_class = WorkflowModuleSerializer


class WorkflowListCreateView(_ConfigView, ListCreateAPIView):
    serializer_class = WorkflowSerializer

    def get_queryset(self):
        qs = (Workflow.objects
              .select_related('module')
              .prefetch_related('stages__user', 'queries'))
        module = self.request.query_params.get('module')
        if module:
            qs = qs.filter(module__code=module.upper())
        return qs


class WorkflowDetailView(_ConfigView, RetrieveUpdateDestroyAPIView):
    serializer_class = WorkflowSerializer
    queryset = (Workflow.objects
                .select_related('module')
                .prefetch_related('stages__user', 'queries'))


class QueryListCreateView(_ConfigView, ListCreateAPIView):
    serializer_class = WorkflowQuerySerializer

    def get_queryset(self):
        qs = WorkflowQuery.objects.select_related('workflow',
                                                  'workflow__module')
        workflow = self.request.query_params.get('workflow')
        if workflow:
            qs = qs.filter(workflow_id=workflow)
        return qs


class QueryDetailView(_ConfigView, RetrieveUpdateDestroyAPIView):
    serializer_class = WorkflowQuerySerializer
    queryset = WorkflowQuery.objects.select_related('workflow',
                                                    'workflow__module')


class QueryRevalidateView(APIView):
    """Re-run validation for a stored query without editing it.

    Useful after the module's allow-list changes, and the way an operator
    clears a stale `validation_error`.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(CONFIG_KEY)]

    def post(self, request, pk):
        try:
            query = WorkflowQuery.objects.select_related(
                'workflow', 'workflow__module').get(pk=pk)
        except WorkflowQuery.DoesNotExist:
            return fail('Query not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        problems = conditions.validate_and_stamp(query)
        query.refresh_from_db()
        return ok(
            {
                'query': WorkflowQuerySerializer(query).data,
                'problems': problems,
                'isolation_mode': conditions.isolation_mode(),
            },
            message='Query validated.' if not problems
                    else 'Query failed validation and will not be executed.',
        )


class StageListCreateView(_ConfigView, ListCreateAPIView):
    serializer_class = WorkflowStageSerializer

    def get_queryset(self):
        qs = WorkflowStage.objects.select_related('workflow', 'user')
        workflow = self.request.query_params.get('workflow')
        if workflow:
            qs = qs.filter(workflow_id=workflow)
        return qs


class StageDetailView(_ConfigView, RetrieveUpdateDestroyAPIView):
    serializer_class = WorkflowStageSerializer
    queryset = WorkflowStage.objects.select_related('workflow', 'user')


class ReplacementListCreateView(_ConfigView, ListCreateAPIView):
    serializer_class = WorkflowUserReplacementSerializer
    queryset = WorkflowUserReplacement.objects.select_related('old_user',
                                                              'new_user')

    def create(self, request, *args, **kwargs):
        # The overlap bar is a database exclusion constraint, so the readable
        # message is produced here rather than by re-checking in Python (which
        # would also race).
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError as exc:
            if 'workflow_replacement_no_overlap' in str(exc):
                return fail(
                    'This user already has a replacement covering part of '
                    'that date range. Replacement periods may not overlap.',
                    errors={'code': 'ReplacementOverlap'},
                    status=http_status.HTTP_409_CONFLICT,
                )
            raise


class ReplacementDetailView(_ConfigView, RetrieveUpdateDestroyAPIView):
    serializer_class = WorkflowUserReplacementSerializer
    queryset = WorkflowUserReplacement.objects.select_related('old_user',
                                                              'new_user')


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class InboxView(APIView):
    """Pending tasks the caller owns, including any they stand in for."""

    def get_permissions(self):
        return [IsAuthenticated()]

    def get(self, request):
        tasks = engine.inbox_for(request.user)
        return ok(WorkflowTaskSerializer(tasks, many=True).data)


class TaskDetailView(APIView):
    def get_permissions(self):
        return [IsAuthenticated()]

    def get(self, request, pk):
        try:
            task = WorkflowTask.objects.select_related(
                'module', 'stage', 'stage__workflow', 'stage_user').get(pk=pk)
        except WorkflowTask.DoesNotExist:
            return fail('Task not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        return ok(WorkflowTaskSerializer(task).data)


class TaskApproveView(APIView):
    """POST — approve the task. One approve completes the stage."""

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(ACT_KEY)]

    def post(self, request, pk):
        if not WorkflowTask.objects.filter(pk=pk).exists():
            return fail('Task not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        try:
            flow, next_task = engine.approve(
                task_id=pk,
                user=request.user,
                remarks=request.data.get('remarks', ''),
                ctx=_request_ctx(request),
            )
        except wf_exc.WorkflowError as exc:
            return _engine_error(exc)
        return ok({
            'flow_id': flow.pk,
            'flow_status': flow.status,
            'current_sequence': flow.current_sequence,
            'next_task': (WorkflowTaskSerializer(next_task).data
                          if next_task else None),
        }, message='Approved.')


class TaskRejectView(APIView):
    """POST — reject the task. One reject ends the workflow execution."""

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(ACT_KEY)]

    def post(self, request, pk):
        if not WorkflowTask.objects.filter(pk=pk).exists():
            return fail('Task not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        try:
            flow, _ = engine.reject(
                task_id=pk,
                user=request.user,
                remarks=request.data.get('remarks', ''),
                ctx=_request_ctx(request),
            )
        except wf_exc.WorkflowError as exc:
            return _engine_error(exc)
        return ok({'flow_id': flow.pk, 'flow_status': flow.status},
                  message='Rejected.')


class FlowStateView(APIView):
    """Current engine state plus append-only history for one flow."""

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(VIEW_KEY)]

    def get(self, request, module_code, flow_id):
        try:
            module = WorkflowModule.objects.get(code=module_code.upper())
        except WorkflowModule.DoesNotExist:
            return fail('Module not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        try:
            model = engine.flow_model_for(module)
        except wf_exc.WorkflowError as exc:
            return _engine_error(exc)

        flow = (model.objects
                .select_related('workflow', 'matched_query', 'current_stage')
                .filter(pk=flow_id).first())
        if flow is None:
            return fail('Flow not found.',
                        status=http_status.HTTP_404_NOT_FOUND)

        tasks = (WorkflowTask.objects
                 .filter(module=module, flow_id=flow_id)
                 .select_related('module', 'stage', 'stage__workflow',
                                 'stage_user')
                 .order_by('sequence', 'id'))
        history = engine.history_for(module, flow_id)
        return ok({
            'flow': TestFlowSerializer(flow).data
                    if isinstance(flow, TestFlow) else {
                        'id': flow.pk, 'status': flow.status,
                        'current_sequence': flow.current_sequence,
                    },
            'tasks': WorkflowTaskSerializer(tasks, many=True).data,
            'history': WorkflowActionSerializer(history, many=True).data,
        })


# ---------------------------------------------------------------------------
# TestFlow harness — the first integration target
# ---------------------------------------------------------------------------

def _log_module_event(document, event, *, user=None, remarks=''):
    """Append to the MODULE's own history.

    This is the `flow_logs` role from the plan. RESUBMITTED lives here and
    never in `workflow_action` — resubmission is a module event.
    """
    from django.db.models import Max
    top = (TestDocumentLog.objects.filter(document=document)
           .aggregate(t=Max('sequence'))['t'] or 0)
    return TestDocumentLog.objects.create(
        document=document, sequence=top + 1, event=event,
        remarks=remarks, created_by=user if user and user.is_authenticated else None,
    )


class TestDocumentListCreateView(EnvelopeMixin, ListCreateAPIView):
    """Create a test document WITHOUT starting a workflow.

    Kept separate from submit so the harness can exercise "document exists,
    no execution yet".
    """

    serializer_class = TestDocumentSerializer
    queryset = TestDocument.objects.prefetch_related('logs', 'flows')

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(CONFIG_KEY)]


class TestDocumentDetailView(APIView):
    def get_permissions(self):
        return [IsAuthenticated()]

    def get(self, request, pk):
        doc = (TestDocument.objects
               .prefetch_related('logs', 'flows')
               .filter(pk=pk).first())
        if doc is None:
            return fail('Document not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        return ok(TestDocumentSerializer(doc).data)


class TestDocumentSubmitView(APIView):
    """POST — the module-side submit: business row + engine start, one txn.

    This is the reference example of the responsibility boundary. The MODULE:
      * owns the document and its `flow_logs` history;
      * creates OR REUSES its single flow row;
      * decides whether a resubmission is allowed;
      * then invokes the engine.

    The engine performs ordinary selection and stage opening and has no idea
    whether this was a first submission or a resubmission.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(ACT_KEY)]

    def post(self, request, pk):
        from django.db import transaction

        doc = TestDocument.objects.filter(pk=pk).first()
        if doc is None:
            return fail('Document not found.',
                        status=http_status.HTTP_404_NOT_FOUND)

        module = WorkflowModule.objects.filter(code='TESTFLOW').first()
        if module is None:
            return fail(
                'The TESTFLOW module is not configured.',
                errors={'code': 'InvalidWorkflowConfiguration'},
                status=http_status.HTTP_409_CONFLICT,
            )

        try:
            with transaction.atomic():
                # Reuse the document's single flow row; create it only if the
                # document has never had one. A resubmission therefore runs on
                # the SAME row — the engine never makes a second.
                flow, fresh = TestFlow.objects.get_or_create(
                    document=doc, defaults={'company': doc.company},
                )
                resubmission = not fresh

                # MODULE policy lives here, not in the engine. This harness
                # allows resubmission only from a rejected execution.
                if resubmission and flow.status not in ('REJECTED', 'CANCELLED'):
                    raise wf_exc.WorkflowAlreadyRunning(
                        context={'flow_id': flow.pk, 'status': flow.status})

                _log_module_event(
                    doc,
                    TestDocumentLog.Event.RESUBMITTED if resubmission
                    else TestDocumentLog.Event.SUBMITTED,
                    user=request.user,
                    remarks=request.data.get('remarks', ''),
                )

                flow, task = engine.start(
                    module=module,
                    flow=flow,
                    document_key=doc.pk,
                    company=doc.company,
                    user=request.user,
                    context={'branch': doc.branch, 'department': doc.department,
                             'amount': str(doc.amount)},
                    ctx=_request_ctx(request),
                )
                doc.status = 'IN_APPROVAL'
                doc.save(update_fields=['status', 'updated_at'])
        except wf_exc.WorkflowError as exc:
            return _engine_error(exc)

        return created({
            'document_id': doc.pk,
            'resubmission': resubmission,
            'flow': TestFlowSerializer(flow).data,
            'task': WorkflowTaskSerializer(task).data,
        }, message='Workflow started.')
