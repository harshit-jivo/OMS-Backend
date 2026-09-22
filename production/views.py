"""PRDO API.

Every endpoint requires authentication and a permission key. Identity always
comes from `request.user`; no endpoint accepts a user id from the client for an
authorisation decision. JSAP's production endpoints took the approver's id from
the request body.

Approval additionally requires being the current effective stage user — see
`production/permissions.py`. Holding the key opens the desk; it does not decide
anything.

THERE IS NO CREATE AND NO EDIT. SAP is the origin: a production order enters
OMS through `manage.py sync_production_orders` and nothing else. That absence
is the design, not an omission.

Responses use the project's `{success, message, data}` envelope.
"""
import logging

from django.db.models import Count, Max, Q
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.companies import COMPANY_CODES
from core.responses import fail, ok

from production import permissions as prdo_perms
from production.models import (
    FlowStatus,
    LogAction,
    ProductionOrder,
    ProductionOrderFlow,
    SapWriteStatus,
)
from production.serializers import (
    ActionLogSerializer,
    DecisionSerializer,
    ProductionOrderSerializer,
    RejectionSerializer,
)
from production.services import flow as flow_service
from production.services import gate as gate_service
from production.services import sap as sap_service
from production.services import sync as sync_service

logger = logging.getLogger(__name__)


def _queryset():
    return (ProductionOrder.objects
            .select_related('flow', 'flow__workflow', 'flow__current_stage',
                            'flow__current_user')
            .all())


def _company_filter(request):
    """`(Q, error)` for an optional `?company=` filter.

    An unrecognised value is rejected rather than ignored: quietly ignoring
    `?company=OILL` would show every company under a heading that says one.
    """
    raw = (request.query_params.get('company') or '').strip().upper()
    if not raw:
        return Q(), None
    if raw not in COMPANY_CODES:
        return None, ('`company` must be one of '
                      + ', '.join(sorted(COMPANY_CODES)))
    return Q(company=raw), None


def _status_filter(request, field='flow__status'):
    raw = (request.query_params.get('status') or '').strip().upper()
    if not raw:
        return Q(), None
    valid = {c for c, _ in FlowStatus.choices}
    if raw not in valid:
        return None, '`status` must be one of ' + ', '.join(sorted(valid))
    return Q(**{field: raw}), None


class _Base(APIView):
    # `HasKey` is PARAMETERISED, so it must be instantiated in
    # `get_permissions()` rather than listed in `permission_classes` —
    # DRF calls `permission()` on every entry of that list, and a
    # HasKey instance is not callable. `IsProductionApprover` below is
    # a plain class and stays in `permission_classes`.
    def get_permissions(self):
        return [IsAuthenticated(), prdo_perms.CanViewRequests()]


class RequestListView(_Base):
    """Production orders OMS knows about.

    NOTE: this list is NOT scoped by company. OMS has no user->company map
    that scopes documents (`core/companies.py` says so, and
    `payments.user_companies` returns every company to everyone), so `?company=`
    is a filter the caller chooses. ACTING is scoped, by stage. If viewing must
    be scoped too, that is shared `core` work — see PRDO_DESIGN.md §1.3.
    """

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error, status=http_status.HTTP_400_BAD_REQUEST)
        state, error = _status_filter(request)
        if error:
            return fail(error, status=http_status.HTTP_400_BAD_REQUEST)

        qs = _queryset().filter(company).filter(state)
        item = (request.query_params.get('item_code') or '').strip()
        if item:
            qs = qs.filter(item_code__icontains=item)

        return ok(ProductionOrderSerializer(qs[:500], many=True).data)


class RequestDetailView(_Base):
    def get(self, request, pk):
        order = _queryset().filter(pk=pk).first()
        if not order:
            return fail('Production order not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        data = ProductionOrderSerializer(order).data
        data['gate_exemption_reason'] = gate_service.exemption_reason(order)
        return ok(data)


class RequestHistoryView(_Base):
    def get(self, request, pk):
        order = ProductionOrder.objects.filter(pk=pk).first()
        if not order:
            return fail('Production order not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        logs = (order.action_logs
                .select_related('acted_by', 'stage')
                .order_by('acted_at', 'id'))
        return ok(ActionLogSerializer(logs, many=True).data)


class RequestStockView(_Base):
    """Where the item actually is — the stock panel on the detail dialog.

    Read LIVE from SAP each time the dialog opens, and deliberately not
    snapshotted onto the order. A stock figure is only useful while it is
    current, and a stale one is worse than none because nothing on screen tells
    the reader which it is looking at. The order's own `planned_qty` is a
    snapshot for a different reason: it is what was approved, so it must not
    move underneath the approval.

    Answers for the whole SITE, not just the order's warehouse — see
    `sap.ITEM_LOCATION_STOCK_SQL`. An approver deciding whether to release a
    production order wants to know whether the material is already standing in
    the next shed, and that is the question SAP's own
    `Get_Item_Location_Stock` was written to answer.

    503, not 200-with-an-empty-list, when SAP cannot be reached: an empty list
    here means the site genuinely holds none.
    """

    def get(self, request, pk):
        order = ProductionOrder.objects.filter(pk=pk).first()
        if not order:
            return fail('Production order not found.',
                        status=http_status.HTTP_404_NOT_FOUND)
        try:
            rows, meta = sap_service.item_location_stock(
                order.company, order.item_code, order.warehouse)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc),
                        status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        # `item_name` is not repeated here — the dialog's own header already
        # carries it, and the panel sits directly beneath.
        return ok({
            'warehouse': order.warehouse,
            # `location` is null only when the order's warehouse is not in OWHS
            # at all, which is the one case that legitimately has no rows.
            **meta,
            'results': rows,
        })


