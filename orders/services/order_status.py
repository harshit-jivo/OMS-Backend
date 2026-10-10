"""Advancing (or rejecting) an order — `UpdateOrderStatusView`'s logic.

One endpoint serves three desks: the rate approvers, billing and the auditor.
Each desk acts on an order only while the order is at that desk's stage, and
each desk's page sends one fixed status to mean "approve":

    stage (status code)       desk               approve request   approve leads to
    RATE_APPROVAL             assigned approvers APPROVED          next configured stage
    BILLING / BILLING_PENDING billing            AUDITOR_APPROVAL  next configured stage
    AUDITOR_APPROVAL          auditor            COMPLETED         Completed (SO must exist)

Any desk may instead send a rejection status. Everything else is refused with
409 and changes nothing: an order at any other status (Rejected, Billing
Rejected, Completed, SO Cancelled, Draft, Mart's own statuses) is not waiting
on any desk here; a request from a user who is not that stage's desk, or for
a different stage's action, is a stale screen or a repeated click acting on an
order that has already moved on. Before this table, the endpoint accepted any
status from anyone and re-routed it by words in the status NAME ("billing" /
"auditor"), so a second Accept from billing completed orders with no sales
order, rate rejections were overridden, and Completed orders were rejected.

A rejected order re-enters the flow only by being edited and resubmitted —
see `orders.views.lifecycle`.

Stages are matched on `OrderStatus.code`, which is immutable, never on `name`.

`apply_order_status_transition` is only ever called from inside
`UpdateOrderStatusView.post`, itself wrapped in `transaction.atomic()` with
the order row already held by `select_for_update()` by the time this
function runs — see that view's docstring for why both are required (the
multi-approver race they close) and why `send_order_notifications`, called
below, must stay deferred via `transaction.on_commit` rather than fire
eagerly: it ends in outbound HTTP per recipient, and this function runs with
a row lock held.

Unlike its sibling modules (`order_flow.py`, `rate_approval.py`), this one
returns DRF `Response` objects directly instead of plain data the view would
wrap; the response bodies predate the extraction and clients read them.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework import status

from core.permissions import has_role, is_admin
from orders.models import OrdersLog, OrderStatus, log_order_action
from orders.notifications import mark_order_notifications_read
from orders.services.order_flow import (
    _get_next_order_flow_status,
    _get_order_flow_type_for_order,
    _is_completed_status,
    _is_rejection_status,
    _order_status_message,
)
from orders.services.rate_approval import (
    _get_order_rate_approval,
    _has_pending_rate_approvals,
    _mark_rate_approval_decision,
)
# These two are reached from `orders.views`, not `orders.services`, exactly
# as they were before the extraction.
from orders.views._shared import _assigned_rate_approvers_for_order
from orders.views.notifications import _display_user_name, send_order_notifications


#: The status code each stage's desk sends to mean "approve". An order whose
#: status is not a key here is not waiting on any desk.
APPROVE_REQUEST_BY_STAGE = {
    'RATE_APPROVAL': 'APPROVED',
    'BILLING': 'AUDITOR_APPROVAL',
    'BILLING_PENDING': 'AUDITOR_APPROVAL',
    'AUDITOR_APPROVAL': 'COMPLETED',
}

#: Who may act at each stage (admins always may). Rate Approval is absent on
#: purpose: its entitlement is per order — the assigned approvers — and is
#: checked in `_apply_rate_decision`.
DESK_ROLES_BY_STAGE = {
    'BILLING': ('billing',),
    'BILLING_PENDING': ('billing',),
    'AUDITOR_APPROVAL': ('auditor',),
}

BILLING_STAGES = ('BILLING', 'BILLING_PENDING')


def _code(status_obj):
    return (getattr(status_obj, 'code', '') or '').strip().upper()


def _conflict(order, message):
    return Response(
        {
            "message": message,
            "order_id": order.id,
            "status": order.status.name if order.status else "",
        },
        status=status.HTTP_409_CONFLICT,
    )


def _pending_log_user_for_status(status_obj, actor_user=None):
    return actor_user if _is_completed_status(status_obj) else None


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


def _log_entered_stage(order, stage_status, user, remarks=''):
    """Record the order entering `stage_status`: a pending row (no performer)
    for a stage someone still has to act on, a performed row for Completed.
    An existing pending row for the stage is reused, never duplicated."""
    log_user = _pending_log_user_for_status(stage_status, user)
    if log_user is None and OrdersLog.objects.filter(
        order=order, action=stage_status, performed_by__isnull=True,
    ).exists():
        return
    log_order_action(order=order, action_name=stage_status.name, user=log_user,
                     remarks=remarks if log_user else "")


def _set_rejected(order, rejected_status, reason, user):
    order.status = rejected_status
    order.rejected_by = user
    order.rejected_at = timezone.now()
    # `rejection_reason` is the canonical column. `reject_reason` is written
    # alongside it only until the duplicate is dropped — see the note on
    # Order.reject_reason.
    if reason:
        order.rejection_reason = reason
        order.reject_reason = reason
    order.save()


def apply_order_status_transition(order, status_id, reason, user):
    """Apply one desk's approve or reject to `order` (already locked).

    `status_id`, `reason` and `user` are `serializer.validated_data["status"]`,
    `serializer.validated_data.get("reason", "")` and the authenticated user,
    computed in the view.
    """
    previous_status = order.status
    status_obj = get_object_or_404(OrderStatus, id=status_id)
    is_reject = _is_rejection_status(status_obj)
    stage = _code(previous_status)
    stage_name = previous_status.name if previous_status else "no status"

    if previous_status and previous_status.id == status_obj.id and is_reject:
        return Response({
            "message": "Order already rejected",
            "order_id": order.id,
            "status": status_obj.name
        })

    if stage not in APPROVE_REQUEST_BY_STAGE:
        if _is_rejection_status(previous_status):
            return _conflict(
                order,
                f"Order is {stage_name}. It can move on only after it is "
                f"edited and resubmitted.",
            )
        return _conflict(
            order,
            f"Order is {stage_name} and is not waiting on any approval.",
        )

    desk_roles = DESK_ROLES_BY_STAGE.get(stage)
    if desk_roles and not (is_admin(user) or has_role(user, *desk_roles)):
        return _conflict(
            order,
            f"Order is now at {stage_name}; only that desk can act on it. "
            f"Refresh to see its current status.",
        )

    if not is_reject and _code(status_obj) != APPROVE_REQUEST_BY_STAGE[stage]:
        return _conflict(
            order,
            f"Order is now at {stage_name}, so this action no longer applies. "
            f"Refresh to see its current status.",
        )

    order_flow_type = _get_order_flow_type_for_order(order)

    if stage == 'RATE_APPROVAL':
        return _apply_rate_decision(
            order, previous_status, status_obj, is_reject, reason, user, order_flow_type)

    if is_reject:
        _close_or_create_status_log(order, previous_status, user, reason)
        _set_rejected(order, status_obj, reason, user)
        mark_order_notifications_read(order, user)
        log_order_action(order=order, action_name=status_obj.name, user=user, remarks=reason)
        send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)
        return Response({
            "message": "Order rejected successfully",
            "order_id": order.id,
            "status": status_obj.name
        })

    if stage in BILLING_STAGES:
        # The fallback matters for Billing Pending, which is not a configured
        # stage: without it the FOC ladder answers "Billing" — backwards.
        next_status = (
            _get_next_order_flow_status(
                order, previous_status, 'Auditor Approval', flow_type=order_flow_type)
            or status_obj
        )
        default_remarks = "Accepted by billing"
    else:
        next_status = status_obj
        default_remarks = "Sales quotation created by auditor"

    # The auditor's page creates the sales order in SAP and only then asks
    # for Completed; an order reaching Completed any other way has no SO.
    if _is_completed_status(next_status) and not order.sap_created:
        return _conflict(
            order,
            "The sales order has not been created in SAP yet, so the order "
            "cannot be completed.",
        )

    remarks = reason or default_remarks
    _close_or_create_status_log(order, previous_status, user, remarks)
    order.status = next_status
    order.save()
    mark_order_notifications_read(order, user)
    _log_entered_stage(order, next_status, user, remarks)
    send_order_notifications(order, next_status.name, actor=user, previous_status=previous_status)

    return Response({
        "message": _order_status_message(next_status, "accepted"),
        "order_id": order.id,
        "status": next_status.name
    })


def _apply_rate_decision(order, previous_status, status_obj, is_reject, reason, user, order_flow_type):
    """One assigned approver's decision. Any rejection rejects the order; it
    advances only once every assigned approver has approved."""
    rate_approval = _get_order_rate_approval(order, user)
    if not rate_approval:
        return Response(
            {"message": "This order is not assigned to you for rate approval."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if rate_approval.status != "PENDING":
        return Response(
            {
                "message": f"You have already {rate_approval.status.lower()} this order.",
                "order_id": order.id,
                "status": order.status.name if order.status else "",
                "approval_status": rate_approval.status,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    mark_order_notifications_read(order, user)

    if is_reject:
        _mark_rate_approval_decision(order, user, "REJECTED", reason)
        _close_or_create_status_log(
            order=order,
            action_status=previous_status,
            user=user,
            remarks=reason or "Rejected by rate approver"
        )
        _set_rejected(order, status_obj, reason, user)
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
        return Response({
            "message": "Rate approved. Waiting for remaining approvers.",
            "order_id": order.id,
            "status": previous_status.name,
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

    next_flow_status = (
        _get_next_order_flow_status(order, previous_status, 'Billing', flow_type=order_flow_type)
        or status_obj
    )
    order.status = next_flow_status
    order.save()
    _log_entered_stage(order, next_flow_status, user, reason)
    send_order_notifications(order, next_flow_status.name, actor=user, previous_status=previous_status)

    return Response({
        "message": _order_status_message(next_flow_status, "approved"),
        "order_id": order.id,
        "status": next_flow_status.name,
        "approval_status": "APPROVED",
    })
