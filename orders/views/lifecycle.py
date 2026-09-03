"""Placing an order and moving it through its flow — the write path.

This is what is left of the 5,919-line `orders/views.py` after plan item 3.1,
and it is the part that changes data: create, edit, advance, approve, reject.
It was called `_legacy` while it was a remainder; it is a domain now.

Two invariants live here and are the reason this file is worth reading before
editing it:

* `UpdateOrderStatusView.post` runs the whole transition inside
  `transaction.atomic()` with the order row held by `select_for_update()`. It
  previously ran with neither, across ~380 lines and several dependent writes,
  so two approvers acting at once could both pass the pending check and
  double-advance a document.
* notification delivery is deferred with `transaction.on_commit`, so a
  transaction that rolls back cannot leave users notified of something that
  did not happen.

The rules these views apply live in `orders.services` — status transitions,
scheme entries, rate approval, templates, stock. What remains here is request
parsing, orchestration and the response.

`UpdateOrderStatusView`'s own branch dispatch (plan item 3.2) now lives in
`orders.services.order_status.apply_order_status_transition`, moved there as
a pure extraction — same conditions, same side effects, same responses. This
view's job is just the request-facing part: validate, lock the row, hand off.
"""

from urllib import request
from drf_spectacular.utils import (
    OpenApiResponse,
    PolymorphicProxySerializer,
    extend_schema,
    inline_serializer,
)
from orders.serializers import OrderStatusUpdateSerializer, CreateOrderSerializer
from orders.models import Order, OrderStatus, log_order_action, OrderRateApproval, OrderItemApprovalMapping
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework import status
from datetime import datetime
from django.shortcuts import get_object_or_404
from django.http import Http404
from django.db import transaction
import logging
from orders.notifications import mark_order_notifications_read

logger = logging.getLogger(__name__)

from .notifications import send_order_notifications



from orders.services.order_flow import ORDER_FLOW_TYPE_ASM, ORDER_FLOW_TYPE_BILLING, _get_initial_flow_status, _get_next_order_flow_status, _get_order_flow_type_for_order, _get_order_primary_category, _get_party_flow_config
from orders.services.order_templates import _save_template_if_unique
from orders.services.rate_approval import (
    _get_rate_approval_reason,
    _rate_approval_remarks,
    assign_rate_approvers,
)
from orders.services.order_items import _create_order_item
from orders.services.order_status import (
    _close_or_create_status_log,
    _pending_log_user_for_status,
    apply_order_status_transition,
)
from ._shared import (
    MART_STATUS_PENDING_ID,
    _mart_approver_user,
    _mart_status,
)
BILLING_RESOLVED_CODES = ['BILLING_REJECTED', 'COMPLETED']


def _normalize_warehouse_code(value):
    """One warehouse for the whole order. SAP codes are short and upper-case."""
    return str(value or '').strip().upper()[:20]


def _normalize_order_type(value):
    order_type = str(value or 'PARTY').strip().upper()
    if order_type == 'STAFF':
        return 'STAFF'
    if order_type == 'DISTRIBUTOR':
        return 'DISTRIBUTOR'
    return 'PARTY'


# Mart / distributor flow constants — kept in one place so the queue, the create
# branch and the approve/reject endpoints never drift.
DISTRIBUTOR_COMPANY = '3'                 # every distributor order is company 3 (Mart)
DISTRIBUTOR_DISPATCH_ID = 2               # distributor orders dispatch from the factory
DISTRIBUTOR_DISPATCH_NAME = 'FACTORY'
DISTRIBUTOR_WAREHOUSE_CODE = 'GP-FGM'     # default warehouse for distributor orders


# ---------------------------------------------------------------------------
# OpenAPI response shapes (`@extend_schema` below). Documentation only — this
# section runs nothing and changes no response by one byte.
#
# Both write endpoints here branch heavily and their bodies are NOT uniform, so
# the declarations below are deliberately conservative:
#
# * A key is declared required only when EVERY branch that can produce that
#   status code writes it. Everything else is `required=False`, which is the
#   literal truth — the key is sometimes absent, not sometimes null.
# * Where two branches under the SAME status code disagree about a key's TYPE,
#   the declaration is a `oneOf` rather than a merged object, because merging
#   would have to pick one type and be wrong about the other.
#
# The error envelope matters here too. `core.exception_handler` fires only for
# RAISED exceptions, so `serializer.is_valid(raise_exception=True)` in
# `UpdateOrderStatusView` produces field errors PLUS `detail`/`message`/
# `error`/`success`, while `CreateOrderView`'s `return Response(
# serializer.errors, 400)` produces the bare errors dict with no envelope.
# ---------------------------------------------------------------------------