class _DecisionView(APIView):
    """Shared loading and authorisation for approve / reject."""

    permission_classes = [IsAuthenticated, prdo_perms.IsProductionApprover]

    def _load(self, request, pk):
        flow = (ProductionOrderFlow.objects
                .select_related('production_order', 'current_stage', 'workflow')
                .filter(production_order_id=pk)
                .first())
        if not flow:
            return None, fail('Production order not found.',
                              status=http_status.HTTP_404_NOT_FOUND)
        allowed, reason = prdo_perms.may_act_on(request.user, flow)
        if not allowed:
            # 403 for "not yours" and "no permission"; 409 for a flow that has
            # already finished, because that is a state conflict, not an
            # authorisation one.
            already = flow.status != FlowStatus.PENDING
            return None, fail(
                reason,
                status=(http_status.HTTP_409_CONFLICT if already
                        else http_status.HTTP_403_FORBIDDEN))
        return flow, None


class RequestApproveView(_DecisionView):
    def post(self, request, pk):
        flow, error = self._load(request, pk)
        if error:
            return error

        payload = DecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            flow = flow_service.approve(
                flow, user=request.user,
                remarks=payload.validated_data.get('remarks', ''))
        except flow_service.ProductionFlowError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        sap_text, sap_ok = '', None
        if flow.status == FlowStatus.APPROVED:
            # Final approval is what lets SAP release the order. The approval
            # is already committed, so a SAP failure does NOT undo it — it is
            # reported, recorded, and retryable.
            sap_ok = True
            try:
                sap_text = sap_service.write_approval(flow)
            except sap_service.SapWriteError as exc:
                sap_ok = False
                sap_text = str(exc)

        flow.refresh_from_db()
        return ok({
            'flow_id': flow.pk,
            'flow_status': flow.status,
            'current_stage': flow.current_stage_id,
            'current_user': flow.current_user_id,
            'sap_status': flow.sap_status,
            'sap_status_text': sap_text or flow.sap_status_text,
            'sap_written': sap_ok,
        }, message=_approve_message(flow, sap_ok))


def _approve_message(flow, sap_ok):
    if flow.status != FlowStatus.APPROVED:
        stage = flow.current_stage
        where = f' It now waits at {stage.name}.' if stage else ''
        return 'Approved.' + where
    if sap_ok is False:
        return ('Approved, but SAP did not accept it. The approval stands and '
                'can be retried.')
    return 'Approved. SAP will now allow this order to be released.'


class RequestRejectView(_DecisionView):
    def post(self, request, pk):
        flow, error = self._load(request, pk)
        if error:
            return error

        payload = RejectionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            flow = flow_service.reject(
                flow, user=request.user,
                remarks=payload.validated_data['remarks'])
        except flow_service.ProductionFlowError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)

        # Written so SAP can tell "rejected" from "nobody has decided yet".
        # The gate blocks on either, so this is documentation rather than
        # enforcement — and a failure here therefore costs only the record,
        # never a wrongly released order. Handled the same way regardless:
        # the rejection is committed and is not undone.
        sap_text, sap_ok = '', True
        try:
            sap_text = sap_service.write_rejection(flow)
        except sap_service.SapWriteError as exc:
            sap_ok = False
            sap_text = str(exc)

        flow.refresh_from_db()
        return ok({
            'flow_id': flow.pk,
            'flow_status': flow.status,
            'sap_status': flow.sap_status,
            'sap_status_text': sap_text or flow.sap_status_text,
            'sap_written': sap_ok,
        }, message=(
            'Rejected. SAP will keep refusing to release this order; the '
            'planner cancels or re-raises it there.'
            if sap_ok else
            'Rejected, but SAP did not record the rejection. The order stays '
            'blocked regardless — only the SAP-side record is missing, and it '
            'can be retried.'))


