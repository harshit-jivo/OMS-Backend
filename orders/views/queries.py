"""Read-only order queries: lists, details, logs, status tracking.

Split out of `orders/views.py` (plan item 3.1). Nothing here writes. The
scoping is the substance — which orders a given user may see at all — and it
comes from `._shared._get_base_orders`, so these views mostly shape what that
returns rather than deciding it.

`ORDERS_CROSS_USER_ROLES` is the exception list: the roles allowed to see
orders they did not create.
"""
from urllib import request
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_serializer,
    inline_serializer,
)
from orders.serializers import OrderDetailSerializer, OrderItemSchemeSerializer, OrderItemSerializer, OrderListByUserIdSerializer, OrdersLogSerializer, OrdersByItemSerializer
from orders.models import OrdersLog, Order, OrderItem, OrderStatus, OrderRateApproval
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from core.pagination import StandardPagination, ordering_from
from core.permissions import HasKey
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
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
    _get_user_category_names,
    sees_all_orders,
)


# ---------------------------------------------------------------------------
# OpenAPI response shapes (`@extend_schema` below). Documentation only — none
# of this runs, and none of it changes what any view returns.
#
# These views build their JSON by hand, so drf-spectacular has no
# `serializer_class` to infer from and emits an undescribed body. Each shape
# below was read off the view it annotates, branch by branch.
#
# Two shapes recur and are worth stating once:
#
# * A RAISED exception (an `Http404` from `get_object_or_404`, a permission
#   denial) passes through `core.exception_handler.api_exception_handler`,
#   which keeps the original keys and fills in `detail` / `message` / `error` /
#   `success`. That is `ORDERS_QUERY_ERROR` below.
# * An explicit `return Response({...}, status=...)` never reaches that
#   handler, so those branches carry exactly the keys the view wrote and
#   nothing else. They are declared literally.
# ---------------------------------------------------------------------------

# Five doc-only serializer subclasses. They are NEVER instantiated at runtime
# — the views below still return the originals — and they exist because the
# schema drf-spectacular derives from those originals is wrong about a handful
# of fields, in ways that would be baked straight into the generated
# TypeScript.
#
# Each override was checked against the method it replaces:
#
# * `OrdersLogSerializer.performed_by_name` has `source="performed_by.username"`
#   and is not `allow_null`, so when `performed_by` is None DRF raises
#   `SkipField` and OMITS THE KEY. That is not an edge case here — this very
#   view blanks `performed_by` on the pending "Rate approval" row before
#   serialising — so the key is optional, not merely nullable.
# * `SerializerMethodField` has no return annotation on any of these getters,
#   and spectacular's documented fallback for that is `string`. Four of them do
#   not return a string at all: `is_scheme_visible` returns a bool,
#   `approval_approvers` a list of `{id, name}`, `last_purchase_price` a
#   Decimal (rendered as a JSON number) and `vareity_cost` an object of three
#   Decimals. The rest return `str | None`, so they are pinned nullable.
#
# The real fix is a return annotation (or `@extend_schema_field`) on each
# getter in `orders/serializers.py`; these subclasses stand in until that lands
# and inherit every other field, so there is nothing here to keep in sync.


@extend_schema_serializer(component_name='OrdersLog')
class _OrdersLogSchema(OrdersLogSerializer):
    """Schema alias for `OrdersLogSerializer` — `performed_by_name` optional."""

    performed_by_name = serializers.CharField(required=False)


@extend_schema_serializer(component_name='OrderItemScheme')
class _OrderItemSchemeSchema(OrderItemSchemeSerializer):
    """Schema alias for `OrderItemSchemeSerializer`.

    Both getters return None when the line carries no scheme id, which is the
    common case.
    """

    scheme_name = serializers.CharField(read_only=True, allow_null=True)
    scheme_item_code = serializers.CharField(read_only=True, allow_null=True)


@extend_schema_serializer(component_name='OrderItemDetail')
class _OrderItemSchema(OrderItemSerializer):
    """Schema alias for `OrderItemSerializer`, with its method fields typed."""

    schemes = _OrderItemSchemeSchema(many=True, read_only=True)
    is_scheme_visible = serializers.BooleanField(read_only=True)
    approval_approvers = inline_serializer(
        name='OrderItemApprover',
        fields={
            'id': serializers.IntegerField(),
            'name': serializers.CharField(allow_blank=True),
        },
        many=True,
        read_only=True,
    )
    # `OrderItem.basic_price` of the party's previous order for this item, or
    # null when there is no previous order. A Decimal, so a JSON number.
    last_purchase_price = serializers.FloatField(read_only=True, allow_null=True)
    scheme_name = serializers.CharField(read_only=True, allow_null=True)
    scheme_item_code = serializers.CharField(read_only=True, allow_null=True)


