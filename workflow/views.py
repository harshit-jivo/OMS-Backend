"""Workflow engine API — CONFIGURATION ONLY.

Authentication is the project default (JWT via `DEFAULT_AUTHENTICATION_CLASSES`);
nothing here defines its own. Authorisation uses `core.permissions.HasKey` with
keys registered in `core.permission_registry`: every endpoint in this module
requires `workflow.config.manage`.

There are no runtime endpoints. Approval tasks, actions and flow state belong
to the business module, which publishes its own API for them; this one manages
the configuration those modules are driven by. See `workflow/urls.py`.

Responses use the `core.responses` envelope ({success, message, data}), the
convention the newer apps standardise on.
"""
import logging

from django.db import IntegrityError
from django.db.models import Count, Q
from rest_framework import status as http_status
from rest_framework.generics import (
    ListCreateAPIView,
    RetrieveUpdateAPIView,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.permissions import HasKey
from core.responses import fail, ok
from workflow import exceptions as wf_exc
from workflow.models import (
    COMPANY_ALL,
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowUserReplacement,
)
from workflow.serializers import (
    WorkflowModuleSerializer,
    WorkflowQuerySerializer,
    WorkflowSerializer,
    WorkflowStageSerializer,
    WorkflowUserReplacementSerializer,
)
from workflow.services import assignments, conditions, replacements

logger = logging.getLogger(__name__)

CONFIG_KEY = 'workflow.config.manage'

#: Engine error -> HTTP status. Each error is explicit rather than collapsing
#: into a generic 400, so a client can branch on it.
ERROR_STATUS = {
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

def _active_only(request, qs):
    """Hide deactivated rows unless `?include_inactive=1`.

    Without the escape hatch a deactivated row could never be found again, so
    deactivation would be a one-way door rather than the reversible
    alternative to deleting that it is meant to be.
    """
    flag = (request.query_params.get('include_inactive') or '').lower()
    if flag not in {'1', 'true', 'yes'}:
        qs = qs.filter(is_active=True)
    return qs


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
    """Shared permission gate for the five configuration resources.

    NOTHING HERE IS DELETABLE. The detail views are Retrieve+Update only, so
    every one of them answers DELETE with 405. That is deliberate and it is
    enforced at the API, not left to the UI: every configuration row is
    referenced by flows, tasks and the append-only action history, and those
    foreign keys are ON DELETE RESTRICT. A delete therefore either fails with a
    ProtectedError or, where nothing references the row YET, quietly removes
    configuration someone is about to point a module at.

    `is_active` is the supported alternative: a deactivated row stops taking
    part in selection and disappears from the default listing, while every
    reference to it survives and it can be brought back. See `_active_only`.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(CONFIG_KEY)]


class ModuleListCreateView(_ConfigView, ListCreateAPIView):
    """The registry: which modules exist, and how much is configured on each.

    Every registered module is listed, always. There is no `is_active` on a
    module and so no `?include_inactive=1` here — the registry is a statement
    of what exists, and routing is stopped by deactivating a module's
    WORKFLOWS, which the Workflows tab does.

    Modules normally register THEMSELVES at deploy time (see
    `WorkflowConfig.ready`), so this is primarily a read endpoint; POST
    remains for the administrative case and takes `code` + `name` only.
    """

    serializer_class = WorkflowModuleSerializer

    def get_queryset(self):
        # Annotated so the list does not issue one COUNT per module.
        return WorkflowModule.objects.annotate(
            workflow_count=Count('workflows', distinct=True))


class ModuleDetailView(_ConfigView, RetrieveUpdateAPIView):
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
        company = (self.request.query_params.get('company') or '').upper()
        if company:
            # Filter for the UI's company selector. `ALL` workflows always
            # apply, so they are included for every specific company — the
            # same applicability rule selection uses, not a preference.
            qs = qs.filter(Q(company=COMPANY_ALL) | Q(company=company))
        return _active_only(self.request, qs)


class WorkflowDetailView(_ConfigView, RetrieveUpdateAPIView):
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
        return _active_only(self.request, qs)


class QueryDetailView(_ConfigView, RetrieveUpdateAPIView):
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
    """Stages, by workflow or by user.

    `?workflow=<id>` is the Stages tab's By Workflow view — the stages of one
    workflow, in sequence.

    `?user=<id>` is its By User view: every stage this person is the CONFIGURED
    actor for, across every module. That is a join over the three configuration
    tables and nothing more — no business module's documents or tasks are
    touched, because an administrator looking at assignments must not make
    every module load its runtime.
    """

    serializer_class = WorkflowStageSerializer

    def get_queryset(self):
        qs = (WorkflowStage.objects
              .select_related('user', 'workflow', 'workflow__module'))
        workflow = self.request.query_params.get('workflow')
        if workflow:
            qs = qs.filter(workflow_id=workflow)
        user = self.request.query_params.get('user')
        if user:
            qs = qs.filter(user_id=user).order_by(
                'workflow__module__code', 'workflow__code', 'sequence')
        return _active_only(self.request, qs)

    def get_serializer_context(self):
        """Resolve every row's effective user in ONE replacement query.

        Done here rather than per row: the serializer would otherwise issue a
        replacement lookup for each stage, which is the N+1 this view exists to
        avoid.
        """
        context = super().get_serializer_context()
        if self.request.method == 'GET':
            stages = list(self.get_queryset())
            context['effective_map'] = assignments._effective_map(
                stages, replacements.today())
        return context


class StageDetailView(_ConfigView, RetrieveUpdateAPIView):
    serializer_class = WorkflowStageSerializer
    queryset = WorkflowStage.objects.select_related('workflow', 'user')


# There is deliberately NO bulk "replace all assignments" endpoint.
#
# One existed briefly. It is gone because a single request that rewrites every
# stage a person owns, across every module, is an organisation-wide change with
# no natural review step — the blast radius is invisible at the moment of
# clicking, and there is no undo. Moving somebody's work is done one stage at a
# time through `PATCH /stages/<id>/`, where what changes is exactly what you
# were looking at.
#
# For a holiday, use `/replacements/` instead: dated, reversible, and it leaves
# the configuration alone.


class ReplacementListCreateView(_ConfigView, ListCreateAPIView):
    serializer_class = WorkflowUserReplacementSerializer

    def get_queryset(self):
        return _active_only(
            self.request,
            WorkflowUserReplacement.objects.select_related('old_user', 'new_user'))

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


class ReplacementDetailView(_ConfigView, RetrieveUpdateAPIView):
    serializer_class = WorkflowUserReplacementSerializer
    queryset = WorkflowUserReplacement.objects.select_related('old_user',
                                                              'new_user')


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# There is deliberately no runtime API here
# ---------------------------------------------------------------------------
#
# `/inbox/`, `/tasks/<pk>/`, `/tasks/<pk>/approve/`, `/tasks/<pk>/reject/`,
# `/flows/<module>/<id>/state/` and the whole `/testflow/` harness used to
# live below this line. They are gone because tasks, actions, flows and
# approval are owned by the BUSINESS MODULE, not by this engine.
#
# A module asks `workflow.services.selection.select_for_module()` which
# workflow applies and what its stages are, then exposes its own
# approve/reject endpoints over its own task model. Re-adding a generic
# `/tasks/<id>/approve/` here would put every module's runtime back into one
# shape, which is exactly what this refactor removed.