#: What `core.exception_handler` makes of a raised `Http404` — from
#: `get_object_or_404(Order, ...)` on the edit path, `get_object_or_404(
#: OrderStatus, ...)` inside the transition, or `raise Http404('Order not
#: found')` in `UpdateOrderStatusView`.
ORDER_LIFECYCLE_ERROR = inline_serializer(
    name='OrderLifecycleError',
    fields={
        'detail': serializers.CharField(),
        'message': serializers.CharField(),
        'error': serializers.CharField(),
        'success': serializers.BooleanField(),
    },
)

#: Every 200 `apply_order_status_transition` can return. `message`, `order_id`
#: and `status` are on all of them; the rate-approval branches add
#: `approval_status`, and the "still waiting on other approvers" branch alone
#: adds `pending_approvers`.
#:
#: `status` is a status NAME, not a code and not an id — and on the
#: "waiting for remaining approvers" branch it is the PREVIOUS status's name,
#: because that branch deliberately rolls the order back.
ORDER_STATUS_UPDATE_OK = inline_serializer(
    name='OrderStatusUpdateResult',
    fields={
        'message': serializers.CharField(),
        'order_id': serializers.IntegerField(),
        'status': serializers.CharField(allow_blank=True),
        # Rate-approval branches only: 'APPROVED' or 'REJECTED'.
        'approval_status': serializers.CharField(required=False),
        # Only when a multi-approver order still has approvers outstanding.
        'pending_approvers': serializers.ListField(
            child=serializers.CharField(), required=False,
        ),
    },
)

#: 403 — two different producers, and they do not share a key.
ORDER_STATUS_UPDATE_FORBIDDEN = inline_serializer(
    name='OrderStatusUpdateForbidden',
    fields={
        # The view's own branch: the order is not assigned to this user for
        # rate approval. Written explicitly, so `message` is the only key.
        'message': serializers.CharField(required=False),
        # DRF's permission/authentication layer, via
        # `core.exception_handler` — that one carries the full envelope.
        'detail': serializers.CharField(required=False),
        'error': serializers.CharField(required=False),
        'success': serializers.BooleanField(required=False),
    },
)

#: 400, shape one: the approver has already decided this order. Returned
#: explicitly, so no envelope keys.
_ORDER_STATUS_ALREADY_DECIDED = inline_serializer(
    name='OrderStatusAlreadyDecided',
    fields={
        'message': serializers.CharField(),
        'order_id': serializers.IntegerField(),
        'status': serializers.CharField(allow_blank=True),
        'approval_status': serializers.CharField(),
    },
)

#: 400, shape two: `OrderStatusUpdateSerializer` rejected the request body.
#: Raised, so the envelope is added on top of the field errors. Note `status`
#: is a LIST of messages here and a plain string in the shape above — which is
#: precisely why these two cannot be merged into one object.
_ORDER_STATUS_UPDATE_INVALID = inline_serializer(
    name='OrderStatusUpdateInvalid',
    fields={
        'status': serializers.ListField(
            child=serializers.CharField(), required=False,
        ),
        'reason': serializers.ListField(
            child=serializers.CharField(), required=False,
        ),
        'detail': serializers.CharField(),
        'message': serializers.CharField(),
        'error': serializers.CharField(),
        'success': serializers.BooleanField(),
    },
)

ORDER_STATUS_UPDATE_BAD_REQUEST = PolymorphicProxySerializer(
    component_name='OrderStatusUpdateBadRequest',
    serializers=[_ORDER_STATUS_ALREADY_DECIDED, _ORDER_STATUS_UPDATE_INVALID],
    resource_type_field_name=None,
)

#: `CreateOrderView`'s 200 — the three EDIT branches (staff update, standard
#: update, distributor edit). Six keys are common to all three; the rest depend
#: on which branch ran.
ORDER_EDIT_RESULT = inline_serializer(
    name='OrderEditResult',
    fields={
        'id': serializers.IntegerField(),
        'order_number': serializers.CharField(),
        # `str(order.total_amount)` — a STRING, not a number.
        'total_amount': serializers.CharField(),
        # The status NAME, or '' when the order somehow has no status.
        'status': serializers.CharField(allow_blank=True),
        'needs_approval': serializers.BooleanField(),
        'message': serializers.CharField(),
        # Staff-update and distributor-edit branches only.
        'order_type': serializers.CharField(required=False),
        # Staff-update branch only.
        'employee_id': serializers.CharField(
            required=False, allow_null=True, allow_blank=True,
        ),
        # Distributor-edit branch only — forced to '3' (Mart).
        'company': serializers.CharField(
            required=False, allow_null=True, allow_blank=True,
        ),
    },
)