@extend_schema_serializer(component_name='OrderDetail')
class _OrderDetailSchema(OrderDetailSerializer):
    """Schema alias for `OrderDetailSerializer`, with its method fields typed."""

    items = _OrderItemSchema(many=True, read_only=True)
    vareity_cost = inline_serializer(
        name='OrderVarietyCost',
        fields={
            'commodity_price': serializers.FloatField(),
            'other_total': serializers.FloatField(),
            'premium_total': serializers.FloatField(),
        },
        read_only=True,
    )
    party_state = serializers.CharField(read_only=True, allow_null=True)
    created_by_name = serializers.CharField(read_only=True, allow_null=True)
    # Null when the order carries no address id, or when the id no longer
    # resolves to a row in `sap_party_addresses`.
    bill_to_full_address = serializers.CharField(read_only=True, allow_null=True)
    ship_to_full_address = serializers.CharField(read_only=True, allow_null=True)


@extend_schema_serializer(component_name='OrderListByUserId')
class _OrderListByUserIdSchema(OrderListByUserIdSerializer):
    """Schema alias for `OrderListByUserIdSerializer`."""

    # `get_created_by_name` returns None for an order whose creator was
    # deleted (the FK is SET_NULL).
    created_by_name = serializers.CharField(read_only=True, allow_null=True)


ORDERS_QUERY_ERROR = inline_serializer(
    name='OrdersQueryError',
    fields={
        'detail': serializers.CharField(),
        'message': serializers.CharField(),
        'error': serializers.CharField(),
        'success': serializers.BooleanField(),
    },
)

#: `OrdersByUserView`'s 403 — written by the view itself, so it is a bare
#: `{"detail": ...}` with none of the handler's extra keys.
ORDERS_BY_USER_FORBIDDEN = inline_serializer(
    name='OrdersByUserForbidden',
    fields={'detail': serializers.CharField()},
)

#: `OrderStatusList` — literally `OrderStatus.objects.values('id', 'name')`.
ORDER_STATUS_OPTIONS = inline_serializer(
    name='OrderStatusOption',
    fields={
        'id': serializers.IntegerField(),
        'name': serializers.CharField(),
    },
    many=True,
)

#: `OrderListView`'s hand-built row. No serializer describes it; the types are
#: what the view puts in the dict, not what the model column is.
ORDER_LIST_ROWS = inline_serializer(
    name='OrderListRow',
    fields={
        'id': serializers.IntegerField(),
        'order_number': serializers.CharField(),
        # 'PARTY' | 'STAFF' | 'DISTRIBUTOR' for anything written since the
        # normaliser landed. Left as a plain string rather than an enum
        # because the column is not constrained and older rows are not
        # guaranteed to be one of the three.
        'order_type': serializers.CharField(),
        'employee_id': serializers.CharField(allow_null=True, allow_blank=True),
        'card_code': serializers.CharField(),
        'card_name': serializers.CharField(),
        # `str(order.total_amount)` — a STRING in the JSON, not a number.
        'total_amount': serializers.CharField(),
        # `OrderStatus.code`, not its id and not its display name.
        'status': serializers.CharField(),
        'status_display': serializers.CharField(),
        # Falls back to the latest successful SalesQuotationLog, then to ''.
        # Never null.
        'sap_doc_number': serializers.CharField(allow_blank=True),
        'items_count': serializers.IntegerField(),
        # `User.name` of the creator — null when the creator row was deleted
        # (the FK is SET_NULL).
        'created_by': serializers.CharField(allow_null=True),
        'created_at': serializers.DateTimeField(),
        'delivery_date': serializers.DateField(allow_null=True),
        'is_foc': serializers.BooleanField(),
    },
    many=True,
)


