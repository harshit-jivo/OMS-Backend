"""BackDate (BKDT) API.

Every endpoint requires authentication and a permission key. Identity always
comes from `request.user`; no endpoint accepts a user id from the client for an
authorisation decision. That is the single largest difference from JSAP, where
all 22 endpoints were reachable anonymously and the approver's identity was a
field in the request body.

Approval additionally requires being the current effective stage user — see
`backdate/permissions.py`. Holding the key opens the desk; it does not decide
anything.

Decisions are addressed by REQUEST, not by task: there is no task table, so
`/requests/<pk>/approve/` acts on whatever stage that request is waiting at.

Responses use the project's `{success, message, data}` envelope.
"""
import logging
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.companies import COMPANY_CODES
from core.permissions import HasKey
from core.responses import created, fail, ok

from backdate import permissions as bkdt_perms
from backdate.models import (
    BackDate,
    BackDateActionLog,
    BackDateFlow,
    FlowStatus,
    HanaStatus,
    LogAction,
)
from backdate.serializers import (
    ApprovalDecisionSerializer,
    BackDateActionLogSerializer,
    BackDateCreateSerializer,
    BackDateSerializer,
    BackDateUpdateSerializer,
    diff,
    snapshot,
)
from backdate.services import flow as flow_service
from backdate.services import hana as hana_service
from backdate.services import sap_masters

logger = logging.getLogger(__name__)


def _company_filter(request, field='company'):
    """`(Q, error)` for an optional `?company=` filter.

    One helper rather than four copies, because the requester list, the
    counts and both approval lists all offer the same filter and must agree on
    what an unknown value means: a rejected request, not a silently unfiltered
    one. Quietly ignoring `?company=OILL` would show every company under a
    label saying OIL.
    """
    raw = (request.query_params.get('company') or '').strip().upper()
    if not raw:
        return Q(), None
    if raw not in COMPANY_CODES:
        return None, ('`company` must be one of '
                      + ', '.join(COMPANY_CODES) + '.')
    # A request's `company` is a SET now — `'OIL,BEVERAGES'` — so the filter
    # asks whether the named company is IN it rather than whether it IS it.
    # Spelt out as four exact positions rather than one bare `contains`, which
    # would let a company match another whose name merely contained it.
    return (Q(**{field: raw})
            | Q(**{f'{field}__startswith': f'{raw},'})
            | Q(**{f'{field}__endswith': f',{raw}'})
            | Q(**{f'{field}__contains': f',{raw},'})), None


def _queryset():
    """Requests with everything the read serializer needs, in one round trip.

    The stage NAME and the current user come through the flow's own relations
    rather than columns on BKDT rows — that is the whole point of holding the
    stage id — so they are selected here instead.
    """
    return (
        BackDate.objects
        .select_related('created_by', 'flow', 'flow__workflow',
                        'flow__current_stage', 'flow__current_user')
    )


# ---------------------------------------------------------------------------
# SAP master data
# ---------------------------------------------------------------------------

class SapUserListView(APIView):
    """SAP logins for one company, for the request form's picker."""

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(bkdt_perms.REQUEST_KEY)]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        try:
            return ok(sap_masters.sap_users(company))
        except sap_masters.MasterDataError as exc:
            return fail(str(exc))


