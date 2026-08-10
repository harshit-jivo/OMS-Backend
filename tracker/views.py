from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone
from rest_framework import status as http
from .permissions import (
    IsTrackerAdmin, IsTrackerAlerts, IsTrackerEntry, IsTrackerReports,
    IsTrackerUser,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .permissions import PAGE_ADMIN, PAGE_ENTRY, tracker_pages_for

# A tracker admin may delete an invoice up to this stage order (inclusive).
# Stage 6 == "JSAP Approval"; nothing at Save-in-SAP or Payment can be deleted.
# (Was 5 when SAP and JSAP shared one desk — the split moved the cut-off down.)
DELETE_ADMIN_MAX_ORDER = 6
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


class VendorsView(APIView):
    """SAP vendors for the entry form's searchable party dropdown (fast,
    cached, direct HANA query)."""
    permission_classes = [IsTrackerEntry]

    def get(self, request):
        from .sap import fetch_vendors
        force = request.query_params.get('refresh') == '1'
        try:
            return Response(fetch_vendors(force_refresh=force))
        except Exception as exc:
            return Response(
                {'detail': f'Could not fetch vendors from SAP: {exc}'},
                status=http.HTTP_502_BAD_GATEWAY)


class JsapStatusView(APIView):
    """Budget-approval status of one invoice, straight from JSAP.

    Read-only: JSAP owns the decision, this only reports it. Always 200 —
    "we could not link this invoice to SAP" is an answer the desk needs to
    show, not an error.
    """
    permission_classes = [IsTrackerUser]

    def get(self, request, pk):
        from . import jsap
        invoice = Invoice.objects.filter(pk=pk).select_related(
            'unit', 'branch', 'category').first()
        if not invoice:
            return Response(status=http.HTTP_404_NOT_FOUND)
        return Response(jsap.status_for_invoice(invoice))


class JsapSyncView(APIView):
    """Manual "refresh from JSAP" for the JSAP desk.

    Same engine as the scheduled `sync_jsap` command: approved invoices
    advance, rejected ones return to SAP Approval with JSAP's own reason,
    pending ones stay. POST with {"invoice_id": n} to sync one invoice, or no
    body to sweep the whole desk.
    """
    permission_classes = [IsTrackerUser]

    def post(self, request):
        from . import jsap
        if not jsap.is_configured():
            return Response({'detail': 'JSAP database is not configured.'},
                            status=http.HTTP_503_SERVICE_UNAVAILABLE)

        invoice_id = request.data.get('invoice_id')
        try:
            if invoice_id:
                invoice = Invoice.objects.filter(pk=invoice_id).select_related(
                    'current_stage', 'unit', 'branch', 'category').first()
                if not invoice:
                    return Response(status=http.HTTP_404_NOT_FOUND)
                if not services.can_act(request.user, invoice):
                    return Response({'detail': 'You are not assigned to this stage.'},
                                    status=http.HTTP_403_FORBIDDEN)
                return Response(services.sync_jsap(invoice, user=request.user))
            return Response(services.sync_jsap_all(user=request.user))
        except (ValidationError, PermissionDenied) as exc:
            return Response({'detail': str(getattr(exc, 'message', exc))},
                            status=http.HTTP_400_BAD_REQUEST)


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


def _can_use_entry(user):
    """True for users who work the head-office / entry desk (entry-page role)."""
    return user.is_superuser or PAGE_ENTRY in tracker_pages_for(user)


def _is_tracker_admin(user):
    """True for a tracker admin (holds the Tracker_Admin page) or a superuser."""
    return user.is_superuser or PAGE_ADMIN in tracker_pages_for(user)


def _scoped_queryset(user):
    """Invoices this user is allowed to see: their own creations, anything
    parked at a stage they're assigned to, plus — for entry-desk users — every
    invoice currently at the shared head-office / entry stage (regardless of
    who created it). Superusers see all."""
    qs = Invoice.objects.select_related(
        'current_stage', 'gst_type', 'gst_rate', 'category',
        'unit', 'branch', 'mode', 'created_by', 'payment',
    )
    if user.is_superuser:
        return qs
    stage_ids = services.accessible_stage_ids(user)
    q = Q(created_by=user) | Q(current_stage_id__in=stage_ids)
    if _can_use_entry(user):
        q |= Q(current_stage__code='entry')
    return qs.filter(q)


def _apply_filters(qs, params):
    party = params.get('party')
    if party:
        qs = qs.filter(party_name__icontains=party)
    inv_no = params.get('invoice_number')
    if inv_no:
        qs = qs.filter(invoice_number__icontains=inv_no)
    # Effective month filter — value is "YYYY-MM"; match that accounting period.
    eff = params.get('effective_month')
    if eff:
        try:
            y, m = str(eff).split('-')[:2]
            qs = qs.filter(effective_month__year=int(y), effective_month__month=int(m))
        except (ValueError, TypeError):
            pass
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
        # Editable only while unlocked at the shared entry (head-office) desk;
        # any entry-desk user may edit, not just the original creator.
        if invoice.is_locked or invoice.current_stage.code != 'entry':
            return Response(
                {'detail': 'Invoice is locked and can no longer be edited.'},
                status=http.HTTP_403_FORBIDDEN)
        if not _can_use_entry(request.user):
            return Response(
                {'detail': 'You are not on the entry desk.'},
                status=http.HTTP_403_FORBIDDEN)
        serializer = InvoiceWriteSerializer(invoice, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            InvoiceDetailSerializer(invoice, context={'request': request}).data)

    def delete(self, request, pk):
        """Soft-delete an invoice (row kept, hidden everywhere).

        Two paths:
          * Tracker admin  -> may delete an invoice up to stage 5 (order <= 5),
            regardless of lock state or stage-assignment scope.
          * Entry-desk user -> may delete only while it is still unlocked at the
            entry (head-office) stage.
        """
        user = request.user
        admin = _is_tracker_admin(user)
        # Admins can reach any invoice; entry users are limited to their scope.
        invoice = (Invoice.objects.filter(pk=pk).first() if admin
                   else self._get(request, pk))
        if not invoice:
            return Response(status=http.HTTP_404_NOT_FOUND)

        stage = invoice.current_stage
        if admin:
            if stage.order > DELETE_ADMIN_MAX_ORDER:
                return Response(
                    {'detail': f'This invoice is at "{stage.name}" (stage {stage.order}). '
                               f'Tracker admins can delete only up to stage '
                               f'{DELETE_ADMIN_MAX_ORDER}.'},
                    status=http.HTTP_403_FORBIDDEN)
        elif _can_use_entry(user) and not invoice.is_locked and stage.code == 'entry':
            pass  # entry desk deleting a fresh entry-stage invoice
        else:
            return Response(
                {'detail': 'You do not have permission to delete this invoice.'},
                status=http.HTTP_403_FORBIDDEN)

        invoice.is_deleted = True
        invoice.deleted_at = timezone.now()
        invoice.deleted_by = request.user
        invoice.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by', 'updated_at'])
        return Response(status=http.HTTP_204_NO_CONTENT)


class MyQueueView(APIView):
    """The actionable inbox: invoices parked at a stage this user handles.

    The head-office / entry desk is shared: any entry-desk user sees every
    invoice at the entry stage (freshly created or returned back), regardless of
    creator. Returns every stage the user works (so all their desks show as tabs
    even when empty) alongside the pending invoices.
    """
    permission_classes = [IsTrackerUser]

    def get(self, request):
        user = request.user
        stage_ids = set(services.accessible_stage_ids(user))
        entry = Stage.objects.filter(code='entry').first()
        # Entry-desk users get the shared entry queue even without an explicit
        # stage assignment.
        if entry and _can_use_entry(user):
            stage_ids.add(entry.id)

        qs = Invoice.objects.select_related(
            'current_stage', 'gst_type', 'gst_rate', 'category',
            'unit', 'branch', 'mode', 'created_by', 'payment',
        ).filter(
            current_stage_id__in=stage_ids,
            status=Invoice.Status.IN_PROGRESS,
        )

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
        entry_shared = stage.code == 'entry' and _can_use_entry(request.user)
        if not request.user.is_superuser and not entry_shared and \
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

        # The entry desk is shared — show every advanced-from-entry invoice,
        # regardless of who created it.
        invoices = Invoice.objects.filter(id__in=advanced_at.keys()).select_related(
            'current_stage', 'gst_type', 'gst_rate', 'category',
            'unit', 'branch', 'mode', 'created_by')

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
        hold_type = request.data.get('hold_type', '')  # FULL | PARTIAL (HOLD only)
        amount = request.data.get('amount')            # hold / debit amount
        try:
            processed, errors = services.apply_bulk(
                invoice_ids=ids, user=request.user, action=action,
                stage_status=stage_status, remarks=remarks,
                hold_type=hold_type, amount=amount,
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


class AdminInvoicesView(APIView):
    """Master list of EVERY invoice for the tracker admin, with filters.

    Completed invoices and overdue (past-threshold) invoices are flagged so the
    UI can fade the former and highlight the latter. Supports filters: stage,
    status, overdue, party, invoice_number, category, branch, unit.
    """
    permission_classes = [IsTrackerAdmin]

    def get(self, request):
        qs = Invoice.objects.select_related(
            'current_stage', 'gst_type', 'gst_rate', 'category',
            'unit', 'branch', 'mode', 'created_by', 'payment',
        )
        qs = _apply_filters(qs, request.query_params)
        data = InvoiceListSerializer(qs, many=True, context={'request': request}).data
        overdue = request.query_params.get('overdue')
        if overdue == 'true':
            data = [d for d in data if d['is_overdue']]
        elif overdue == 'false':
            data = [d for d in data if not d['is_overdue']]
        return Response(data)


class AdminInvoicesExportView(APIView):
    """Excel export of every invoice in the office's original sheet layout,
    honouring the same filters as the All-Invoices list."""
    permission_classes = [IsTrackerAdmin]

    def get(self, request):
        from django.http import HttpResponse
        from .exports import build_workbook
        qs = Invoice.objects.select_related(
            'current_stage', 'gst_type', 'gst_rate', 'category',
            'unit', 'branch', 'mode', 'payment',
        ).prefetch_related('events__stage')
        qs = _apply_filters(qs, request.query_params)
        invoices = list(qs)
        overdue = request.query_params.get('overdue')
        if overdue in ('true', 'false'):
            want = overdue == 'true'
            invoices = [i for i in invoices if services.is_overdue(i) == want]

        buf = build_workbook(invoices)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = 'attachment; filename="invoice-register.xlsx"'
        return resp


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
            'invoice', 'stage').prefetch_related('notifications__user')
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

        # The client sends the inputs (discount %, TDS %, paid amount); the
        # engine derives the amounts, open balance and status, caps the paid
        # amount at the net payable, and completes the invoice when fully paid.
        try:
            invoice, _payment = services.apply_payment(
                invoice=invoice, user=request.user,
                discount_pct=request.data.get('discount_pct', 0),
                tds_pct=request.data.get('tds_pct', 0),
                paid_amount=request.data.get('paid_amount'),
                hold_added_back=request.data.get('hold_added_back', False),
            )
        except ValidationError as exc:
            return Response(
                {'detail': str(getattr(exc, 'message', exc))},
                status=http.HTTP_400_BAD_REQUEST)
        return Response(
            InvoiceDetailSerializer(invoice, context={'request': request}).data)
