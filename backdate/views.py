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
    return Q(**{field: raw}), None


def _search_filter(request):
    """`Q` for an optional `?search=` — a BackDate id, or a SAP user.

    Two fields because those are the two ways a person refers to one of these:
    by the number on the screen, or by whose SAP login the rights are for. A
    term that is entirely digits matches the id EXACTLY as well as appearing in
    a SAP username, so searching "82" finds request 82 and does not bury it
    under every id containing 82.
    """
    term = (request.query_params.get('search') or '').strip()
    if not term:
        return Q()
    matches = Q(sap_username__icontains=term)
    if term.isdigit():
        matches |= Q(pk=int(term))
    return matches


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


def _ctx(request):
    """Serializer context. The read serializer needs the CALLER to answer
    `can_edit` — who may edit depends on who is asking, so a serializer with no
    request in it reports False rather than guessing."""
    return {'request': request}


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
        qs = qs.filter(company).filter(_search_filter(request))

        # `COMPLETED` is handled here too — see `flow_service.status_q`. One
        # definition, so the list and the card above it agree on the word.
        condition = flow_service.status_q(
            request.query_params.get('status'), prefix='flow__')
        if condition is not None:
            qs = qs.filter(condition)

        month = (request.query_params.get('month') or '').strip()
        if month:
            parsed = _parse_month(month)
            if parsed is None:
                return fail('`month` must be MM-YYYY, e.g. 09-2026.')
            year, mon = parsed
            qs = qs.filter(created_at__year=year, created_at__month=mon)

        return ok(BackDateSerializer(qs, many=True, context=_ctx(request)).data)

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
                # The reason travels to the CREATE log, not to a column.
                flow_service.submit(
                    backdate, user=request.user,
                    remarks=serializer.validated_data.get('remarks', ''))
        except flow_service.BackDateError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        payload = BackDateSerializer(_queryset().get(pk=backdate.pk),
                                    context=_ctx(request)).data
        return created(payload, message='BackDate request submitted.')


class RequestDetailView(APIView):
    """Read one request, or edit it while it is still pending.

    READING takes either key — an approver has to be able to open what they
    are deciding, and they usually do not hold `BackDate`. EDITING is still
    the requester's alone, checked in `patch`.
    """

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanReadRequests()]

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
        return ok(BackDateSerializer(obj, context=_ctx(request)).data)

    def patch(self, request, pk):
        """Edit a pending request, recording exactly what changed.

        Who may edit, and when, is `permissions.may_edit` — one rule, shared
        with the serializer so the UI never offers an Edit control the server
        is going to refuse.
        """
        obj, error = self._load(request, pk)
        if error:
            return error

        allowed, reason, why = bkdt_perms.may_edit(request.user, obj)
        if not allowed:
            # "not yours" and "too late" are different answers and get
            # different statuses, so a client can tell them apart without
            # reading the prose.
            return fail(reason, status=(
                http_status.HTTP_409_CONFLICT if why == bkdt_perms.TOO_LATE
                else http_status.HTTP_403_FORBIDDEN))

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
            remarks = serializer.validated_data.get('remarks', '')
            # An UPDATE row is written when anything changed OR when the user
            # gave a reason. Those are two different edits: correcting a date,
            # and explaining a request without altering it (the usual answer to
            # a SAP refusal an approver is puzzling over). Writing nothing for
            # the second would lose the only thing the user actually said.
            if changes or remarks.strip():
                flow_service.log(
                    backdate, action=LogAction.UPDATE, user=request.user,
                    remarks=remarks, action_data=changes)

        payload = BackDateSerializer(_queryset().get(pk=backdate.pk),
                                    context=_ctx(request)).data
        return ok(payload,
                  message=('Request updated.' if changes
                           else 'Remark added.' if remarks.strip()
                           else 'Nothing changed.'))