class DocumentTypeListView(APIView):
    """SAP object types for one company."""

    def get_permissions(self):
        return [IsAuthenticated(), HasKey(bkdt_perms.REQUEST_KEY)]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        try:
            return ok(sap_masters.document_types(company))
        except sap_masters.MasterDataError as exc:
            return fail(str(exc))


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class RequestListCreateView(APIView):
    """List the caller's own requests, or raise a new one."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanRaiseRequests()]

    def get(self, request):
        # Scoped to the caller. JSAP took `userId` from the query string, so
        # any caller could read anyone's backdate history.
        qs = _queryset().filter(created_by=request.user)

        company, error = _company_filter(request)
        if error:
            return fail(error)
        qs = qs.filter(company)

        status_filter = (request.query_params.get('status') or '').upper()
        if status_filter in FlowStatus.values:
            qs = qs.filter(flow__status=status_filter)

        month = (request.query_params.get('month') or '').strip()
        if month:
            parsed = _parse_month(month)
            if parsed is None:
                return fail('`month` must be MM-YYYY, e.g. 09-2026.')
            year, mon = parsed
            qs = qs.filter(created_at__year=year, created_at__month=mon)

        return ok(BackDateSerializer(qs, many=True).data)

    def post(self, request):
        serializer = BackDateCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return fail('Please correct the highlighted fields.',
                        errors=serializer.errors)

        try:
            # The business row and its routing commit together, so a request
            # that can never be routed is never stored. JSAP created the row
            # first and left it orphaned when no template matched — silently.
            with transaction.atomic():
                # `created_by` from the session, never the payload.
                backdate = serializer.save(created_by=request.user)
                flow_service.submit(backdate, user=request.user)
        except flow_service.BackDateError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        payload = BackDateSerializer(_queryset().get(pk=backdate.pk)).data
        return created(payload, message='BackDate request submitted.')


class RequestDetailView(APIView):
    """Read one request, or edit it while it is still pending."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanRaiseRequests()]

    def _load(self, request, pk):
        obj = _queryset().filter(pk=pk).first()
        if obj is None:
            return None, fail('Request not found.',
                              status=http_status.HTTP_404_NOT_FOUND)
        # Own requests, unless the caller also runs the approval desk — an
        # approver has to be able to open what they are deciding.
        if (obj.created_by_id != request.user.pk
                and not bkdt_perms.has_approval_access(request.user)):
            return None, fail('Request not found.',
                              status=http_status.HTTP_404_NOT_FOUND)
        return obj, None

    def get(self, request, pk):
        obj, error = self._load(request, pk)
        if error:
            return error
        return ok(BackDateSerializer(obj).data)

    def patch(self, request, pk):
        """Edit a pending request, recording exactly what changed.

        Only the REQUESTER may edit, and only while nothing has been decided —
        an approver agreed to the request in front of them, and letting the
        dates move underneath an approval already given would make the record
        untrue. An approver who wants different terms rejects; the requester
        raises a new one.
        """
        obj, error = self._load(request, pk)
        if error:
            return error

        if obj.created_by_id != request.user.pk:
            return fail('Only the requester can edit this request.',
                        status=http_status.HTTP_403_FORBIDDEN)

        flow = getattr(obj, 'flow', None)
        if flow is not None and flow.status != FlowStatus.PENDING:
            return fail(
                f'This request is already {flow.get_status_display().lower()} '
                f'and can no longer be edited.',
                status=http_status.HTTP_409_CONFLICT)
        if flow is not None and BackDateActionLog.objects.filter(
                backdate=obj, action=LogAction.APPROVE).exists():
            return fail(
                'This request has already been approved at one of its stages '
                'and can no longer be edited.',
                status=http_status.HTTP_409_CONFLICT)

        serializer = BackDateUpdateSerializer(obj, data=request.data,
                                              partial=True)
        if not serializer.is_valid():
            return fail('Please correct the highlighted fields.',
                        errors=serializer.errors)

        with transaction.atomic():
            # Read BEFORE, apply, diff, log — in that order, in one
            # transaction, so the log can never describe an edit that did not
            # commit or miss one that did.
            before = snapshot(obj)
            backdate = serializer.save()
            after = snapshot(backdate)
            changes = diff(before, after)
            if changes:
                flow_service.log(
                    backdate, action=LogAction.UPDATE, user=request.user,
                    remarks=(request.data.get('log_remarks') or '')[:2000],
                    action_data=changes)

        payload = BackDateSerializer(_queryset().get(pk=backdate.pk)).data
        return ok(payload,
                  message=('Request updated.' if changes
                           else 'Nothing changed.'))


