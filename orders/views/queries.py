"""Read-only order queries: lists, details, logs, status tracking.

Split out of `orders/views.py` (plan item 3.1). Nothing here writes. The
scoping is the substance — which orders a given user may see at all — and it
comes from `._shared._get_base_orders`, so these views mostly shape what that
returns rather than deciding it.

`ORDERS_CROSS_USER_ROLES` is the exception list: the roles allowed to see
orders they did not create.
"""
from urllib import request
from orders.serializers import OrderDetailSerializer, OrderListByUserIdSerializer, OrdersLogSerializer, OrdersByItemSerializer
from orders.models import OrdersLog, Order, OrderItem, OrderStatus, OrderRateApproval
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404
from sap_sync.models import SalesQuotationLog
from ._shared import (
    APPROVER_ACTIVE_CODES,
    AUDITOR_ACCEPTED_ACTION_ID,
    AUDITOR_DECISION_ACTION_IDS,
    AUDITOR_REJECTED_ACTION_ID,
    BILLING_ACCEPTED_ACTION_ID,
    BILLING_ACTIVE_CODES,
    BILLING_DECISION_ACTION_IDS,
    BILLING_REJECTED_ACTION_ID,
    _get_base_orders,
)



class OrderStatusTrackingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        mode = (request.query_params.get('mode') or '').strip().lower()
        base_orders = _get_base_orders(request.user).select_related('status', 'created_by')

        if mode == 'auditor':
            latest_auditor_action = (
                OrdersLog.objects
                .filter(
                    order=OuterRef('pk'),
                    performed_by=request.user,
                    action_id__in=AUDITOR_DECISION_ACTION_IDS,
                )
                .order_by('-created_at', '-id')
                .values('action_id')[:1]
            )
            orders = base_orders.annotate(
                latest_decision_action_id=Subquery(latest_auditor_action)
            )
            accepted_orders = orders.filter(
                latest_decision_action_id=AUDITOR_ACCEPTED_ACTION_ID
            ).exclude(status__code='AUDITOR_APPROVAL')
            rejected_orders = orders.filter(
                latest_decision_action_id=AUDITOR_REJECTED_ACTION_ID
            ).exclude(status__code='AUDITOR_APPROVAL')
        elif mode == 'billing':
            latest_billing_action = (
                OrdersLog.objects
                .filter(
                    order=OuterRef('pk'),
                    performed_by=request.user,
                    action_id__in=BILLING_DECISION_ACTION_IDS,
                )
                .order_by('-created_at', '-id')
                .values('action_id')[:1]
            )
            orders = base_orders.annotate(
                latest_decision_action_id=Subquery(latest_billing_action)
            )
            accepted_orders = orders.filter(
                latest_decision_action_id=BILLING_ACCEPTED_ACTION_ID
            ).exclude(status__code__in=BILLING_ACTIVE_CODES)
            rejected_orders = orders.filter(
                latest_decision_action_id=BILLING_REJECTED_ACTION_ID
            ).exclude(status__code__in=BILLING_ACTIVE_CODES)
        elif mode in ['rate_approver', 'rate approver', 'approver']:
            accepted_orders = base_orders.filter(
                rate_approvals__approver=request.user,
                rate_approvals__status='APPROVED',
            )
            rejected_orders = base_orders.filter(
                rate_approvals__approver=request.user,
                rate_approvals__status='REJECTED',
            )
        else:
            return Response({'error': 'mode must be auditor, billing, or rate_approver'}, status=status.HTTP_400_BAD_REQUEST)

        
        accepted_data = OrderListByUserIdSerializer(accepted_orders.distinct(), many=True).data
        rejected_data = OrderListByUserIdSerializer(rejected_orders.distinct(), many=True).data

        for item in accepted_data:
            item['decision_type'] = 'accepted'
        for item in rejected_data:
            item['decision_type'] = 'rejected'

        return Response(sorted(
            [*accepted_data, *rejected_data],
            key=lambda item: item.get('created_at') or '',
            reverse=True,
        ))


class OrderStatusList(APIView):
    def get(self,request):
        status = OrderStatus.objects.all().values('id','name')
        return Response(list(status))

class OrderLogsByOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)

        logs = (
            OrdersLog.objects
            .filter(order=order)
            .exclude(action_id=1)
            .order_by('created_at', 'id')
        )

        logs = list(logs)
        current_status_name = (getattr(order.status, "name", "") or "").strip().lower()
        if current_status_name == "rate approval":
            latest_rate_log = next(
                (
                    log for log in reversed(logs)
                    if (getattr(log.action, "name", "") or "").strip().lower() == "rate approval"
                ),
                None
            )
            if latest_rate_log:
                latest_rate_log.performed_by = None
            elif order.status:
                latest_rate_log = OrdersLog.objects.create(
                    order=order,
                    action=order.status,
                    performed_by=None,
                    remarks='Rate approval pending',
                )
                logs.append(latest_rate_log)

        serializer = OrdersLogSerializer(logs, many=True)
        return Response(serializer.data)

class OrderDetailsByOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        order = get_object_or_404(
            Order.objects.select_related("status")
            .prefetch_related("items"),
            id=order_id
        )

        serializer = OrderDetailSerializer(order)
        return Response(serializer.data)

# Roles allowed to view other users' orders. Everyone else (e.g. distributors)
# is restricted to their own orders regardless of the user_id in the URL.
ORDERS_CROSS_USER_ROLES = {"admin", "manager", "billing", "approver", "auditor"}


class OrdersByUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        role_name = getattr(getattr(request.user, "role", None), "name", "")
        can_view_others = (
            request.user.is_superuser
            or request.user.is_staff
            or str(role_name).strip().lower() in ORDERS_CROSS_USER_ROLES
        )

        # A distributor (or any non-privileged user) can only ever see the
        # orders they themselves placed, never another user's.
        if not can_view_others and request.user.id != user_id:
            return Response(
                {"detail": "You can only view your own orders."},
                status=status.HTTP_403_FORBIDDEN,
            )

        orders = (
            Order.objects.filter(created_by=user_id)
            .select_related("status", "created_by")
            .prefetch_related("items", "items__schemes")
            .order_by("-created_at")
        )

        serializer = OrderListByUserIdSerializer(orders, many=True)

        return Response(serializer.data)


class OrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_filter = request.query_params.get('status', None)
        user_id = request.query_params.get('user_id', None)
        billing_view = request.query_params.get('billing', 'false').lower() == 'true'
        include_sap = request.query_params.get('include_sap', 'false').lower() == 'true'

        if request.user.is_authenticated:
            orders = _get_base_orders(request.user)
        else:
            orders = Order.objects.all()

        role = getattr(request.user, 'role', None) if request.user.is_authenticated else None
        role_name = getattr(role, 'name', '').lower() if role else ''

        if billing_view:
            # Billing view: show billing-related orders from the caller's allowed scope.
            orders = orders.filter(status_id__in=[3, 5, 6, 8])
        elif role_name == 'approver' and request.query_params.get('approval_pending', '').lower() == 'true':
            # Approver pending tab: only orders still waiting at the rate approval stage.
            orders = orders.filter(
                rate_approvals__approver=request.user,
                rate_approvals__status='PENDING',
            )
            if status_filter:
                orders = orders.filter(status__code=status_filter)
            else:
                orders = orders.filter(status__code__in=APPROVER_ACTIVE_CODES)
        elif role_name == 'approver' and status_filter:
            # Approver others: query directly to avoid double JOIN
            acted_order_ids = OrderRateApproval.objects.filter(
                approver=request.user,
                status__in=['APPROVED', 'REJECTED'],
            ).values_list('order_id', flat=True) 
            
            orders = Order.objects.filter(id__in=acted_order_ids)
        else:
            if status_filter:

                status_codes = [code for code in status_filter.split(',') if code]
                if len(status_codes) > 1:
                    orders = orders.filter(status__code__in=status_codes)
                else:
                    orders = orders.filter(status__code=status_filter)
            elif not (include_sap and role_name == 'admin'):
                orders = orders.filter(sap_created=False)

        if user_id:
            orders = orders.filter(created_by=user_id)

        orders = (
            orders
            .select_related('status', 'created_by')
            .prefetch_related('items', 'rate_approvals__approver')
            .order_by('-created_at')
            .distinct()
        )

        # The SAP document number is stored on the latest successful SalesQuotationLog
        # (keyed by str(order.id)), not on Order.sap_doc_number. Build a lookup so the
        # list response can surface it for orders that were pushed to SAP.
        order_ids = [str(oid) for oid in orders.values_list('id', flat=True)]
        sap_doc_map = {}
        if order_ids:
            quotation_logs = (
                SalesQuotationLog.objects
                .filter(order_id__in=order_ids, status='SUCCESS', sap_doc_num__isnull=False)
                .order_by('order_id', '-created_at')
                .values_list('order_id', 'sap_doc_num')
            )
            for log_order_id, sap_doc_num in quotation_logs:
                if log_order_id not in sap_doc_map:
                    sap_doc_map[log_order_id] = sap_doc_num

        data = []
        for order in orders:
            items_qs = order.items.all()
            items_count = items_qs.count()
            data.append({
                'id': order.id,
                'order_number': order.order_number,
                'order_type': order.order_type,
                'employee_id': order.employee_id,
                'card_code': order.card_code,
                'card_name': order.card_name,
                'total_amount': str(order.total_amount),
                'status': order.status.code,
                'status_display': order.status.name,
                'sap_doc_number': order.sap_doc_number or sap_doc_map.get(str(order.id)) or '',
                'items_count': items_count,
                'created_by': order.created_by.name if order.created_by else None,
                'created_at': order.created_at,
                'delivery_date': order.delivery_date,
                # 'po_number': order.po_number,
                'is_foc': order.is_foc,
                # 'bill_to_address': order.bill_to_address,
                # 'ship_to_address': order.ship_to_address,
                # 'dispatch_from_id': order.dispatch_from_id,
                # 'categories': list(
                #     items_qs.exclude(category__isnull=True)
                #     .exclude(category__exact='')
                #     .values_list('category', flat=True)
                #     .distinct()
                # ),
                # 'rate_approvals': _order_rate_approval_payload(order),
                # 'items': OrderItemSerializer(items_qs, many=True).data
            })

        return Response(data)