class RequestHistoryView(APIView):
    """The append-only history for one request, plus its stage progress."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanReadRequests()]

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
            'stages': _progress(obj, logs),
        })


def _progress(backdate, logs):
    """Every stage of this request's workflow, decided or not yet reached.

    The action log alone cannot answer "where is this?", because it only
    records what has HAPPENED — a request waiting at stage 1 of 3 has one row
    and says nothing about the two stages ahead. The stages come from the
    engine, the decisions from the log, and they are joined here.

    Users are the CURRENT effective ones, resolved per stage: a reviewer who
    has not acted yet is whoever would act today, not whoever was configured
    when the request was raised.
    """
    flow = getattr(backdate, 'flow', None)
    if flow is None:
        return []

    decided = {}
    for entry in logs:
        if entry.action in (LogAction.APPROVE, LogAction.REJECT) and entry.stage_id:
            decided[entry.stage_id] = entry

    from workflow.services.assignments import get_stage_assignment

    rows = []
    for stage in flow_service.stages_for(flow):
        entry = decided.get(stage.id)
        assignment = get_stage_assignment(stage.id)
        if entry is not None:
            status = ('APPROVED' if entry.action == LogAction.APPROVE
                      else 'REJECTED')
        elif flow.current_stage_id == stage.id:
            status = 'AWAITING'
        elif flow.status == FlowStatus.PENDING:
            status = 'UPCOMING'
        else:
            # The flow ended before this stage had its turn — a rejection
            # upstream. "Upcoming" would imply it is still going to happen.
            status = 'SKIPPED'

        rows.append({
            'stage_id': stage.id,
            'sequence': stage.sequence,
            'stage_name': stage.name,
            'status': status,
            'reviewer': (assignment.effective_username if assignment
                         else ''),
            'configured_reviewer': (assignment.configured_username
                                    if assignment else ''),
            'has_active_replacement': bool(
                assignment and assignment.has_active_replacement),
            'acted_by': entry.acted_by.username if entry and entry.acted_by
                        else '',
            'acted_at': entry.acted_at if entry else None,
            'remarks': entry.remarks if entry else '',
        })
    return rows


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
        qs = qs.filter(company).filter(_search_filter(request))

        month = (request.query_params.get('month') or '').strip()
        if month:
            parsed = _parse_month(month)
            if parsed is None:
                return fail('`month` must be MM-YYYY, e.g. 09-2026.')
            year, mon = parsed
            qs = qs.filter(created_at__year=year, created_at__month=mon)

        counts = {'pending': 0, 'approved': 0, 'rejected': 0, 'completed': 0}
        for status, hana in qs.values_list('flow__status', 'flow__hana_status'):
            if status == FlowStatus.PENDING:
                counts['pending'] += 1
            elif status == FlowStatus.APPROVED:
                counts['approved'] += 1
                # A SUBSET of approved, deliberately not a fourth bucket: the
                # two cards answer different questions ("who said yes" and
                # "does the user actually have the rights"), and since SAP
                # became the gate they differ only for requests approved under
                # the old order. Those are the ones worth finding.
                if hana == HanaStatus.SUCCESS:
                    counts['completed'] += 1
            elif status == FlowStatus.REJECTED:
                counts['rejected'] += 1
        # `completed` is NOT added in: it is already counted in `approved`, and
        # a total that double-counted it would not match the list below.
        counts['total'] = (counts['pending'] + counts['approved']
                           + counts['rejected'])
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
        qs = _queryset().filter(company, _search_filter(request),
                                pk__in=_pending_ids(request.user))
        return ok(BackDateSerializer(qs, many=True, context=_ctx(request)).data)


class ApprovalHistoryView(APIView):
    """Requests this user has already decided, approved or rejected."""

    def get_permissions(self):
        return [IsAuthenticated(), bkdt_perms.CanOpenApprovalDesk()]

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error)
        ids = flow_service.decided_backdate_ids(
            request.user, request.query_params.get('status'))
        qs = _queryset().filter(company, _search_filter(request),
                                pk__in=ids)
        return ok(BackDateSerializer(qs, many=True, context=_ctx(request)).data)


def _pending_ids(user):
    """Requests whose current stage this user is the effective approver for."""
    return list(flow_service.pending_for(user)
                .values_list('backdate_id', flat=True))


class ApprovalInsightsView(APIView):
    """Counts for the approval desk — the KPI cards above its list.

    `pending` is what is waiting on this user RIGHT NOW (the queue), while
    `approved` and `rejected` are what they have already decided. Those three
    are disjoint, so `total` is their sum: a request cannot be both awaiting
    this user's decision and already carrying it.

    `completed` is NOT in that sum. It counts the approved ones whose rights
    actually reached SAP — a subset of `approved`, not a fourth state.
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
            return BackDate.objects.filter(
                company, _search_filter(request), pk__in=ids).count()

        counts = {
            'pending': count(_pending_ids(request.user)),
            'approved': count(flow_service.decided_backdate_ids(
                request.user, FlowStatus.APPROVED)),
            'rejected': count(flow_service.decided_backdate_ids(
                request.user, FlowStatus.REJECTED)),
            # Approved AND the grant is in SAP — a subset of `approved`.
            'completed': count(flow_service.decided_backdate_ids(
                request.user, flow_service.COMPLETED)),
        }
        # The three disjoint states only; `completed` is inside `approved`.
        counts['total'] = (counts['pending'] + counts['approved']
                           + counts['rejected'])
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
        except hana_service.HanaWriteError as exc:
            # The last stage calls SAP BEFORE approving, so a refusal means
            # nothing was approved. The request is still sitting at this
            # stage; the recorded SAP response says why.
            #
            # Recorded HERE, not inside the service: the approval transaction
            # has rolled back by the time this line runs, which is exactly what
            # makes this write survive — and what lets it take the row lock
            # that transaction was holding.
            hana_service.record_failure(flow, exc)
            flow.refresh_from_db()
            return fail(str(exc),
                        errors={'sap': _sap_detail(flow)},
                        status=http_status.HTTP_502_BAD_GATEWAY)
        except flow_service.BackDateError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        flow.refresh_from_db()
        return ok({
            'flow_id': flow.pk,
            'flow_status': flow.status,
            'current_stage': flow.current_stage_id,
            'current_user': flow.current_user_id,
            'hana_status': flow.hana_status,
            'hana_status_text': flow.hana_status_text,
            'hana_applied': flow.status == FlowStatus.APPROVED or None,
        }, message=_approve_message(flow))


def _sap_detail(flow):
    """What the client needs to show the operator the SAP refusal."""
    return {
        'hana_status': flow.hana_status,
        'hana_status_text': flow.hana_status_text,
        'sap_payload': flow.sap_payload,
    }


def _approve_message(flow):
    if flow.status == FlowStatus.PENDING:
        return 'Approved. The request has moved to the next stage.'
    # "Approved" now means the grant is in SAP: the write happened first and
    # the status was only set because it succeeded.
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

        # An approved flow whose write failed is impossible now — SAP is the
        # gate on the final approval — but a retry still exists for a flow that
        # was approved under the OLD order, and for an operator re-running a
        # write after fixing something in SAP itself.
        if flow.status != FlowStatus.APPROVED:
            return fail(
                'Only a fully approved request can be written to SAP. A '
                'request refused by SAP is still awaiting its last approval — '
                'correct it and approve again.',
                status=http_status.HTTP_409_CONFLICT)
        if flow.hana_status == HanaStatus.SUCCESS:
            return fail('These rights have already been applied in SAP.',
                        status=http_status.HTTP_409_CONFLICT)

        try:
            text = hana_service.apply_grant(flow)
        except hana_service.HanaWriteError as exc:
            hana_service.record_failure(flow, exc)
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