class RequestHistoryView(APIView):
    """The append-only history for one request."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanRaiseRequests()]

    def get(self, request, pk):
        obj = BackDate.objects.filter(pk=pk).first()
        if obj is None:
            return fail('Request not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        if (obj.created_by_id != request.user.pk
                and not bkdt_perms.has_approval_access(request.user)):
            return fail('Request not found.',
                        status=http_status.HTTP_404_NOT_FOUND)

        logs = list(obj.action_logs.select_related('acted_by')
                    .order_by('acted_at', 'id'))
        # Stage names resolved once for the whole list rather than per row —
        # they are not stored on the log, deliberately, so that a renamed
        # stage reads correctly everywhere.
        from backdate.serializers import _stage_names
        context = {'stage_names': _stage_names(l.stage_id for l in logs)}
        return ok({
            'actions': BackDateActionLogSerializer(
                logs, many=True, context=context).data,
        })


class InsightsView(APIView):
    """Counts for the caller's own requests."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanRaiseRequests()]

    def get(self, request):
        qs = BackDate.objects.filter(created_by=request.user)

        # The counts follow the same filter as the list they head, or the card
        # and the table below it would disagree about how many there are.
        company, error = _company_filter(request)
        if error:
            return fail(error)
        qs = qs.filter(company)

        month = (request.query_params.get('month') or '').strip()
        if month:
            parsed = _parse_month(month)
            if parsed is None:
                return fail('`month` must be MM-YYYY, e.g. 09-2026.')
            year, mon = parsed
            qs = qs.filter(created_at__year=year, created_at__month=mon)

        counts = {'pending': 0, 'approved': 0, 'rejected': 0}
        for value in qs.values_list('flow__status', flat=True):
            if value == FlowStatus.PENDING:
                counts['pending'] += 1
            elif value == FlowStatus.APPROVED:
                counts['approved'] += 1
            elif value == FlowStatus.REJECTED:
                counts['rejected'] += 1
        counts['total'] = sum(counts.values())
        return ok(counts)


# ---------------------------------------------------------------------------
# Approval desk
# ---------------------------------------------------------------------------

class ApprovalQueueView(APIView):
    """Requests awaiting THIS user's decision.

    Membership is resolved from the workflow stage's current effective user,
    not from anything stored on the flow — so a stage reassignment or an active
    replacement changes this queue with no write in this module.
    """

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanOpenApprovalDesk()]

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error)
        qs = _queryset().filter(company, pk__in=_pending_ids(request.user))
        return ok(BackDateSerializer(qs, many=True).data)


class ApprovalHistoryView(APIView):
    """Requests this user has already decided, approved or rejected."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanOpenApprovalDesk()]

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error)
        status_filter = (request.query_params.get('status') or '').upper()
        ids = flow_service.decided_backdate_ids(
            request.user,
            status_filter if status_filter in FlowStatus.values else None)
        qs = _queryset().filter(company, pk__in=ids)
        return ok(BackDateSerializer(qs, many=True).data)


def _pending_ids(user):
    """Requests whose current stage this user is the effective approver for."""
    return list(flow_service.pending_for(user)
                .values_list('backdate_id', flat=True))


class ApprovalInsightsView(APIView):
    """Counts for the approval desk — the KPI cards above its list.

    `pending` is what is waiting on this user RIGHT NOW (the queue), while
    `approved` and `rejected` are what they have already decided. The three are
    disjoint, so `total` is their sum: a request cannot be both awaiting this
    user's decision and already carrying it.
    """

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanOpenApprovalDesk()]

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error)

        def count(ids):
            if not ids:
                return 0
            return BackDate.objects.filter(company, pk__in=ids).count()

        counts = {
            'pending': count(_pending_ids(request.user)),
            'approved': count(flow_service.decided_backdate_ids(
                request.user, FlowStatus.APPROVED)),
            'rejected': count(flow_service.decided_backdate_ids(
                request.user, FlowStatus.REJECTED)),
        }
        counts['total'] = sum(counts.values())
        return ok(counts)


class _DecisionView(APIView):
    """Shared gate for approve/reject: permission AND effective stage user."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanOpenApprovalDesk()]

    def _load(self, request, pk):
        flow = (BackDateFlow.objects
                .select_related('backdate', 'current_stage')
                .filter(backdate_id=pk).first())
        if flow is None:
            return None, fail('Request not found.',
                              status=http_status.HTTP_404_NOT_FOUND)

        allowed, reason = bkdt_perms.may_act_on(request.user, flow)
        if not allowed:
            return None, fail(reason, status=http_status.HTTP_403_FORBIDDEN)
        return flow, None