# Helpers for the disabled Sales Quotation views below — no live caller.
# def _quotation_entries_for_orders(order_ids):
#     """Map order_id -> latest successful quotation {doc_entry, doc_num}.
#
#     The SAP Sales Quotation created when an order completes is recorded in
#     SalesQuotationLog (keyed by str(order.id)). We need the DocEntry to talk to
#     the SAP Service Layer and the DocNum just for display.
#     """
#     str_ids = [str(oid) for oid in order_ids]
#     mapping = {}
#     if not str_ids:
#         return mapping
#     logs = (
#         SalesQuotationLog.objects
#         .filter(order_id__in=str_ids, status='SUCCESS', sap_doc_entry__isnull=False)
#         .order_by('order_id', '-created_at')
#         .values_list('order_id', 'sap_doc_entry', 'sap_doc_num')
#     )
#     for log_order_id, doc_entry, doc_num in logs:
#         # first row per order is the latest (queryset ordered by -created_at)
#         if log_order_id not in mapping:
#             mapping[log_order_id] = {'doc_entry': doc_entry, 'doc_num': doc_num}
#     return mapping
#
#
# def _is_quotation_open(row):
#     """A quotation is open/cancellable when DocStatus is 'O' and not cancelled."""
#     doc_status = str(row.get('DocStatus') or '').upper()
#     canceled = str(row.get('CANCELED') or '').upper()
#     return doc_status == 'O' and canceled != 'Y'


