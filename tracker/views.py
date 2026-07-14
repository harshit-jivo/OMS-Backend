from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from rest_framework import status as http
from .permissions import (
    IsTrackerAlerts, IsTrackerEntry, IsTrackerReports, IsTrackerUser,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .reports import build_report
from .models import (
    Branch, Category, GstRate, GstType, Invoice, InvoiceMode, PaymentDetail,
    Stage, StageEvent, StuckAlert, Unit,
)
from .serializers import (
    BranchSerializer, CategorySerializer, GstRateSerializer, GstTypeSerializer,
    InvoiceDetailSerializer, InvoiceListSerializer, InvoiceModeSerializer,
    InvoiceWriteSerializer, PaymentDetailSerializer, StageSerializer,
    StuckAlertSerializer, UnitSerializer,
)


class LookupsView(APIView):
    """Everything the entry form / filters need in one call."""
    permission_classes = [IsTrackerUser]

    def get(self, request):
        active = lambda qs: qs.filter(is_active=True)
        return Response({
            'categories': CategorySerializer(active(Category.objects), many=True).data,
            'units': UnitSerializer(active(Unit.objects), many=True).data,
            'branches': BranchSerializer(active(Branch.objects), many=True).data,
            'modes': InvoiceModeSerializer(active(InvoiceMode.objects), many=True).data,
            'gst_types': GstTypeSerializer(active(GstType.objects), many=True).data,
            'gst_rates': GstRateSerializer(active(GstRate.objects), many=True).data,
            'stages': StageSerializer(
                Stage.objects.filter(is_active=True), many=True).data,
        })


def _scoped_queryset(user):
    """Invoices this user is allowed to see: their own creations plus anything
    parked at a stage they're assigned to. Superusers see all."""
    qs = Invoice.objects.select_related(
        'current_stage', 'gst_type', 'gst_rate', 'category',
        'unit', 'branch', 'mode', 'created_by',
    )
    if user.is_superuser:
        return qs
    stage_ids = services.accessible_stage_ids(user)
    return qs.filter(Q(created_by=user) | Q(current_stage_id__in=stage_ids))


def _apply_filters(qs, params):
    party = params.get('party')
    if party:
        qs = qs.filter(party_name__icontains=party)
    inv_no = params.get('invoice_number')
    if inv_no:
        qs = qs.filter(invoice_number__icontains=inv_no)
    for field in ('category', 'branch', 'unit', 'status'):
        val = params.get(field)
        if val:
            qs = qs.filter(**{f'{field}': val} if field == 'status'
                           else {f'{field}_id': val})
    stage = params.get('stage')
    if stage:
        qs = qs.filter(current_stage__code=stage)
    return qs


class InvoiceListCreateView(APIView):
    permission_classes = [IsTrackerEntry]

    def get(self, request):
        qs = _apply_filters(_scoped_queryset(request.user), request.query_params)
        data = InvoiceListSerializer(qs, many=True, context={'request': request}).data
        if request.query_params.get('overdue') == 'true':
            data = [d for d in data if d['is_overdue']]
        return Response(data)

    def post(self, request):
        serializer = InvoiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = services.create_invoice(
            created_by=request.user, **serializer.validated_data)
        return Response(
            InvoiceDetailSerializer(invoice, context={'request': request}).data,
            status=http.HTTP_201_CREATED,
        )


class InvoiceDetailView(APIView):
    permission_classes = [IsTrackerUser]

    def _get(self, request, pk):
        return _scoped_queryset(request.user).filter(pk=pk).first()

    def get(self, request, pk):
        invoice = self._get(request, pk)
        if not invoice:
            return Response(status=http.HTTP_404_NOT_FOUND)
        return Response(
            InvoiceDetailSerializer(invoice, context={'request': request}).data)

    def patch(self, request, pk):
        invoice = self._get(request, pk)
        if not invoice:
            return Response(status=http.HTTP_404_NOT_FOUND)
        # Editable only while unlocked at the entry stage, by its creator.
        if invoice.is_locked or invoice.current_stage.code != 'entry':
            return Response(
                {'detail': 'Invoice is locked and can no longer be edited.'},
                status=http.HTTP_403_FORBIDDEN)
        if invoice.created_by_id != request.user.id and not request.user.is_superuser:
            return Response(
                {'detail': 'Only the creator may edit this invoice.'},
                status=http.HTTP_403_FORBIDDEN)
        serializer = InvoiceWriteSerializer(invoice, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            InvoiceDetailSerializer(invoice, context={'request': request}).data)


class MyQueueView(APIView):
    """The actionable inbox: invoices parked at a stage this user handles.
    Entry-stage items are restricted to the user's own creations.

    Returns every stage the user is assigned to (so all their desks show as
    tabs even when empty) alongside the pending invoices.
    """
    permission_classes = [IsTrackerUser]

    def get(self, request):
        user = request.user
        stage_ids = services.accessible_stage_ids(user)
        entry = Stage.objects.filter(code='entry').first()

        qs = Invoice.objects.select_related('current_stage').filter(
            current_stage_id__in=stage_ids,
            status=Invoice.Status.IN_PROGRESS,
        )
        # At the entry desk a user only sees invoices they themselves created.
        if entry and entry.id in stage_ids and not user.is_superuser:
            qs = qs.filter(~Q(current_stage_id=entry.id) | Q(created_by=user))

        invoices = list(qs)
        counts = {}
        for inv in invoices:
            counts[inv.current_stage_id] = counts.get(inv.current_stage_id, 0) + 1

        accessible_stages = (
            Stage.objects.filter(id__in=stage_ids, is_active=True).order_by('order')
        )
        stages = [{
            'code': s.code, 'name': s.name, 'order': s.order,
            'count': counts.get(s.id, 0),
        } for s in accessible_stages]

        rows = InvoiceListSerializer(
            invoices, many=True, context={'request': request}).data

        # Flag invoices that arrived at their current desk via a RETURN (rework),
        # so the UI can split "Current" from "Returned" and surface the reason.
        # A return always closes a visit at the immediately-later stage, so the
        # invoice's latest closed event tells us how it got here.
        inv_ids = [i.id for i in invoices]
        latest_closed = {}
        for ev in StageEvent.objects.filter(
            invoice_id__in=inv_ids, exited_at__isnull=False
        ).select_related('stage', 'acted_by').order_by('invoice_id', 'exited_at'):
            latest_closed[ev.invoice_id] = ev  # last wins == latest exit
        for r in rows:
            ev = latest_closed.get(r['id'])
            if ev and ev.event_type == StageEvent.EventType.RETURN:
                r['arrived_via_return'] = True
                r['return_reason'] = ev.remarks
                r['returned_from'] = ev.stage.name
                r['returned_by'] = ev.acted_by.username if ev.acted_by else None
                r['returned_at'] = ev.exited_at
            else:
                r['arrived_via_return'] = False

        return Response({'stages': stages, 'invoices': rows})


class StageAdvancedView(APIView):
    """Invoices already advanced FORWARD from a stage (read-only history).

    Used by the entry stage's "Advanced" tab. An invoice qualifies if it has a
    closed ADVANCE event at the given stage and is no longer sitting there;
    `advanced_at` is when it last left this desk going forward.
    """
    permission_classes = [IsTrackerUser]

    def get(self, request):
        stage = Stage.objects.filter(code=request.query_params.get('stage')).first()
        if not stage:
            return Response({'detail': 'Unknown stage.'}, status=http.HTTP_400_BAD_REQUEST)
        if not request.user.is_superuser and \
                stage.id not in services.accessible_stage_ids(request.user):
            return Response({'detail': 'You are not assigned to this stage.'},
                            status=http.HTTP_403_FORBIDDEN)

        advanced_at = {}
        for ev in StageEvent.objects.filter(
            stage=stage, event_type=StageEvent.EventType.ADVANCE,
            exited_at__isnull=False,
        ).exclude(invoice__current_stage=stage).order_by(
            'invoice_id', 'exited_at'
        ).values('invoice_id', 'exited_at'):
            advanced_at[ev['invoice_id']] = ev['exited_at']

        # Entry desk: only the user's own creations (unless superuser).
        invoices = Invoice.objects.filter(id__in=advanced_at.keys()).select_related(
            'current_stage', 'gst_type', 'gst_rate', 'category',
            'unit', 'branch', 'mode', 'created_by')
        if stage.code == 'entry' and not request.user.is_superuser:
            invoices = invoices.filter(created_by=request.user)

        rows = InvoiceListSerializer(
            invoices, many=True, context={'request': request}).data
        for r in rows:
            r['advanced_at'] = advanced_at.get(r['id'])
        rows.sort(key=lambda r: r['advanced_at'] or '', reverse=True)
        return Response(rows)


class BulkActionView(APIView):
    """Advance / return / hold one or many invoices at once."""
    permission_classes = [IsTrackerUser]

    def post(self, request):
        ids = request.data.get('ids') or []
        action = request.data.get('action')            # ADVANCE | RETURN (optional)
        stage_status = request.data.get('stage_status', '')
        remarks = request.data.get('remarks', '')
        try:
            processed, errors = services.apply_bulk(
                invoice_ids=ids, user=request.user, action=action,
                stage_status=stage_status, remarks=remarks,
            )
        except (ValidationError, PermissionDenied) as exc:
            return Response(
                {'detail': str(getattr(exc, 'message', exc))},
                status=http.HTTP_400_BAD_REQUEST)
        return Response({
            'processed': processed,
            'errors': errors,
            'processed_count': len(processed),
        }, status=http.HTTP_207_MULTI_STATUS if errors else http.HTTP_200_OK)


class ReportsView(APIView):
    """Turnaround analytics: pending, avg days/stage, bottlenecks, ageing."""
    permission_classes = [IsTrackerReports]

    def get(self, request):
        return Response(build_report(request.query_params))


class AlertsView(APIView):
    """Active stuck-invoice alerts, scoped to the stages the user handles.
    Superusers see all. Pass ?all=true (superuser) to include everything."""
    permission_classes = [IsTrackerAlerts]

    def get(self, request):
        qs = StuckAlert.objects.filter(is_active=True).select_related(
            'invoice', 'stage')
        if not request.user.is_superuser:
            stage_ids = services.accessible_stage_ids(request.user)
            qs = qs.filter(stage_id__in=stage_ids)
        return Response(StuckAlertSerializer(qs, many=True).data)


class PaymentDetailView(APIView):
    """Capture / update payment at the terminal stage; marking it Paid
    completes the invoice."""
    permission_classes = [IsTrackerUser]

    def patch(self, request, pk):
        invoice = _scoped_queryset(request.user).filter(pk=pk).first()
        if not invoice:
            return Response(status=http.HTTP_404_NOT_FOUND)
        if not invoice.current_stage.is_terminal:
            return Response(
                {'detail': 'Invoice is not at the payment stage.'},
                status=http.HTTP_400_BAD_REQUEST)
        if not services.can_act(request.user, invoice):
            return Response(
                {'detail': 'You are not assigned to the payment stage.'},
                status=http.HTTP_403_FORBIDDEN)

        payment, _ = PaymentDetail.objects.get_or_create(invoice=invoice)
        serializer = PaymentDetailSerializer(payment, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        if payment.status == PaymentDetail.Status.PAID:
            invoice.status = Invoice.Status.COMPLETED
            invoice.save(update_fields=['status'])
        return Response(
            InvoiceDetailSerializer(invoice, context={'request': request}).data)