def with_items_count(orders):
    """Annotate `items_count` for `OrderListByUserIdSerializer`.

    Applied AFTER a view's filters. The rate-approver branch of the tracking
    view filters through `rate_approvals`, so the count is taken over a join;
    `distinct=True` keeps it a count of items whatever that join yields.
    """
    return orders.annotate(items_count=Count('items', distinct=True))


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

        
        accepted_data = OrderListByUserIdSerializer(
            with_items_count(accepted_orders.distinct()), many=True).data
        rejected_data = OrderListByUserIdSerializer(
            with_items_count(rejected_orders.distinct()), many=True).data

        for item in accepted_data:
            item['decision_type'] = 'accepted'
        for item in rejected_data:
            item['decision_type'] = 'rejected'

        return Response(sorted(
            [*accepted_data, *rejected_data],
            key=lambda item: item.get('created_at') or '',
            reverse=True,
        ))


@extend_schema(
    responses={200: ORDER_STATUS_OPTIONS},
    description='Every row of the order-status table as `{id, name}`. A bare '
                'array — no envelope, no pagination, and no `code`, which is '
                'the column the rest of the API keys off.',
)
class OrderStatusList(APIView):
    def get(self,request):
        status = OrderStatus.objects.all().values('id','name')
        return Response(list(status))

@extend_schema(
    responses={
        200: _OrdersLogSchema(many=True),
        404: OpenApiResponse(
            response=ORDERS_QUERY_ERROR,
            description='No order with this id (`get_object_or_404`).',
        ),
    },
    description='The audit timeline for one order, oldest first, with the '
                '"Order Created" row (action_id 1) excluded.\n\n'
                'Note `performed_by_name`: an order sitting at "Rate approval" '
                'has its latest rate-approval log blanked to '
                '`performed_by=None` (or a synthetic pending row created) '
                'before serialising, and DRF OMITS the `performed_by_name` key '
                'entirely for such a row rather than sending null. Treat it as '
                'possibly-absent, not merely nullable.',
)
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

@extend_schema(
    responses={
        200: _OrderDetailSchema,
        404: OpenApiResponse(
            response=ORDERS_QUERY_ERROR,
            description='No order with this id (`get_object_or_404`).',
        ),
    },
    description='One order with its items. Mounted twice — '
                '`/orderdetailsbyid/{order_id}/` and '
                '`/{order_id}/orderdetails/` — and both routes answer with the '
                'same `OrderDetailSerializer` body. Not scoped to the caller: '
                'any authenticated user may read any order id.',
)
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


@extend_schema(
    responses={
        200: _OrderListByUserIdSchema(many=True),
        403: OpenApiResponse(
            response=ORDERS_BY_USER_FORBIDDEN,
            description='A non-privileged caller (anyone outside '
                        '`ORDERS_CROSS_USER_ROLES`, and not staff/superuser) '
                        'asked for another user\'s orders. The view writes '
                        'this body itself, so it is `{"detail": ...}` alone.',
        ),
    },
    description='Every order created by `user_id`, newest first. Roles in '
                '`ORDERS_CROSS_USER_ROLES` may read any user; everyone else '
                'only themselves, and gets 403 otherwise.',
)
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

        # No items prefetch: the serializer emits `items_count` only, which
        # the annotation supplies in the same query as the orders.
        orders = with_items_count(
            Order.objects.filter(created_by=user_id)
            .select_related("status", "created_by")
            .order_by("-created_at")
        )

        serializer = OrderListByUserIdSerializer(orders, many=True)

        return Response(serializer.data)


@extend_schema(
    responses={200: ORDER_LIST_ROWS},
    description='The approval queues and the dashboard list. A bare array of '
                'hand-built rows — no serializer describes it, and the shape '
                'is NOT `OrderListByUserIdSerializer`. Filtered by the '
                '`status`, `user_id`, `billing`, `include_sap` and '
                '`approval_pending` query parameters, but the row shape is the '
                'same on every branch.',
)
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


def _parse_date(value):
    """An ISO `YYYY-MM-DD` query param, or None.

    A malformed date is ignored rather than raising: these arrive from a date
    input whose value is empty until a full date is picked, and a half-typed
    one must not 400 the page the user is looking at.
    """
    value = (value or '').strip()
    if not value:
        return None
    return parse_date(value)