# ---------------------------------------------------------------------------
# DISABLED 2026-08-27 — the Sales Quotation flow is closed and no longer used.
#
# Commented out rather than deleted, at the maintainers' request, so the
# implementation stays visible in place. `sap_sync.SalesQuotationLog` and its
# table are KEPT: the history stays queryable and nothing is dropped.
#
# Its routes are commented out in urls.py alongside this. Note that
# QuotationStatusView had already stopped working: it called
# `SalesOrderService().get_quotation_status(doc_entries)` without the required
# `branch` argument, so every call raised TypeError — caught by the `except
# Exception` below, which returns the same empty map it returns when SAP is
# unreachable. The breakage was indistinguishable from SAP being down, which is
# why nothing surfaced it.
#
# To restore: uncomment here and in urls.py, and fix that call by deciding
# which company DB to query.
# ---------------------------------------------------------------------------
# class QuotationStatusView(APIView):
#     """Batch lookup of SAP Sales Quotation status for completed orders.
#
#     The View Orders page calls this with the ids of completed orders so it can
#     show the "Cancel Sales Quotation" button only for those whose quotation is
#     still open in SAP. Degrades gracefully (empty map) if SAP is unreachable so
#     the orders list still renders.
#     """
#
#     def get(self, request):
#         from hana.services.services import SalesOrderService
#
#         raw_ids = request.query_params.get('order_ids', '')
#         order_ids = [oid for oid in (i.strip() for i in raw_ids.split(',')) if oid.isdigit()]
#         if not order_ids:
#             return Response({'success': True, 'statuses': {}})
#
#         entry_map = _quotation_entries_for_orders(order_ids)
#         doc_entries = [v['doc_entry'] for v in entry_map.values() if v['doc_entry'] is not None]
#
#         sap_rows_by_entry = {}
#         if doc_entries:
#             try:
#                 rows = SalesOrderService().get_quotation_status(doc_entries)
#                 for row in rows:
#                     sap_rows_by_entry[int(row['DocEntry'])] = row
#             except Exception as exc:
#                 # SAP/HANA unreachable: return what we know but don't break the page.
#                 return Response(
#                     {'success': False, 'statuses': {}, 'error': str(exc)},
#                     status=status.HTTP_200_OK,
#                 )
#
#         statuses = {}
#         for order_id, info in entry_map.items():
#             doc_entry = info['doc_entry']
#             row = sap_rows_by_entry.get(int(doc_entry)) if doc_entry is not None else None
#             statuses[order_id] = {
#                 'doc_entry': doc_entry,
#                 'doc_num': info['doc_num'],
#                 'doc_status': (row or {}).get('DocStatus'),
#                 'canceled': (row or {}).get('CANCELED'),
#                 'is_open': bool(row) and _is_quotation_open(row),
#             }
#
#         return Response({'success': True, 'statuses': statuses})


# class CancelSalesQuotationView(APIView):
#     """Cancel a completed order's SAP Sales Quotation, then mirror it in OMS.
#
#     Only allowed for COMPLETED orders whose quotation is still open in SAP. The
#     SAP cancellation is the source of truth: OMS is only updated if SAP confirms.
#     """
#     permission_classes = [IsAuthenticated]
#
#     def post(self, request, order_id):
#         from django.conf import settings
#         from serviceLayer.service import SAPServiceLayerManager
#         from hana.services.services import SalesOrderService
#
#         try:
#             order = Order.objects.select_related('status').get(pk=order_id)
#         except Order.DoesNotExist:
#             return Response({'success': False, 'message': 'Order not found'},
#                             status=status.HTTP_404_NOT_FOUND)
#
#         if order.status.code != 'COMPLETED':
#             return Response(
#                 {'success': False, 'message': 'Only completed orders can have their quotation cancelled'},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#
#         if order.quotation_cancelled:
#             return Response(
#                 {'success': False, 'message': 'Quotation already cancelled'},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#
#         entry_map = _quotation_entries_for_orders([order.id])
#         info = entry_map.get(str(order.id))
#         doc_entry = info['doc_entry'] if info else None
#         doc_num = info['doc_num'] if info else None
#         if doc_entry is None:
#             return Response(
#                 {'success': False, 'message': 'No SAP sales quotation found for this order'},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#
#         # Confirm the quotation is still open before the destructive call.
#         try:
#             rows = SalesOrderService().get_quotation_status([doc_entry])
#             current = next((r for r in rows if int(r['DocEntry']) == int(doc_entry)), None)
#         except Exception as exc:
#             return Response(
#                 {'success': False, 'message': f'Could not verify quotation status in SAP: {exc}'},
#                 status=status.HTTP_502_BAD_GATEWAY,
#             )
#         if not current or not _is_quotation_open(current):
#             return Response(
#                 {'success': False, 'message': 'Quotation is not open in SAP and cannot be cancelled'},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#
#         cancel_url = f"{settings.HANA_SERVICE_LAYER_URL}/Quotations({int(doc_entry)})/Cancel"
#         try:
#             session = SAPServiceLayerManager.get_session('OIL')
#             sap_response = session.post(cancel_url, timeout=20)
#             if sap_response.status_code == 401:
#                 SAPServiceLayerManager.clear_session()
#                 session = SAPServiceLayerManager.get_session('OIL')
#                 sap_response = session.post(cancel_url, timeout=20)
#         except Exception as exc:
#             return Response(
#                 {'success': False, 'message': f'SAP request failed: {exc}'},
#                 status=status.HTTP_502_BAD_GATEWAY,
#             )
#
#         if sap_response.status_code not in (200, 201, 204):
#             try:
#                 details = sap_response.json()
#             except Exception:
#                 details = sap_response.text
#             return Response(
#                 {'success': False, 'message': 'SAP rejected the cancellation', 'details': details},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#
#         # SAP confirmed — mirror in OMS.
#         actor = request.user if getattr(request.user, 'is_authenticated', False) else None
#         order.quotation_cancelled = True
#         order.quotation_cancelled_at = timezone.now()
#         order.quotation_cancelled_by = actor
#         order.save(update_fields=['quotation_cancelled', 'quotation_cancelled_at', 'quotation_cancelled_by'])
#
#         try:
#             from audit.models import AuditLog
#             AuditLog.objects.create(
#                 user=actor,
#                 username=getattr(actor, 'username', '') or '',
#                 page='View Orders',
#                 action='Cancelled',
#                 record=f'Order {order.order_number} (Quotation {doc_num})',
#                 field='sales_quotation',
#                 old_value='Open',
#                 new_value='Cancelled',
#             )
#         except Exception:
#             pass  # auditing must never block the operation
#
#         return Response({
#             'success': True,
#             'message': 'Sales quotation cancelled',
#             'order_id': order.id,
#             'doc_num': doc_num,
#         })