#: `CreateOrderView`'s 201 — the three CREATE branches (staff, standard,
#: distributor). Same six common keys; `flagged_items` and `remarks` are sent
#: by the two non-distributor branches, `company` only by the distributor one.
ORDER_CREATE_RESULT = inline_serializer(
    name='OrderCreateResult',
    fields={
        'id': serializers.IntegerField(),
        'order_number': serializers.CharField(),
        'total_amount': serializers.CharField(),
        'status': serializers.CharField(allow_blank=True),
        'needs_approval': serializers.BooleanField(),
        'message': serializers.CharField(),
        # Staff-create and distributor-create branches only.
        'order_type': serializers.CharField(required=False),
        # Staff-create branch only.
        'employee_id': serializers.CharField(
            required=False, allow_null=True, allow_blank=True,
        ),
        # Distributor-create branch only.
        'company': serializers.CharField(
            required=False, allow_null=True, allow_blank=True,
        ),
        # Staff-create ([] always) and standard-create. One human-readable
        # sentence per line whose entered rate differs from the price list.
        'flagged_items': serializers.ListField(
            child=serializers.CharField(), required=False,
        ),
        'remarks': serializers.CharField(required=False, allow_blank=True),
    },
)

#: `CreateOrderView`'s 400. Two producers, and only one of them has a shape
#: worth naming, so this is written as a raw schema rather than forced through
#: a serializer:
#:
#: * the four explicit guards (no items, staff order without employee_id,
#:   party order without card_code) — `{'error': <sentence>}` and nothing else,
#:   because the view returns it rather than raising it;
#: * `return Response(serializer.errors, 400)` — the raw DRF error dict, keyed
#:   by whichever `CreateOrderSerializer` fields failed, values being lists of
#:   messages, except `items` (a ListField of DictField) whose errors nest per
#:   index. That half is left as a free-form object ON PURPOSE: the key set is
#:   not fixed, and inventing one would be worse than admitting it is dynamic.
ORDER_CREATE_BAD_REQUEST = {
    'oneOf': [
        {
            'type': 'object',
            'properties': {'error': {'type': 'string'}},
            'required': ['error'],
            'description': 'One of the four explicit guards in the view.',
        },
        {
            'type': 'object',
            'additionalProperties': True,
            'description': (
                'Raw DRF validation errors from CreateOrderSerializer, keyed '
                'by field name. Deliberately untyped: which keys appear '
                'depends on which fields failed, and `items` errors nest by '
                'index. No error envelope — this branch returns the errors '
                'dict directly rather than raising.'
            ),
        },
    ],
}


class UpdateOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, order_id):
        def _to_float(value, default=0.0):
            try:
                if value in (None, ''):
                    return float(default)
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        def _to_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            if value in (None, ""):
                return False
            return bool(value)

        order = get_object_or_404(Order, id=order_id)
        previous_status = order.status
        order_flow_type = _get_order_flow_type_for_order(order)

        serializer = CreateOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        order_type = _normalize_order_type(request.data.get('order_type', order.order_type))
        employee_id = str(data.get('employee_id') or order.employee_id or '').strip()
        items = data.pop('items', [])
        order_remarks = request.data.get('remarks', data.get('remarks', ''))

        if not items:
            return Response({'error': 'At least one item is required'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'STAFF' and not employee_id:
            return Response({'error': 'employee_id is required for staff orders'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'PARTY' and not data.get('card_code'):
            return Response({'error': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Update order header fields
        order.order_type = order_type
        order.employee_id = employee_id if order_type == 'STAFF' else data.get('employee_id', order.employee_id)
        order.card_code = data.get('card_code') or ('STAFF' if order_type == 'STAFF' else order.card_code)
        order.card_name = data.get('card_name') or (employee_id if order_type == 'STAFF' else order.card_name)
        order.bill_to_id = data.get('bill_to_id') or order.bill_to_id
        order.bill_to_address = data.get('bill_to_address', order.bill_to_address)
        order.ship_to_id = data.get('ship_to_id') or order.ship_to_id
        order.ship_to_address = data.get('ship_to_address', order.ship_to_address)
        order.dispatch_from_id = data.get('dispatch_from_id') or order.dispatch_from_id
        order.dispatch_from_name = data.get('dispatch_from_name', order.dispatch_from_name)
        order.company = data.get('company', order.company)
        order.po_number = data.get('po_number', order.po_number)
        order.warehouse_code = _normalize_warehouse_code(
            data.get('warehouse_code', order.warehouse_code))
        order.is_foc = data.get('is_foc', order.is_foc)
        order.delivery_date = data.get('delivery_date') or order.delivery_date
        order.remarks = order_remarks

        # Replace items
        order.items.all().delete()

        needs_approval = False
        flagged_items = []

        created_items = []
        for item in items:
            created_items.append(_create_order_item(order, item, _to_float, _to_bool))

            bp = _to_float(item.get('price_list_basic', 0))
            mp = _to_float(item.get('basic_price', 0))
            rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
            if rate_approval_reason:
                needs_approval = True
                flagged_items.append(rate_approval_reason)


        order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)

        user = request.user if request.user.is_authenticated else None
        if order_type == 'STAFF':
            order.status = previous_status or get_status('Order Created')
            order.save()
            mark_order_notifications_read(order, user)
            log_order_action(order, 'Order Created', user=user, remarks='Staff order updated')
            return Response({
                'id': order.id,
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'status': order.status.name if order.status else '',
                'order_type': order.order_type,
                'employee_id': order.employee_id,
                'needs_approval': False,
                'message': 'Staff order updated successfully',
            }, status=status.HTTP_200_OK)

        editor_role = getattr(getattr(user, "role", None), "name", "").lower() if user else ""
        is_billing_editor = editor_role == "billing"
        order_flow_type = _get_order_flow_type_for_order(order)

        if is_billing_editor:
            next_status = _get_next_order_flow_status(order, previous_status, 'Auditor Approval', flow_type=order_flow_type)
            flow_needs_approval = False
        else:
            next_status, flow_needs_approval = _get_initial_flow_status(
                items,
                _to_float,
                flow_type=order_flow_type,
                force_foc_flow=order.is_foc,
                config=_get_party_flow_config(order.card_code, order_flow_type, _get_order_primary_category(items)),
            )
        if next_status:
            order.status = next_status


        order.save()

        mark_order_notifications_read(order, user)
        if next_status:
            send_order_notifications(order, next_status.name, actor=user, previous_status=previous_status)
        

        log_action = next_status.name if next_status else 'Order Created'
        log_remarks = 'Sent to auditor' if is_billing_editor else _rate_approval_remarks(flagged_items) if flow_needs_approval else ''
        if is_billing_editor:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks="Accepted by billing"
            )
        log_user = _pending_log_user_for_status(next_status, user) if next_status else user
        log_order_action(order, log_action, user=log_user, remarks=log_remarks)

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': str(order.total_amount),
            'status': order.status.name if order.status else '',
            'needs_approval': False if is_billing_editor else flow_needs_approval,
            'message': f"Order updated and sent to {next_status.name.lower()}" if next_status else 'Order updated successfully',
        }, status=status.HTTP_200_OK)

@extend_schema(
    request=CreateOrderSerializer,
    responses={
        200: OpenApiResponse(
            response=ORDER_EDIT_RESULT,
            description='An EDIT — the request carried an `order_id`. Three '
                        'branches land here (staff update, standard update, '
                        'distributor edit) and their key sets differ; see the '
                        'per-field notes on the schema.',
        ),
        201: OpenApiResponse(
            response=ORDER_CREATE_RESULT,
            description='A CREATE — no `order_id` in the request. Three '
                        'branches land here (staff, standard, distributor) and '
                        'their key sets differ; see the per-field notes on the '
                        'schema.',
        ),
        400: ORDER_CREATE_BAD_REQUEST,
        404: OpenApiResponse(
            response=ORDER_LIFECYCLE_ERROR,
            description='Edit mode only: the `order_id` in the body matches no '
                        'order (`get_object_or_404`).',
        ),
    },
    description='The single order-write endpoint: create, save-as-draft and '
                'edit all post here, and an `order_id` in the BODY (not the '
                'URL) is what makes it an edit.\n\n'
                'The response is not one shape. Six success branches exist — '
                'staff/standard/distributor, each for create and edit — and '
                'they agree on `id`, `order_number`, `total_amount`, `status`, '
                '`needs_approval` and `message` only. `order_type`, '
                '`employee_id`, `company`, `flagged_items` and `remarks` are '
                'each sent by some branches and not others, and the create and '
                'edit paths are separated by status code (201 vs 200).',
)
class CreateOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        def _to_float(value, default=0.0):
            try:
                if value in (None, ''):
                    return float(default)
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        def _to_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            if value in (None, ""):
                return False
            return bool(value)

        # ── Edit mode: order_id in payload means update existing order ──────
        order_id = request.data.get('order_id')
        user = request.user if request.user.is_authenticated else None
        if order_id:
            order = get_object_or_404(Order, id=int(order_id))
            previous_status = order.status
            serializer = CreateOrderSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            data = serializer.validated_data
            order_type = _normalize_order_type(request.data.get('order_type', order.order_type))
            employee_id = str(data.get('employee_id') or order.employee_id or '').strip()
            items = data.pop('items', [])
            order_remarks = request.data.get('remarks', data.get('remarks', ''))
            if not items:
                return Response({'error': 'At least one item is required'}, status=status.HTTP_400_BAD_REQUEST)
            if order_type == 'STAFF' and not employee_id:
                return Response({'error': 'employee_id is required for staff orders'}, status=status.HTTP_400_BAD_REQUEST)
            if order_type == 'PARTY' and not data.get('card_code'):
                return Response({'error': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)
        
            order.order_type = order_type
            order.employee_id = employee_id if order_type == 'STAFF' else data.get('employee_id', order.employee_id)
            order.card_code = data.get('card_code') or ('STAFF' if order_type == 'STAFF' else order.card_code)
            order.card_name = data.get('card_name') or (employee_id if order_type == 'STAFF' else order.card_name)
            order.bill_to_id = data.get('bill_to_id') or order.bill_to_id
            order.bill_to_address = data.get('bill_to_address', order.bill_to_address)
            order.ship_to_id = data.get('ship_to_id') or order.ship_to_id
            order.ship_to_address = data.get('ship_to_address', order.ship_to_address)
            order.dispatch_from_id = data.get('dispatch_from_id') or order.dispatch_from_id
            order.dispatch_from_name = data.get('dispatch_from_name', order.dispatch_from_name)
            order.company = data.get('company', order.company)
            order.po_number = data.get('po_number', order.po_number)
            order.warehouse_code = _normalize_warehouse_code(
                data.get('warehouse_code', order.warehouse_code))
            order.is_foc = data.get('is_foc', order.is_foc)
            order.delivery_date = data.get('delivery_date') or order.delivery_date
            order.remarks = order_remarks

            # Distributor edit (Mart Approval role adjusting the order): re-save the
            # lines and keep it in the Mart flow — never route through billing.
            if order_type == 'DISTRIBUTOR':
                return self._finalize_distributor_order(
                    order, items, user, _to_float, _to_bool,
                    is_edit=True, previous_status=previous_status,
                )

            order.items.all().delete()

            needs_approval = False
            flagged_items = []
            created_items = []
            for item in items:
                created_items.append(_create_order_item(order, item, _to_float, _to_bool))
                bp = _to_float(item.get('price_list_basic', 0))
                mp = _to_float(item.get('basic_price', 0))
                rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
                if rate_approval_reason:
                    needs_approval = True
                    flagged_items.append(rate_approval_reason)

            assign_rate_approvers(order)

            order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)
          
            if order_type == 'STAFF':
                order.status = previous_status or get_status('Order Created')
                order.save()
                mark_order_notifications_read(order, user)
                log_order_action(order, 'Order Created', user=user, remarks='Staff order updated')
                return Response({
                    'id': order.id,
                    'order_number': order.order_number,
                    'total_amount': str(order.total_amount),
                    'status': order.status.name if order.status else '',
                    'order_type': order.order_type,
                    'employee_id': order.employee_id,
                    'needs_approval': False,
                    'message': 'Staff order updated successfully',
                }, status=status.HTTP_200_OK)

            editor_role = getattr(getattr(user, "role", None), "name", "").lower() if user else ""
            is_billing_editor = editor_role == "billing"
            order_flow_type = _get_order_flow_type_for_order(order)

            if is_billing_editor:
                next_status = _get_next_order_flow_status(order, previous_status, 'Auditor Approval', flow_type=order_flow_type)
                flow_needs_approval = False
            else:
                next_status, flow_needs_approval = _get_initial_flow_status(
                    items,
                    _to_float,
                    flow_type=order_flow_type,
                    force_foc_flow=order.is_foc,
                    config=_get_party_flow_config(order.card_code, order_flow_type, _get_order_primary_category(items)),
                )

            # If the edit sends the order back into Rate Approval, a fresh approval
            # round begins (a new pending rate-approval log is created below), so any
            # prior approver decisions must be cleared back to PENDING. Otherwise the
            # edit bypasses rate approval and existing decisions are preserved by
            # assign_rate_approvers().
            if next_status and (next_status.name or "").strip().lower() == "rate approval":
                OrderRateApproval.objects.filter(order=order).update(
                    status="PENDING",
                    approved_at=None,
                    remarks="",
                )
            if next_status:
                order.status = next_status
            order.save()

            mark_order_notifications_read(order, user)
            if next_status:
                send_order_notifications(order, next_status.name, actor=user, previous_status=previous_status)
           

            log_action = next_status.name if next_status else 'Order Created'
            log_remarks = 'Sent to auditor' if is_billing_editor else _rate_approval_remarks(flagged_items) if flow_needs_approval else ''
            if is_billing_editor:
                _close_or_create_status_log(
                    order=order,
                    action_status=previous_status,
                    user=user,
                    remarks="Accepted by billing"
                )
            log_user = _pending_log_user_for_status(next_status, user) if next_status else user
            log_order_action(order, log_action, user=log_user, remarks=log_remarks)

            # Save every unique order as a template, but skip true duplicates.
            _save_template_if_unique(user, order)

            return Response({
                'id': order.id,
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'status': order.status.name if order.status else '',
                'needs_approval': False if is_billing_editor else flow_needs_approval,
                'message': f"Order updated and sent to {next_status.name.lower()}" if next_status else 'Order updated successfully',
            }, status=status.HTTP_200_OK)

        serializer = CreateOrderSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        order_type = _normalize_order_type(data.get('order_type'))
        employee_id = str(data.get('employee_id') or '').strip()
        items = data.pop('items', [])
        # Keep remarks resilient even if serializer/view code drifts across deployments.
        order_remarks = request.data.get('remarks', data.get('remarks', ''))

        if not items:
            return Response({'error': 'At least one item is required'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'STAFF' and not employee_id:
            return Response({'error': 'employee_id is required for staff orders'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'PARTY' and not data.get('card_code'):
            return Response({'error': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Generate order number: ORD-YYYYMMDD-XXXX
        today = datetime.now().strftime('%Y%m%d')
        last_order = Order.objects.filter(
            order_number__startswith=f'ORD-{today}'
        ).order_by('-order_number').first()

        if last_order:
            last_num = int(last_order.order_number.split('-')[-1])
            new_num = last_num + 1
        else:
            new_num = 1

        order_number = f'ORD-{today}-{new_num:04d}'
        total_amount = sum(_to_float(item.get('total', 0)) for item in items)
        user = request.user if request.user.is_authenticated else None
        order = Order.objects.create(
            order_number=order_number,
            order_type=order_type,
            employee_id=employee_id,
            card_code=data.get('card_code') or ('STAFF' if order_type == 'STAFF' else ''),
            card_name=data.get('card_name') or (employee_id if order_type == 'STAFF' else ''),
            bill_to_id=data.get('bill_to_id'),
            bill_to_address=data.get('bill_to_address', ''),
            ship_to_id=data.get('ship_to_id'),
            ship_to_address=data.get('ship_to_address', ''),
            dispatch_from_id=data.get('dispatch_from_id'),
            dispatch_from_name=data.get('dispatch_from_name', ''),
            company=data.get('company', ''),
            po_number=data.get('po_number', ''),
            warehouse_code=_normalize_warehouse_code(data.get('warehouse_code')),
            is_foc=data.get('is_foc', False),
            total_amount=total_amount,
            status=get_status('Order Created'),
            created_by=user,
            delivery_date=data.get('delivery_date'),
            remarks=order_remarks

        )

        # ── Distributor (Mart / company 3) orders take their OWN path ────────
        # They never enter the billing / auditor / rate-approval flow: they are
        # simply recorded and parked at "Mart Approval" for the Mart Approval
        # role to review. This keeps the entire billing flow below untouched.
        if order_type == 'DISTRIBUTOR':
            return self._finalize_distributor_order(order, items, user, _to_float, _to_bool)

        needs_approval = False
        flagged_items = []

        created_items = []
        for item in items:
            created_items.append(_create_order_item(order, item, _to_float, _to_bool))
            bp = _to_float(item.get('price_list_basic', 0))
            mp = _to_float(item.get('basic_price', 0))
            rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
            if rate_approval_reason:
                needs_approval = True
                flagged_items.append(rate_approval_reason)


        OrderRateApproval.objects.filter(order=order).delete()
        OrderItemApprovalMapping.objects.filter(order=order).delete()

        assign_rate_approvers(order)

        # Save party orders as reusable templates, but keep staff orders separate.
        if order_type != 'STAFF':
            _save_template_if_unique(user, order)

        # Log: Order created
        log_order_action(order, 'Order Created', user=user)

        if order_type == 'STAFF':
            return Response({
                'id': order.id,
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'status': order.status.name if order.status else '',
                'order_type': order.order_type,
                'employee_id': order.employee_id,
                'needs_approval': False,
                'flagged_items': [],
                'remarks': order.remarks or '',
                'message': 'Staff order created successfully',
            }, status=status.HTTP_201_CREATED)

        creator_role = getattr(getattr(user, "role", None), "name", "").lower() if user else ""
        is_billing_creator = creator_role == "billing"
        order_flow_type = ORDER_FLOW_TYPE_BILLING if is_billing_creator else ORDER_FLOW_TYPE_ASM

        # A party-specific flow override (for this role's flow type + order category)
        # fully replaces the global ASM/BILLING flow.
        party_flow_config = _get_party_flow_config(
            order.card_code, order_flow_type, _get_order_primary_category(items)
        )

        next_status, flow_needs_approval = _get_initial_flow_status(
            items,
            _to_float,
            flow_type=order_flow_type,
            force_foc_flow=order.is_foc,
            config=party_flow_config,
        )
        if next_status:
            order.status = next_status
            order.save()
            send_order_notifications(order, next_status.name, actor=user)

        log_user = _pending_log_user_for_status(next_status, user) if next_status else user
        log_remarks = _rate_approval_remarks(flagged_items) if flow_needs_approval else ''
        if next_status:
            log_order_action(order, next_status.name, user=log_user, remarks=log_remarks)

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': str(order.total_amount),
            'status': order.status.name if order.status else '',
            'needs_approval': flow_needs_approval,
            'flagged_items': flagged_items if flow_needs_approval else [],
            'remarks': order.remarks or '',
            'message': f"Order sent to {next_status.name.lower()}" if next_status else 'Order created successfully',
        }, status=status.HTTP_201_CREATED)

    def _finalize_distributor_order(self, order, items, user, _to_float, _to_bool,
                                    is_edit=False, previous_status=None):
        """Save a distributor order's lines and park it at 'Mart Approval'.

        Deliberately isolated from the billing/auditor/rate-approval machinery:
        no rate approvers, no templates, no billing notifications. Company is
        forced to 3 (Mart). Used by both the create and edit paths.
        """
        if is_edit:
            order.items.all().delete()

        # The distributor page now builds full, billing-shaped line items
        # (brand / variety / type / pcs / boxes / ltrs / tax computed exactly
        # like Add Sales), so a distributor order stores the SAME data as a
        # normal order. Use the standard item creator for both create and edit.
        for item in items:
            _create_order_item(order, item, _to_float, _to_bool)

        order.company = DISTRIBUTOR_COMPANY
        # Distributor orders always dispatch from the factory.
        order.dispatch_from_id = DISTRIBUTOR_DISPATCH_ID
        order.dispatch_from_name = DISTRIBUTOR_DISPATCH_NAME
        # Default every distributor order to GP-FGM, regardless of what the client
        # sent, so the warehouse is always stored. An explicit choice (e.g. Mart
        # Approval switching to DL-MP on the edit screen) is preserved.
        if not (order.warehouse_code or '').strip():
            order.warehouse_code = DISTRIBUTOR_WAREHOUSE_CODE
        order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)

        # Land at the Mart Approval stage (status id 12). On a fresh submit we
        # always move it there; on an approver edit we only reset it to pending
        # if it isn't already an approved/rejected order.
        mart_status = _mart_status(MART_STATUS_PENDING_ID)
        if mart_status and (not is_edit or order.status_id == MART_STATUS_PENDING_ID):
            order.status = mart_status
        order.save()

        if user:
            mark_order_notifications_read(order, user)

        if not is_edit:
            # Two-row create log, mirroring the billing flow:
            #   • 'Order Created' (action_id 1), performed_by = the distributor.
            #   • the Mart Approval stage (action_id 12), performed_by = the
            #     Mart Approval user — a "pending" marker showing who the order is
            #     waiting on. The approve/reject step then adds its own row.
            log_order_action(order, 'Order Created', user=user,
                             remarks='Distributor order submitted')
            if order.status:
                log_order_action(order, order.status.name,
                                 user=_mart_approver_user())

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': str(order.total_amount),
            'status': order.status.name if order.status else '',
            'order_type': order.order_type,
            'company': order.company,
            'needs_approval': False,
            'message': 'Distributor order updated successfully'
            if is_edit else 'Distributor order submitted for Mart approval',
        }, status=status.HTTP_200_OK if is_edit else status.HTTP_201_CREATED)

@extend_schema(
    request=OrderStatusUpdateSerializer,
    responses={
        200: OpenApiResponse(
            response=ORDER_STATUS_UPDATE_OK,
            description='The transition was applied — or, in the '
                        '"already rejected" and "waiting for remaining '
                        'approvers" branches, deliberately not applied. Every '
                        '200 branch sends `message`, `order_id` and `status`; '
                        'the rate-approval ones add `approval_status`, and one '
                        'of them adds `pending_approvers`.',
        ),
        400: ORDER_STATUS_UPDATE_BAD_REQUEST,
        403: OpenApiResponse(
            response=ORDER_STATUS_UPDATE_FORBIDDEN,
            description='Either the order is not assigned to this user for '
                        'rate approval (the view\'s own `{"message": ...}`) or '
                        'DRF refused the request (the '
                        '`detail`/`message`/`error`/`success` envelope). The '
                        'two do not share a key, so both are optional here.',
        ),
        404: OpenApiResponse(
            response=ORDER_LIFECYCLE_ERROR,
            description='No order with this id, or no order status with the '
                        'posted `status` id.',
        ),
    },
    description='Advance or reject one order. The body is `{status: <status '
                'id>, reason?: <text>}`.\n\n'
                'The response body is built by '
                '`orders.services.order_status.apply_order_status_transition`, '
                'not by this view, and that function has nine return points. '
                'The posted `status` id is also NOT always the status the '
                'order ends up in: the flow configuration can override it, and '
                'the multi-approver branch rolls it back — so read the `status` '
                'NAME in the response rather than assuming the one you sent.',
)
class UpdateOrderStatusView(APIView):
    """Advance (or reject) an order through its configured status flow.

    Every transition runs under `transaction.atomic()` with the order row held
    by `select_for_update()`. It previously ran with neither, across ~380 lines
    and several dependent writes — the defect `approvals/services.py` names in
    its own opening docstring as the reason its design differs:

        "UpdateOrderStatusView.post is neither, across ~380 lines and several
         dependent writes — so two approvers acting at once can both pass the
         pending check and double-advance a document."

    That is exactly right, and the multi-approver rate-approval path is where
    it bit hardest. Two approvers submitting together both read
    `rate_approval.status == "PENDING"`, both record a decision, and both then
    ask `_has_pending_rate_approvals` — which by then answers "none pending" for
    both. The order advanced twice, with two status logs and two notification
    fan-outs. The guards were all present and all correct; they were simply
    reading uncommitted state.

    The lock serialises them. The second approver now blocks, re-reads
    committed state, and takes the branch that was always meant for them:
    "You have already approved this order."

    Two things had to be true before locking was safe:

    * **No network calls inside the transaction.** `send_order_notifications`
      is called from nine points in this method and ends in
      `requests.post(..., timeout=15)` per recipient. Holding a row lock across
      that would trade a rare silent corruption for a routine hang. It now
      defers through `transaction.on_commit` — see that function.

    * **The hand-rolled rollbacks still work.** Several paths save the new
      status and then restore `previous_status` on failure. Inside
      `atomic()`, `return Response(...)` does NOT roll back — only an exception
      does — so those restores are still doing the work. Leave them, and
      do not convert them to exceptions without reading each one first.

    What atomic() DOES fix for free is the crash case: the method saved the new
    status at the top and validated entitlement ~60 lines later, so a process
    death in between left an order advanced with no approval recorded. That
    window is now rolled back by the database.

    Deliberately unchanged: the 71 sites keying business logic off mutable
    status NAMES, and the hardcoded status ids. Both are real problems and both
    are a separate change — locking a fragile flow is still strictly better
    than leaving it racy.

    The branch dispatch described above — the rate-approval race fix, the
    auditor/billing handoffs, the rejection and completion paths — is now
    `orders.services.order_status.apply_order_status_transition`, moved out
    verbatim (plan item 3.2). This method still does the row lock and the
    request parsing, then hands off.
    """

    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, order_id):
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Locked for the rest of the method. A second transition on the SAME
        # order waits here; transitions on other orders are unaffected, since
        # this is a row lock.
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            raise Http404('Order not found')

        return apply_order_status_transition(
            order=order,
            status_id=serializer.validated_data["status"],
            reason=serializer.validated_data.get("reason", ""),
            user=request.user if request.user.is_authenticated else None,
        )

_status_cache = {}

def get_status(name):
    if name not in _status_cache:
        try:
            _status_cache[name] = OrderStatus.objects.get(name=name)
        except OrderStatus.DoesNotExist:
            return None
    return _status_cache[name]

class ApproveOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
    
        if order.status.name != 'Order Created':
            return Response(
                {'error': f'Cannot approve. Current status: {order.status.name}'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        order.status = get_status('Approved')
        order.approved_by = request.user if request.user.is_authenticated else None
        order.approved_at = datetime.now()
        order.save()

        mark_order_notifications_read(
            order,
            request.user if request.user.is_authenticated else None,
        )

        if order.status:
            send_order_notifications(
                order,
                'Approved',
                actor=request.user if request.user.is_authenticated else None,
            )

    
        return Response({
            'message': f'Order {order.order_number} approved successfully',
            'order_number': order.order_number,
            'status': order.status,
        })

class RejectOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
    
        if order.status.name != 'Order Created':
            return Response(
                {'error': f'Cannot reject. Current status: {order.status.name}'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        reason = request.data.get('reason', '')
    
        if not reason:
            return Response(
                {'error': 'Rejection reason is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        order.status = get_status('Rejected')
        order.rejected_by = request.user if request.user.is_authenticated else None
        order.rejected_at = datetime.now()
        order.rejection_reason = reason
        order.save()

        mark_order_notifications_read(
            order,
            request.user if request.user.is_authenticated else None,
        )
        if order.status:
            send_order_notifications(
                order,
                'Rejected',
                actor=request.user if request.user.is_authenticated else None,
            )
        
        
    
        return Response({
            'message': f'Order {order.order_number} rejected',
            'order_number': order.order_number,
            'status': order.status,
        })