def _master_orders_for(user):
    """Every order the master page may show `user`, across all creators.

    NOT `_get_base_orders`. That answers "which orders is this user working
    on" — a billing user gets the billing stages, an approver gets what is
    pending on them — which is a queue, not an overview. The master page is
    the overview, so it starts from every order and narrows only by WHO the
    user is allowed to see, never by what stage the order is at.

    The narrowing is the holder's own categories. That is the fix for the bug
    this page shipped with: it was gated on `orders.sales.view_all`, and
    because `_get_base_orders` reads that same key, granting it so somebody
    could open this page also unscoped their queue and tracker. A BEVERAGES
    billing user went from 705 orders to all 3,060 — OIL included — everywhere
    in the app, not just here.

    Holders of `orders.sales.view_all` still see everything: that key means
    "no scoping" and this is the one place it should still say so.

    Category, and NOT main group. `_apply_billing_order_scope` narrows by both,
    which is right for a queue and wrong for an overview: an OIL user assigned
    the single main group ROI would see 137 of the 1,675 OIL orders, a page
    that hides 92% of its own subject. The complaint this fixes is about
    categories — a BEVERAGES user seeing OIL — so categories are what it
    filters on. It is read-only, and wider than that user's billing queue by
    design.

    A user with no category at all is unscoped here, which is
    `_apply_billing_order_scope`'s own rule too: no scope configured means no
    scope applied.
    """
    orders = Order.objects.all()
    if sees_all_orders(user):
        return orders
    categories = _get_user_category_names(user)
    if not categories:
        return orders
    return orders.filter(items__category__in=categories).distinct()