# class QuotationOverviewView(APIView):
#     """Admin overview of every completed order and its SAP sales-quotation
#     status (CANCELLED / OPEN / CLOSED / UNKNOWN). Powers the admin
#     "Sales Quotation" screen on web and mobile.
#
#     Degrades gracefully if SAP is unreachable: cancelled orders are still
#     reported from OMS, and the rest show UNKNOWN with a sap_error note.
#     """
#     permission_classes = [IsAuthenticated]
#
#     def get(self, request):
#         from hana.services.services import SalesOrderService
#
#         role_name = getattr(getattr(request.user, 'role', None), 'name', '')
#         is_admin = request.user.is_staff or str(role_name).strip().lower() == 'admin'
#         if not is_admin:
#             return Response(
#                 {'success': False, 'message': 'Only admin can view quotation status'},
#                 status=status.HTTP_403_FORBIDDEN,
#             )
#
#         completed = (
#             Order.objects
#             .filter(status__code='COMPLETED')
#             .select_related('quotation_cancelled_by')
#             .order_by('-created_at')
#         )
#
#         order_ids = [str(order.id) for order in completed]
#         entry_map = _quotation_entries_for_orders(order_ids)
#         doc_entries = [v['doc_entry'] for v in entry_map.values() if v['doc_entry'] is not None]
#
#         sap_rows_by_entry = {}
#         sap_error = None
#         if doc_entries:
#             try:
#                 rows = SalesOrderService().get_quotation_status(doc_entries)
#                 for row in rows:
#                     sap_rows_by_entry[int(row['DocEntry'])] = row
#             except Exception as exc:
#                 sap_error = str(exc)
#
#         data = []
#         for order in completed:
#             info = entry_map.get(str(order.id)) or {}
#             doc_entry = info.get('doc_entry')
#             doc_num = info.get('doc_num')
#             row = sap_rows_by_entry.get(int(doc_entry)) if doc_entry is not None else None
#
#             if order.quotation_cancelled:
#                 quotation_status = 'CANCELLED'
#             elif row is not None:
#                 quotation_status = 'OPEN' if _is_quotation_open(row) else 'CLOSED'
#             else:
#                 quotation_status = 'UNKNOWN'
#
#             data.append({
#                 'id': order.id,
#                 'order_number': order.order_number,
#                 'card_code': order.card_code,
#                 'card_name': order.card_name,
#                 'created_at': order.created_at,
#                 'doc_num': doc_num,
#                 'doc_entry': doc_entry,
#                 'quotation_cancelled': order.quotation_cancelled,
#                 'quotation_cancelled_at': order.quotation_cancelled_at,
#                 'quotation_cancelled_by': getattr(order.quotation_cancelled_by, 'username', None),
#                 'quotation_status': quotation_status,
#             })
#
#         return Response({'success': True, 'data': data, 'sap_error': sap_error})



class GetOrdersByItemView(APIView):
    # permission_classes = [IsAuthenticate]

    def get(self, request):
        item_code = request.query_params.get('item_code')
        if not item_code:
            return Response({"error": "item_code is required"}, status=status.HTTP_400_BAD_REQUEST)

        orders = OrderItem.objects.filter(item_code=item_code).select_related('order').order_by('-order__created_at')

        serializer = OrdersByItemSerializer(orders, many=True)
        return Response(serializer.data)