class RequestApproveView(_DecisionView):
    def post(self, request, pk):
        flow, error = self._load(request, pk)
        if error:
            return error

        payload = ApprovalDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            flow = flow_service.approve(
                flow, user=request.user,
                remarks=payload.validated_data.get('remarks', ''))
        except flow_service.BackDateError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        hana_text = ''
        hana_ok = None
        if flow.status == FlowStatus.APPROVED:
            # Final approval is what grants the rights in SAP. The approval
            # itself is already committed, so a HANA failure does NOT undo it —
            # it is reported, recorded, and retryable.
            hana_ok = True
            try:
                hana_text = hana_service.apply_grant(flow)
            except hana_service.HanaWriteError as exc:
                hana_ok = False
                hana_text = str(exc)

        flow.refresh_from_db()
        return ok({
            'flow_id': flow.pk,
            'flow_status': flow.status,
            'current_stage': flow.current_stage_id,
            'current_user': flow.current_user_id,
            'hana_status': flow.hana_status,
            'hana_status_text': hana_text or flow.hana_status_text,
            'hana_applied': hana_ok,
        }, message=_approve_message(flow, hana_ok))


def _approve_message(flow, hana_ok):
    if flow.status == FlowStatus.PENDING:
        return 'Approved. The request has moved to the next stage.'
    if hana_ok is False:
        # Deliberately not "success". JSAP reported 200/Success=true here even
        # when the SAP write never happened.
        return ('Approved, but the rights could not be applied in SAP. '
                'An administrator can retry the SAP write.')
    return 'Approved. The back-posting rights have been applied in SAP.'


class RequestRejectView(_DecisionView):
    def post(self, request, pk):
        flow, error = self._load(request, pk)
        if error:
            return error

        payload = ApprovalDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        remarks = payload.validated_data.get('remarks', '')

        # A missing reason is a VALIDATION failure (400), not a state conflict
        # (409) — the request is fine, the payload is not. The service keeps
        # its own guard as the invariant; this one gets the status right.
        if not remarks.strip():
            return fail('A reason is required when rejecting a request.',
                        errors={'remarks': ['This field is required.']},
                        status=http_status.HTTP_400_BAD_REQUEST)

        try:
            flow = flow_service.reject(flow, user=request.user,
                                       remarks=remarks)
        except flow_service.BackDateError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        return ok({'flow_id': flow.pk, 'flow_status': flow.status},
                  message='Request rejected.')


class RetryHanaView(APIView):
    """Re-attempt the SAP write for an already-approved request.

    This is the ONLY path to HANA that is not a final approval, and it is
    deliberately narrow: the flow must already be fully approved, and the
    caller must hold the approval key.

    JSAP's equivalent (`BackDateSaveInHana?flowId=`) required neither — it
    re-applied rights for any flow id, approved or not, with no authentication
    at all. `SaveBKDT`, which wrote arbitrary rights with no request and no
    approval, is not migrated.
    """

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanOpenApprovalDesk()]

    def post(self, request, pk):
        flow = (BackDateFlow.objects
                .select_related('backdate').filter(backdate_id=pk).first())
        if flow is None:
            return fail('Request not found.',
                        status=http_status.HTTP_404_NOT_FOUND)

        if flow.status != FlowStatus.APPROVED:
            return fail(
                'Only a fully approved request can be written to SAP.',
                status=http_status.HTTP_409_CONFLICT)
        if flow.hana_status == HanaStatus.SUCCESS:
            return fail('These rights have already been applied in SAP.',
                        status=http_status.HTTP_409_CONFLICT)

        try:
            text = hana_service.apply_grant(flow)
        except hana_service.HanaWriteError as exc:
            return fail(str(exc), status=http_status.HTTP_502_BAD_GATEWAY)

        return ok({'flow_id': flow.pk, 'hana_status': flow.hana_status,
                   'hana_status_text': text},
                  message='Rights applied in SAP.')


def _parse_month(value):
    """`MM-YYYY` -> `(year, month)`, or None.

    One explicit format, parsed with no locale involved. JSAP accepted eight
    formats and tried the SERVER's culture first, so `03-04-2026` meant a
    different month depending on where it ran.
    """
    try:
        parsed = datetime.strptime(value, '%m-%Y')
    except (ValueError, TypeError):
        return None
    return parsed.year, parsed.month