class MasterOrderListView(APIView):
    """The order master view: every order, who raised it, and its stage history.

    One row per order, carrying the whole `orders_log` trail inline, so the
    page can show where an order stands AND how it got there without an
    N+1 of `/orderlogs/` calls behind it.

    Gated on `orders.sales.view_all` — the key that already means "see every
    order, company-wide" and that `_shared.sees_all_orders` already reads. A
    second key meaning the same thing would be a second authority source,
    which is the failure PERMISSIONS.md exists to prevent. `_get_base_orders`
    is still what builds the queryset, so a non-holder who somehow reaches
    this view gets their own scope, never a leak.

    Reads `OrdersLog` directly rather than reusing `OrderLogsByOrderView`:
    that view CREATES a log row when it serves a rate-approval order
    (`queries.py`, the `latest_rate_log` branch), and a list endpoint must not
    write one row per page view.

    Unlike that view, action_id=1 (`Order Created`) is kept — "which stages
    has this been through" starts at creation.
    """

    ORDERING = {'created_at', 'updated_at', 'order_number', 'total_amount'}

    # No order carries a per-order assignee for these desks, so the stage's
    # responsible desk is the honest answer. Rate approval is the exception:
    # `order_rate_approvals` names actual people, so those are returned.
    #
    # Keyed in lowercase and looked up that way because `order_statuses.code`
    # is not consistently cased — every seeded row is upper, but `mart_approval`
    # (id 12, added in the DB rather than by migration) is lower.
    #
    # A code absent here yields no desk rather than a guess. `APPROVED` is the
    # deliberate one: it means rate-approved and moving on, but which desk it
    # moves to depends on the order's flow config (billing/auditor are
    # per-party toggles), so naming one here would be a guess.
    DESK_FOR_STAGE = {
        'billing': 'Billing',
        'billing_pending': 'Billing',
        'auditor_approval': 'Auditor',
        'need_approval': 'Rate approver',
        'rate_approval': 'Rate approver',
        'mart_approval': 'Mart approver',
        'created': 'Order creator',
        'draft': 'Order creator',
    }
    # A rejected or cancelled order sits in nobody's queue until its creator
    # reworks it, which is a new transition — not a pending stage.
    TERMINAL_CODES = {
        'completed', 'rejected', 'billing_rejected', 'so_cancelled',
    }

    def get_permissions(self):
        return [IsAuthenticated(), HasKey('orders.master.view')]

    def get(self, request):
        orders = (
            _master_orders_for(request.user)
            .select_related('status', 'created_by')
            .prefetch_related(
                Prefetch(
                    'logs',
                    queryset=OrdersLog.objects
                    .select_related('action', 'performed_by')
                    .order_by('created_at', 'id'),
                ),
                Prefetch(
                    'rate_approvals',
                    queryset=OrderRateApproval.objects
                    .filter(status='PENDING')
                    .select_related('approver'),
                    to_attr='pending_rate_approvals',
                ),
            )
        )

        # Either spelling: `/orders/status/` — the only status master the
        # frontend has — returns `{id, name}` and has never exposed `code`, so
        # a page built on it can only filter by id. Codes are accepted too
        # because they are what the rest of this module reasons in.
        status_filter = (request.query_params.get('status') or '').strip()
        if status_filter.isdigit():
            orders = orders.filter(status_id=int(status_filter))
        elif status_filter:
            orders = orders.filter(status__code__iexact=status_filter)

        created_by = (request.query_params.get('created_by') or '').strip()
        if created_by.isdigit():
            orders = orders.filter(created_by_id=int(created_by))

        # Inclusive on both ends, and on the DATE rather than the instant: a
        # user picking 17 Sep to 17 Sep means that whole day, not midnight to
        # midnight. `__date` resolves in the active timezone, so the day
        # boundary is the one the reader is actually in.
        date_from = _parse_date(request.query_params.get('date_from'))
        if date_from:
            orders = orders.filter(created_at__date__gte=date_from)

        date_to = _parse_date(request.query_params.get('date_to'))
        if date_to:
            orders = orders.filter(created_at__date__lte=date_to)

        search = (request.query_params.get('q') or '').strip()
        if search:
            orders = orders.filter(
                Q(order_number__icontains=search)
                | Q(card_name__icontains=search)
                | Q(card_code__icontains=search)
            )

        orders = orders.order_by(
            ordering_from(request, self.ORDERING, '-created_at'), '-id'
        )

        paginator = StandardPagination()
        page = paginator.paginate_queryset(orders, request, view=self)
        return paginator.get_paginated_response(
            [self._row(order) for order in page]
        )

    def _row(self, order):
        stages = [
            {
                'status_id': log.action_id,
                'status_code': getattr(log.action, 'code', None),
                'status_name': getattr(log.action, 'name', '') or '',
                'performed_by_name': getattr(log.performed_by, 'username', None),
                'remarks': (log.remarks or '').strip(),
                'at': log.created_at,
            }
            for log in order.logs.all()
        ]
        status_code = getattr(order.status, 'code', '') or ''

        return {
            'id': order.id,
            'order_number': order.order_number,
            'card_code': order.card_code,
            'card_name': order.card_name,
            'order_type': order.order_type,
            'is_foc': order.is_foc,
            'total_amount': order.total_amount,
            'created_at': order.created_at,
            'delivery_date': order.delivery_date,
            'created_by_id': order.created_by_id,
            'created_by_name': getattr(order.created_by, 'username', None),
            'status_code': status_code,
            'status_name': getattr(order.status, 'name', '') or '',
            # `orders` has no `stage_entered_at`, so the newest log IS the best
            # available answer to "since when". Falls back to order creation
            # for an order that has not moved yet.
            'stage_since': stages[-1]['at'] if stages else order.created_at,
            'pending_with': self._pending_with(order, status_code),
            'stages': stages,
            'sap_doc_number': order.sap_doc_number,
        }

    def _pending_with(self, order, status_code):
        code = status_code.strip().lower()
        if code in self.TERMINAL_CODES:
            return []
        if code == 'rate_approval':
            named = [
                approval.approver.username
                for approval in order.pending_rate_approvals
                if approval.approver_id
            ]
            if named:
                return named
        desk = self.DESK_FOR_STAGE.get(code)
        return [desk] if desk else []


class MasterOrderCreatorsView(APIView):
    """Who has raised orders the caller can see — the master page's creator filter.

    Derived from the orders in scope rather than read off the user roster.
    `users/list/` would have served a name list, but it answers a different
    question: it is every active account, most of which have never raised an
    order, and it serialises role/company/state/main-group per row for a
    dropdown that needs two fields.

    Scoping it through `_get_base_orders` also keeps the filter honest — the
    list can never name someone whose orders the caller is not allowed to see,
    so picking any entry returns rows.

    Not narrowed by the page's other filters: a facet list that shrinks as you
    filter cannot be used to change your mind.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKey('orders.master.view')]

    def get(self, request):
        creators = (
            _master_orders_for(request.user)
            .exclude(created_by=None)
            .values('created_by_id', 'created_by__username')
            .distinct()
            .order_by('created_by__username')
        )
        return Response([
            {'id': row['created_by_id'], 'username': row['created_by__username']}
            for row in creators
        ])
