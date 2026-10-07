"""Credit Limit API. Identity always comes from `request.user`; approval also
requires being the current stage's effective user. Responses use the project's
`{success, message, data}` envelope."""
import logging

from django.http import FileResponse
from rest_framework import status as http_status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.companies import COMPANY_CODES
from core.responses import created, fail, ok

from credit_limit import permissions as perms
from credit_limit.models import CreditLimitFlow, CreditLimitRequest, FlowStatus
from credit_limit.serializers import (
    CreditLimitActionLogSerializer,
    CreditLimitCreateSerializer,
    CreditLimitRequestSerializer,
    DecisionSerializer,
)
from credit_limit.services import flow as flow_service
from credit_limit.services import sap

logger = logging.getLogger(__name__)


def _queryset():
    return CreditLimitRequest.objects.select_related(
        'created_by', 'flow', 'flow__workflow', 'flow__current_stage',
        'flow__current_user')


def _filtered(request, qs):
    """`(qs, error)` for the optional `?company=` and `?status=` filters."""
    company = (request.query_params.get('company') or '').strip().upper()
    if company:
        if company not in COMPANY_CODES:
            return None, fail('`company` must be one of '
                              + ', '.join(COMPANY_CODES) + '.')
        qs = qs.filter(company=company)
    status = (request.query_params.get('status') or '').strip().upper()
    if status:
        if status not in FlowStatus.values:
            return None, fail('`status` must be one of '
                              + ', '.join(FlowStatus.values) + '.')
        qs = qs.filter(flow__status=status)
    return qs, None


def _readable(request, pk):
    """`(obj, error)`: own requests, or any request for an approver."""
    obj = _queryset().filter(pk=pk).first()
    if obj is None or (obj.created_by_id != request.user.pk
                       and not perms.has_approval_access(request.user)):
        return None, fail('Request not found.',
                          status=http_status.HTTP_404_NOT_FOUND)
    return obj, None


def customer_payload(card):
    return {**card, 'balance': str(card['balance']),
            'credit_limit': str(card['credit_limit'])}


class CustomerView(APIView):
    """One customer's live credit facts from SAP, for the request form."""

    def get_permissions(self):
        return [IsAuthenticated(), perms.CanRaiseRequests()]

    def get(self, request, card_code):
        company = (request.query_params.get('company') or '').strip().upper()
        if company not in COMPANY_CODES:
            return fail('`company` must be one of '
                        + ', '.join(COMPANY_CODES) + '.')
        try:
            card = sap.customer(company, card_code)
        except sap.SapUnavailable as exc:
            return fail(str(exc), status=http_status.HTTP_502_BAD_GATEWAY)
        if card is None:
            return fail(f'SAP has no customer {card_code} in {company}.',
                        status=http_status.HTTP_404_NOT_FOUND)
        return ok(customer_payload(card))


class RequestListCreateView(APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        return [IsAuthenticated(), perms.CanRaiseRequests()]

    def get(self, request):
        qs, error = _filtered(request,
                              _queryset().filter(created_by=request.user))
        if error:
            return error
        return ok(CreditLimitRequestSerializer(qs, many=True).data)

    def post(self, request):
        serializer = CreditLimitCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return fail('Please correct the highlighted fields.',
                        errors=serializer.errors)
        try:
            obj = flow_service.create(user=request.user,
                                      **serializer.validated_data)
        except flow_service.CreditLimitError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)
        return created(CreditLimitRequestSerializer(
            _queryset().get(pk=obj.pk)).data,
            message='Credit limit request submitted.')


class RequestDetailView(APIView):
    def get_permissions(self):
        return [IsAuthenticated(), perms.CanReadRequests()]

    def get(self, request, pk):
        obj, error = _readable(request, pk)
        if error:
            return error
        return ok(CreditLimitRequestSerializer(obj).data)


class RequestHistoryView(APIView):
    """The action log plus every stage's progress."""

    def get_permissions(self):
        return [IsAuthenticated(), perms.CanReadRequests()]

    def get(self, request, pk):
        obj, error = _readable(request, pk)
        if error:
            return error
        logs = obj.action_logs.select_related('acted_by', 'stage')
        return ok({
            'actions': CreditLimitActionLogSerializer(logs, many=True).data,
            'stages': flow_service.progress(obj),
        })


class AttachmentView(APIView):
    def get_permissions(self):
        return [IsAuthenticated(), perms.CanReadRequests()]

    def get(self, request, pk):
        obj, error = _readable(request, pk)
        if error:
            return error
        if not obj.attachment:
            return fail('This request has no attachment.',
                        status=http_status.HTTP_404_NOT_FOUND)
        name = obj.attachment.name.rsplit('/', 1)[-1]
        return FileResponse(obj.attachment.open('rb'), filename=name)


class ApprovalQueueView(APIView):
    """Requests awaiting THIS user's decision."""

    def get_permissions(self):
        return [IsAuthenticated(), perms.CanOpenApprovalDesk()]

    def get(self, request):
        qs, error = _filtered(request, _queryset().filter(
            pk__in=flow_service.pending_request_ids(request.user)))
        if error:
            return error
        return ok(CreditLimitRequestSerializer(qs, many=True).data)


class ApprovalHistoryView(APIView):
    """Requests this user has already decided a stage of."""

    def get_permissions(self):
        return [IsAuthenticated(), perms.CanOpenApprovalDesk()]

    def get(self, request):
        qs, error = _filtered(request, _queryset().filter(
            pk__in=flow_service.decided_request_ids(request.user)))
        if error:
            return error
        return ok(CreditLimitRequestSerializer(qs, many=True).data)


class _DecisionView(APIView):
    def get_permissions(self):
        return [IsAuthenticated(), perms.CanOpenApprovalDesk()]

    def _load(self, request, pk):
        flow = (CreditLimitFlow.objects.select_related('request')
                .filter(request_id=pk).first())
        if flow is None:
            return None, fail('Request not found.',
                              status=http_status.HTTP_404_NOT_FOUND)
        allowed, reason = flow_service.may_act_on(request.user, flow)
        if not allowed:
            return None, fail(reason, status=http_status.HTTP_403_FORBIDDEN)
        return flow, None

    def _remarks(self, request):
        payload = DecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return payload.validated_data['remarks']


class ApproveView(_DecisionView):
    def post(self, request, pk):
        flow, error = self._load(request, pk)
        if error:
            return error
        try:
            flow = flow_service.approve(flow, user=request.user,
                                        remarks=self._remarks(request))
        except sap.SapWriteError as exc:
            # The approval rolled back; record why now that the lock is gone.
            sap.record_failure(flow, exc)
            return fail(str(exc), errors={'sap': exc.response},
                        status=http_status.HTTP_502_BAD_GATEWAY)
        except flow_service.CreditLimitError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)
        message = ('Approved. The new credit limit is set in SAP.'
                   if flow.status == FlowStatus.APPROVED
                   else 'Approved. The request has moved to the next stage.')
        return ok(CreditLimitRequestSerializer(
            _queryset().get(pk=pk)).data, message=message)


class RejectView(_DecisionView):
    def post(self, request, pk):
        flow, error = self._load(request, pk)
        if error:
            return error
        remarks = self._remarks(request)
        if not remarks.strip():
            return fail('A reason is required when rejecting a request.',
                        errors={'remarks': ['This field is required.']})
        try:
            flow_service.reject(flow, user=request.user, remarks=remarks)
        except flow_service.CreditLimitError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)
        return ok(CreditLimitRequestSerializer(
            _queryset().get(pk=pk)).data, message='Request rejected.')
