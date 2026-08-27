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
"""

from urllib import request
from orders.serializers import OrderStatusUpdateSerializer, CreateOrderSerializer
from orders.models import OrdersLog, Order, OrderStatus, log_order_action, OrderRateApproval, OrderItemApprovalMapping
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.http import Http404
from django.db import transaction
import logging
from orders.notifications import mark_order_notifications_read

logger = logging.getLogger(__name__)

from .notifications import _display_user_name, send_order_notifications



from orders.services.order_flow import ORDER_FLOW_TYPE_ASM, ORDER_FLOW_TYPE_BILLING, _get_initial_flow_status, _get_next_order_flow_status, _get_order_flow_type_for_order, _get_order_primary_category, _get_party_flow_config, _is_completed_status, _is_rejection_status, _order_status_message
from orders.services.order_templates import _save_template_if_unique
from orders.services.rate_approval import (
    APPROVER_REJECTED_ACTION_ID,
    _get_order_rate_approval,
    _get_rate_approval_reason,
    _has_pending_rate_approvals,
    _mark_rate_approval_decision,
    _rate_approval_remarks,
    assign_rate_approvers,
)
from orders.services.order_items import _create_order_item
from ._shared import (
    MART_STATUS_PENDING_ID,
    _assigned_rate_approvers_for_order,
    _mart_approver_user,
    _mart_status,
)
BILLING_RESOLVED_CODES = ['BILLING_REJECTED', 'COMPLETED']

def _pending_log_user_for_status(status_obj, actor_user=None):
    return actor_user if _is_completed_status(status_obj) else None


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

        previous_status = order.status
        order_flow_type = _get_order_flow_type_for_order(order)

        status_id = serializer.validated_data["status"]
        reason = serializer.validated_data.get("reason", "")
    
        # ✅ Fetch status dynamically from table
        status_obj = get_object_or_404(OrderStatus, id=status_id)

        prev_name = (previous_status.name or "").strip().lower() if previous_status else ""

        if (
            previous_status
            and previous_status.id == status_obj.id
            and _is_rejection_status(status_obj)
        ):
            return Response({
                "message": "Order already rejected",
                "order_id": order.id,
                "status": status_obj.name
            })

        # Guardrail: if client sends Auditor status again while already in Auditor stage,
        # treat it as "Auditor approved -> move to Billing Approval".
        if previous_status and previous_status.id == status_obj.id and prev_name == "auditor approval":
            billing_status = (
                OrderStatus.objects.filter(id=3).first()
                or OrderStatus.objects.filter(name__iexact="Billing Approval").first()
                or OrderStatus.objects.filter(name__iexact="Billing").first()
                or OrderStatus.objects.filter(name__icontains="billing").order_by("id").first()
            )
            if billing_status:
                status_obj = billing_status

        if previous_status and not _is_rejection_status(status_obj) and prev_name != "rate approval":
            if "billing" in prev_name or "auditor" in prev_name:
                configured_next_status = _get_next_order_flow_status(order, previous_status, flow_type=order_flow_type)
                if configured_next_status:
                    status_obj = configured_next_status

        user = request.user if request.user.is_authenticated else None
        actor_role = (getattr(getattr(user, "role", None), "name", "") or "").strip().lower()

        # ✅ Update order
        order.status = status_obj

        # Only store reason when providedr
        if reason:
            order.reject_reason = reason

        mark_order_notifications_read(order, user)


        order.save()

        new_name = (status_obj.name or "").strip().lower()
        is_auditor_to_billing = (
            prev_name == "auditor approval"
            and (status_obj.id == 3 or "billing" in new_name)
        )

        # A billing user forwarding an order to the auditor must leave the auditor
        # stage pending (performed_by=None). Match on the actor's role too, so this
        # holds even when the previous status name doesn't literally contain
        # "billing" (otherwise it falls through to the generic handler, which would
        # stamp the billing user as the auditor-stage performer and make the
        # Auditor Approval stage look approved).
        is_billing_to_auditor = (
            ("billing" in prev_name or actor_role == "billing")
            and "auditor" in new_name
        )

        is_billing_completed = (
            "billing" in prev_name
            and (
                getattr(status_obj, "code", "") == "COMPLETED"
                or new_name == "completed"
            )
        )

        is_auditor_completed = (
            "auditor" in prev_name
            and (
                getattr(status_obj, "code", "") == "COMPLETED"
                or new_name == "completed"
            )
        )

        is_auditor_rejected = (
            "auditor" in prev_name
            and (
                getattr(status_obj, "code", "") == "REJECTED"
                or "reject" in new_name
            )
        )

        is_rate_approved = (
            prev_name == "rate approval"
            and status_obj.id == 6
        )

        is_rate_rejected = (
            prev_name == "rate approval"
            and (
                status_obj.id == APPROVER_REJECTED_ACTION_ID
                or getattr(status_obj, "code", "") == "REJECTED"
                or "reject" in new_name
            )
        )

        if is_rate_approved or is_rate_rejected:
            rate_approval = _get_order_rate_approval(order, user)
            if not rate_approval:
                order.status = previous_status
                order.save(update_fields=["status"])
                return Response(
                    {"message": "This order is not assigned to you for rate approval."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if rate_approval.status != "PENDING":
                order.status = previous_status
                order.save(update_fields=["status"])
                return Response(
                    {
                        "message": f"You have already {rate_approval.status.lower()} this order.",
                        "order_id": order.id,
                        "status": order.status.name if order.status else "",
                        "approval_status": rate_approval.status,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if is_rate_rejected:
                _mark_rate_approval_decision(order, user, "REJECTED", reason)
                _close_or_create_status_log(
                    order=order,
                    action_status=previous_status,
                    user=user,
                    remarks=reason or "Rejected by rate approver"
                )

                order.status = status_obj
                order.rejected_by = user
                order.rejected_at = timezone.now()
                if reason:
                    order.reject_reason = reason
                    order.rejection_reason = reason
                order.save()

                log_order_action(order=order, action_name=status_obj.name, user=user, remarks=reason)
                send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)

                return Response({
                    "message": "Order rejected successfully",
                    "order_id": order.id,
                    "status": status_obj.name,
                    "approval_status": "REJECTED",
                })

            _mark_rate_approval_decision(order, user, "APPROVED", reason)
            log_order_action(order=order, action_name=status_obj.name, user=user, remarks=reason)

            if _has_pending_rate_approvals(order):
                order.status = previous_status
                order.save(update_fields=["status"])
                return Response({
                    "message": "Rate approved. Waiting for remaining approvers.",
                    "order_id": order.id,
                    "status": previous_status.name if previous_status else status_obj.name,
                    "approval_status": "APPROVED",
                    "pending_approvers": [
                        _display_user_name(approver)
                        for approver in _assigned_rate_approvers_for_order(order, status_filter="PENDING")
                    ],
                })

            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason or "Approved by all rate approvers"
            )

            next_flow_status = _get_next_order_flow_status(order, previous_status, 'Billing', flow_type=order_flow_type)
            if next_flow_status:
                order.status = next_flow_status
                order.save()
                log_order_action(
                    order=order,
                    action_name=next_flow_status.name,
                    user=_pending_log_user_for_status(next_flow_status, user),
                    remarks="",
                )
                send_order_notifications(order, next_flow_status.name, actor=user, previous_status=previous_status)

            return Response({
                "message": _order_status_message(next_flow_status, "approved") if next_flow_status else "Rate approved",
                "order_id": order.id,
                "status": next_flow_status.name if next_flow_status else status_obj.name,
                "approval_status": "APPROVED",
            })

        if is_auditor_to_billing:

            auditor_pending_log = (
                OrdersLog.objects
                .filter(order=order, action=previous_status, performed_by__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if auditor_pending_log:
                auditor_pending_log.performed_by = user
                if reason:
                    auditor_pending_log.remarks = reason
                auditor_pending_log.save(update_fields=["performed_by", "remarks"])

            # Next stage entry: billing approval should be a new pending row.
            billing_pending_log = (
                OrdersLog.objects
                .filter(order=order, action=status_obj, performed_by__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if not billing_pending_log:
                log_order_action(
                    order=order,
                    action_name=status_obj.name,
                    user=None,
                    remarks=""
                )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_billing_to_auditor:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason or "Accepted by billing"
            )

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=None,
                remarks=""
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_billing_completed:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason or "Accepted by billing"
            )

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=reason
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_auditor_rejected:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason
            )

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=reason
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": "Order rejected successfully",
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_auditor_completed:
            auditor_completed_remarks = reason or "Sales quotation created by auditor"
            auditor_pending_log = (
                OrdersLog.objects
                .filter(order=order, action=previous_status, performed_by__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if auditor_pending_log:
                auditor_pending_log.performed_by = user
                auditor_pending_log.remarks = auditor_completed_remarks
                auditor_pending_log.save(update_fields=["performed_by", "remarks"])

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=auditor_completed_remarks
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        # If a placeholder row exists for this status (created earlier with no performer),
        # update it instead of inserting a duplicate row.
        
        pending_log = (
            OrdersLog.objects
            .filter(order=order, action=status_obj, performed_by__isnull=True)
            .order_by("-created_at")
            .first()
        )

        if pending_log:
            pending_log.performed_by = user
            if reason:
                pending_log.remarks = reason
            pending_log.save(update_fields=["performed_by", "remarks"])
        else:
            # ✅ LOG using status name from DB
            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=reason
            )

        send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


        return Response({
            "message": _order_status_message(status_obj),
            "order_id": order.id,
            "status": status_obj.name
        })
    
_status_cache = {}

def get_status(name):
    if name not in _status_cache:
        try:
            _status_cache[name] = OrderStatus.objects.get(name=name)
        except OrderStatus.DoesNotExist:
            return None
    return _status_cache[name]

def _close_or_create_status_log(order, action_status, user, remarks=''):
    if not action_status:
        return

    log = (
        OrdersLog.objects
        .filter(order=order, action=action_status, performed_by__isnull=True)
        .order_by("-created_at")
        .first()
    )

    if log:
        log.performed_by = user
        log.remarks = remarks
        log.save(update_fields=["performed_by", "remarks"])
        return

    OrdersLog.objects.create(
        order=order,
        action=action_status,
        performed_by=user,
        remarks=remarks
    )

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
