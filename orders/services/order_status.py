"""Advancing (or rejecting) an order — `UpdateOrderStatusView`'s branch logic.

Lifted out of `orders/views/lifecycle.py` (plan item 3.2) as a pure,
mechanical extraction: `apply_order_status_transition` below is the body of
`UpdateOrderStatusView.post`, moved statement-for-statement. Nothing about
the conditions, the branching order, the side effects or the response
bodies has changed — including the parts that are still fragile. In
particular, deliberately UNCHANGED here (real problems, but a separate piece
of work from a mechanical move):

* Most of these branches key off `OrderStatus.name` text (`"billing" in
  prev_name`, `"auditor" in prev_name`, ...) rather than the immutable
  `code` column, so renaming a status can silently reroute the order flow.
  See `orders.services.order_flow` for the same caveat on the functions this
  module calls into.
* Several branches compare `status_obj.id` against hardcoded integers (`3`,
  `6`, `APPROVER_REJECTED_ACTION_ID`) that no migration guarantees.

`apply_order_status_transition` is only ever called from inside
`UpdateOrderStatusView.post`, itself wrapped in `transaction.atomic()` with
the order row already held by `select_for_update()` by the time this
function runs — see that view's docstring for why both are required (the
multi-approver race they close) and why `send_order_notifications`, called
from several branches below, must stay deferred via `transaction.on_commit`
rather than fire eagerly: it ends in outbound HTTP per recipient, and this
function runs with a row lock held.

Unlike its sibling modules (`order_flow.py`, `rate_approval.py`), this one
returns DRF `Response` objects directly instead of plain data the view would
wrap. That is a deliberate one-off, not a new house style for this
directory: the point of this extraction was zero behaviour change end to
end, and re-shaping nine different response bodies into a response-agnostic
result for the view to re-assemble would have been a rewrite wearing an
extraction's clothes.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework import status

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
    APPROVER_REJECTED_ACTION_ID,
    _get_order_rate_approval,
    _has_pending_rate_approvals,
    _mark_rate_approval_decision,
)
# These two are reached from `orders.views`, not `orders.services`, exactly
# as they were reached from inside this method before the move. Moving THEM
# too is out of scope for a mechanical extraction of this one view.
from orders.views._shared import _assigned_rate_approvers_for_order
from orders.views.notifications import _display_user_name, send_order_notifications


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


def apply_order_status_transition(order, status_id, reason, user):
    """The body of `UpdateOrderStatusView.post`, moved verbatim.

    Called with `order` already locked (`select_for_update()`) inside the
    view's `transaction.atomic()` block. `status_id`, `reason` and `user` are
    exactly `serializer.validated_data["status"]`,
    `serializer.validated_data.get("reason", "")` and `request.user if
    request.user.is_authenticated else None` — computed in the view and
    passed in rather than recomputed here, since none of the three has a
    side effect and moving *where* they are read does not change *what* they
    read.
    """
    previous_status = order.status
    order_flow_type = _get_order_flow_type_for_order(order)


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

    actor_role = (getattr(getattr(user, "role", None), "name", "") or "").strip().lower()

    # ✅ Update order
    order.status = status_obj

    # `rejection_reason` is the canonical column. `reject_reason` is written
    # alongside it only until the duplicate is dropped — see the note on
    # Order.reject_reason. Both carry the same text, so nothing downstream can
    # tell which one it read.
    if reason:
        order.rejection_reason = reason
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
                order.rejection_reason = reason
                order.reject_reason = reason
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
        auditor_completed_remarks = reason or "Sales Order created by auditor"
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