class RetrySapView(APIView):
    """Re-attempt the SAP write for a flow that has already been decided.

    The only path to SAP that is not a decision itself. Accepts an APPROVED or
    a REJECTED flow — both write to `OMS_PRDO_APPROVAL`, and a rejection whose
    write failed is just as much a missing record as an approval's.

    Refuses a flow still PENDING (there is no decision to write) and one
    already at SUCCESS: re-writing is harmless because it upserts, but a
    second SUCCESS would paper over the first failure.
    """

    permission_classes = [IsAuthenticated, prdo_perms.IsProductionApprover]

    #: Which writer each decided state needs. A flow in any other state has
    #: nothing to send.
    WRITERS = {
        FlowStatus.APPROVED: ('approval', sap_service.write_approval),
        FlowStatus.REJECTED: ('rejection', sap_service.write_rejection),
    }

    def post(self, request, pk):
        flow = (ProductionOrderFlow.objects
                .select_related('production_order')
                .filter(production_order_id=pk).first())
        if not flow:
            return fail('Production order not found.',
                        status=http_status.HTTP_404_NOT_FOUND)

        entry = self.WRITERS.get(flow.status)
        if not entry:
            return fail('Only a decided request can be written to SAP.',
                        status=http_status.HTTP_409_CONFLICT)
        word, writer = entry

        if flow.sap_status == SapWriteStatus.SUCCESS:
            return fail(f'This {word} has already reached SAP.',
                        status=http_status.HTTP_409_CONFLICT)

        try:
            text = writer(flow)
        except sap_service.SapWriteError as exc:
            return fail(str(exc), status=http_status.HTTP_502_BAD_GATEWAY)
        return ok({'sap_status': flow.sap_status, 'sap_status_text': text},
                  message=f'SAP accepted the {word}.')


# ---------------------------------------------------------------------------
# Approval desk
# ---------------------------------------------------------------------------

class ApprovalQueueView(APIView):
    """What is waiting for THIS user today, stand-ins included.

    Resolved through the engine's stage/replacement mapping, not through
    `flow.current_user` — that column is refreshed when a flow MOVES, so a
    stage reassigned while a flow sat still would be missed by a naive
    `current_user = me` query.
    """

    permission_classes = [IsAuthenticated, prdo_perms.IsProductionApprover]

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error, status=http_status.HTTP_400_BAD_REQUEST)

        flows = flow_service.pending_for(request.user)
        order_ids = [f.production_order_id for f in flows]
        qs = _queryset().filter(company, pk__in=order_ids)
        return ok(ProductionOrderSerializer(qs, many=True).data)


class ApprovalHistoryView(APIView):
    """Requests this user has decided, whatever their current status."""

    permission_classes = [IsAuthenticated, prdo_perms.IsProductionApprover]

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error, status=http_status.HTTP_400_BAD_REQUEST)
        state, error = _status_filter(request)
        if error:
            return fail(error, status=http_status.HTTP_400_BAD_REQUEST)

        ids = flow_service.decided_order_ids(request.user)
        # NEWEST DECISION FIRST, and specifically THIS user's decision — not
        # the order's own `created_at`, which is when SAP raised it and puts a
        # decision taken this morning below one taken last week purely because
        # the planner happened to raise them in that order.
        #
        # An order can carry several decisions (a rejection, a re-raise, an
        # approval), so this is the MAX of the ones belonging to this user.
        qs = (_queryset().filter(company).filter(state).filter(pk__in=ids)
              .annotate(decided_at=Max(
                  'action_logs__acted_at',
                  filter=Q(action_logs__acted_by=request.user,
                           action_logs__action__in=[LogAction.APPROVE,
                                                    LogAction.REJECT])))
              # `-id` breaks a tie deterministically: a bulk decision stamps
              # several rows in the same transaction, and without a second key
              # their order is whatever the database returns that day.
              .order_by('-decided_at', '-id'))
        return ok(ProductionOrderSerializer(qs[:500], many=True).data)


class InsightsView(_Base):
    """Counts by flow status, plus how many were exempt from SAP's gate."""

    def get(self, request):
        company, error = _company_filter(request)
        if error:
            return fail(error, status=http_status.HTTP_400_BAD_REQUEST)

        qs = ProductionOrder.objects.filter(company)
        counts = dict(
            qs.values_list('flow__status')
              .annotate(n=Count('id'))
              .values_list('flow__status', 'n')
        )
        approved = qs.filter(flow__status=FlowStatus.APPROVED)
        exempt = sum(1 for o in approved.only(
            'order_type', 'item_series', 'sap_user_sign') if gate_service.is_exempt(o))

        return ok({
            'total': qs.count(),
            'by_status': {k: v for k, v in counts.items() if k},
            'approved_total': approved.count(),
            # Surfaced deliberately: an approval SAP would not have enforced
            # is a record, not a control. See services/gate.py.
            'approved_but_exempt_from_sap_gate': exempt,
            'sap_write_failed': qs.filter(
                flow__sap_status=SapWriteStatus.FAILED).count(),
        })


class HealthView(_Base):
    """Last successful sync per company, and what is stuck.

    Exists so "is the feed alive?" is answerable without opening Task
    Scheduler. The failure this module is built around was a job that reported
    success for 33 days while writing nothing.
    """

    def get(self, request):
        data = []
        for code in COMPANY_CODES:
            last = sync_service.last_sync_at(code)
            data.append({
                'company': code,
                'last_synced_at': last.isoformat() if last else None,
                'pending': ProductionOrderFlow.objects.filter(
                    status=FlowStatus.PENDING,
                    production_order__company=code).count(),
                'sap_write_failed': ProductionOrderFlow.objects.filter(
                    sap_status=SapWriteStatus.FAILED,
                    production_order__company=code).count(),
            })
        return ok(data)
