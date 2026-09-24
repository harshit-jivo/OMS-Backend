"""Mart (distributor) order views.

Fourth domain out of `orders/views.py` (plan item 3.1). This is the SAP-writing
path: approving a distributor order posts a Sales Order to SAP, so the claim
protocol in `MartApproveView` — take the claim inside the locked block, call
SAP outside it, release only on an unambiguous rejection — is load-bearing.
`_sap_outcome_is_ambiguous` is the distinction it turns on: a rejection
committed nothing and is safe to retry, a timeout may have committed and is
not. Moving it did not change a line of that logic.

Three names stayed in `._shared` rather than travelling here:
`MART_STATUS_PENDING_ID`, `_mart_status` and `_mart_approver_user`. Order
creation and order detail read them too, so bringing them across would have
made `_legacy` import back out of this module.

The SAP-posting logic itself — the claim/lock/push sequence in
`MartApproveView.post` and `MartResendSapView.post`, plus
`_sap_outcome_is_ambiguous` and `_mart_release_sap_claim` — now lives in
`orders.services.mart_posting` (plan item 3.2), moved there as a pure,
mechanical extraction. These two view methods keep only the permission check
and the hand-off; see that module's docstring for the full account.
"""
from orders.models import Order, OrderItem, log_order_action
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404
from core.permissions import HasKey
from sap_sync.models import SalesOrderLog
from ._shared import (
    MART_STATUS_PENDING_ID,
    _mart_status,
)
from orders.services.mart_posting import (
    MART_STATUS_APPROVED_ID,
    MART_STATUS_COMPLETED_ID,
    approve_and_post_to_sap,
    cancel_mart_order,
    resend_sales_order_to_sap,
)


MART_APPROVER_ROLES = {'mart_approval', 'admin'}
MART_STATUS_REJECTED_ID = 7
# Tab key (from the Mart Approval queue) -> the status id it maps to.
MART_TAB_STATUS_IDS = {
    'pending': MART_STATUS_PENDING_ID,
    'approved': MART_STATUS_APPROVED_ID,
    'rejected': MART_STATUS_REJECTED_ID,
}


# ── Mart Approval flow (distributor / company 3 orders) ──────────────────────
def _is_mart_approver(user):
    """Only the Mart Approval desk (or admin) may work the Mart queue.

    Resolved through `core.permissions` (Phase 3): `is_admin` is the project's
    one definition of admin, and the desk itself is the `orders.mart.decide`
    registry key — granted to the mart_approval role by migration 0032 — with
    `has_role` as the transitional fallback for databases where the seed has
    not been applied yet. `has_role` also counts `extra_roles`, which the old
    primary-FK-only comparison here missed; that widening is the model layer's
    stated rule, not a policy change. Once 0032 is verified live, the
    `has_role` line goes — same cleanup contract as `HasKeyOrRole`.
    """
    from core.permissions import effective_keys, has_role, is_admin

    if is_admin(user):
        return True
    if 'orders.mart.decide' in effective_keys(user):
        return True
    return has_role(user, *MART_APPROVER_ROLES)


def _serialize_mart_order(order, *, with_items=False, cancel_info=None):
    """Compact serialization for the Mart queue list / detail.

    `cancel_info` (used only on the SO Cancel page's Cancelled tab) carries the
    reason and time pulled from `sap_sync.SalesCancelledLog`, since those are no
    longer stored on the Order itself.
    """
    cancel_info = cancel_info or {}
    data = {
        'id': order.id,
        'order_number': order.order_number,
        'order_type': order.order_type,
        'card_code': order.card_code,
        'card_name': order.card_name,
        'company': order.company,
        'total_amount': str(order.total_amount),
        'status': order.status.code if order.status else '',
        'status_id': order.status_id,
        'status_display': order.status.name if order.status else '',
        'is_pending': order.status_id == MART_STATUS_PENDING_ID,
        'bill_to_id': order.bill_to_id,
        'bill_to_address': order.bill_to_address,
        'ship_to_id': order.ship_to_id,
        'ship_to_address': order.ship_to_address,
        'po_number': order.po_number,
        'delivery_date': order.delivery_date,
        'created_by': order.created_by.name if order.created_by else None,
        'created_at': order.created_at,
        'rejection_reason': order.rejection_reason or '',
        'rejected_at': order.rejected_at,
        'cancellation_reason': cancel_info.get('reason') or '',
        'cancelled_at': cancel_info.get('cancelled_at'),
        'items_count': order.items.count(),
    }
    if with_items:
        data['items'] = [{
            'id': it.id,
            'item_code': it.item_code,
            'item_name': it.item_name,
            'category': it.category,
            'qty': str(it.qty),
            'basic_price': str(it.basic_price),
            'total': str(it.total),
        } for it in order.items.all()]
    return data


class MartOrderListView(APIView):
    """All distributor (company 3 / Mart) orders, for the Mart Approval queue.
    Optional ?status=<code> filter (e.g. MART_APPROVAL)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized for the Mart queue'},
                            status=status.HTTP_403_FORBIDDEN)

        orders = Order.objects.filter(order_type='DISTRIBUTOR')
        # Filter by the queue tab (pending/approved/rejected/completed/cancelled)
        # -> status id.
        tab = (request.query_params.get('tab') or '').strip().lower()
        if tab == 'approved':
            # The Approved tab shows both the approved-but-not-yet-in-SAP orders
            # (SAP push failed, still 'Mart Approved') AND the ones that pushed
            # successfully (now 'Completed'), so a successful order stays visible.
            orders = orders.filter(
                status_id__in=[MART_STATUS_APPROVED_ID, MART_STATUS_COMPLETED_ID]
            )
        elif tab == 'completed':
            # The Mart Cancel page's actionable list: orders that reached SAP.
            orders = orders.filter(status_id=MART_STATUS_COMPLETED_ID)
        elif tab == 'cancelled':
            # Cancel history — resolved by code (the SO_CANCELLED row is managed
            # directly in order_statuses, so its id is not hardcoded here).
            from orders.models import OrderStatus
            cancelled_id = (OrderStatus.objects
                            .filter(code='SO_CANCELLED')
                            .values_list('id', flat=True)
                            .first())
            orders = orders.filter(status_id=cancelled_id) if cancelled_id else orders.none()
        elif tab in MART_TAB_STATUS_IDS:
            orders = orders.filter(status_id=MART_TAB_STATUS_IDS[tab])

        orders = (orders
                  .select_related('status', 'created_by')
                  .prefetch_related('items')
                  .order_by('-created_at')
                  .distinct())

        # On the Cancelled tab, pull each order's reason + time from its latest
        # successful SalesCancelledLog (sales_cancellation_logs) — the cancel
        # facts are no longer stored on the Order. Batched to one query.
        cancel_map = {}
        if tab == 'cancelled':
            from sap_sync.models import SalesCancelledLog
            order_ids = [str(o.id) for o in orders]
            for log in (SalesCancelledLog.objects
                        .filter(order_id__in=order_ids, status='SUCCESS')
                        .order_by('order_id', '-created_at')):
                if log.order_id not in cancel_map:
                    cancel_map[log.order_id] = {
                        'reason': log.cancellation_reason,
                        'cancelled_at': log.completed_at or log.created_at,
                    }

        return Response([
            _serialize_mart_order(o, cancel_info=cancel_map.get(str(o.id)))
            for o in orders
        ])


class MartOrderDetailView(APIView):
    """One distributor order with its items, for the approver's edit screen."""
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
        order = get_object_or_404(Order, id=order_id, order_type='DISTRIBUTOR')
        return Response(_serialize_mart_order(order, with_items=True))


class MartApproveView(APIView):
    """Approve a distributor order → 'Mart Approved', then book it into SAP.

    This is the only order-flow endpoint that creates a financial document, so
    it is the one place in `orders` where a double submission costs real money:
    two SAP Sales Orders for one OMS order, against the same customer.

    Nothing stopped that. There was no lock, no transaction, and — the cheapest
    miss — no check of `order.sap_created`, a flag `create_sales_order` already
    SETS on success and that nothing ever READ. A double-click, or two
    approvers working the queue at once, put both requests past
    `_is_mart_approver` and both into `create_sales_order`.

    The only thing standing in the way was `_assert_num_at_card_available`,
    which pre-checks HANA for a duplicate customer reference. It is well
    written but cannot close this, for three reasons:

      * it is check-then-act, not a lock — both requests query HANA before
        either has posted, and the window is a SAP login plus an HTTP
        round-trip;
      * it fails OPEN by design, so the guard disappears exactly when HANA is
        unhealthy;
      * it needs `NumAtCard`, and returns early without it — so an order with
        no customer PO reference had no protection at all, from it or from
        SAP's own -5002 duplicate rejection, which keys off the same field.

    The fix is the discipline `payments/sap_poster.py` already applies to
    money: reload the row under `select_for_update()` immediately before the
    SAP call and refuse if it has already been posted.

    Deliberately NOT inside the transaction: the SAP call itself. Holding a row
    lock across a Service Layer round-trip would make every other approver on
    that order wait for SAP, and SAP is exactly the thing that hangs. Instead
    the lock sets an in-flight claim (`sap_created`) and commits, which is how
    `payments.sap_poster.post_document` uses its `POSTING_TO_SAP` status: the
    marker, not the lock, is what stops the second request.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)

        return approve_and_post_to_sap(order_id, request.user)


class MartRejectView(APIView):
    """Reject a distributor order with a mandatory reason → 'Mart Rejected'."""
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
        order = get_object_or_404(Order, id=order_id, order_type='DISTRIBUTOR')

        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'error': 'Rejection reason is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        rejected = _mart_status(MART_STATUS_REJECTED_ID)
        if not rejected:
            return Response({'error': f"Rejected status (id {MART_STATUS_REJECTED_ID}) is not configured in the DB."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        order.status = rejected
        order.rejected_by = request.user if request.user.is_authenticated else None
        order.rejected_at = datetime.now()
        order.rejection_reason = reason
        order.save()
        log_order_action(order, rejected.name, user=request.user, remarks=f'Rejected: {reason}')

        return Response({
            'message': f'Order {order.order_number} rejected',
            'order_number': order.order_number,
            'status': order.status.name,
        })


def _sales_order_sap_entries(order_ids):
    """Map str(order_id) -> the latest SalesOrderLog for that distributor order.

    A distributor order posts to SAP as a Sales Order (SalesOrderLog, keyed by
    str(order.id)). We surface the newest attempt per order so the tracking page
    can show DocEntry/DocNum on success or the SAP error on failure.
    """
    str_ids = [str(oid) for oid in order_ids]
    mapping = {}
    if not str_ids:
        return mapping
    logs = (
        SalesOrderLog.objects
        .filter(order_id__in=str_ids)
        .order_by('order_id', '-created_at')
        .values('order_id', 'status', 'sap_doc_entry', 'sap_doc_num',
                'error_message', 'completed_at')
    )
    for log in logs:
        # first row per order is the latest (queryset ordered by -created_at)
        oid = log['order_id']
        if oid not in mapping:
            mapping[oid] = {
                'status': log['status'],
                'doc_entry': log['sap_doc_entry'],
                'doc_num': log['sap_doc_num'],
                'error_message': log['error_message'],
                'completed_at': log['completed_at'],
            }
    return mapping


class SalesOrderSapStatusView(APIView):
    """Batch lookup of SAP Sales Order status for distributor orders.

    The Distributor Order Tracking page calls this with the ids of the orders it
    is showing so it can display DocEntry/DocNum (success) or the SAP error
    (failure). Returns a map keyed by order id (as string)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        raw_ids = request.query_params.get('order_ids', '')
        order_ids = [oid for oid in (i.strip() for i in raw_ids.split(',')) if oid.isdigit()]
        return Response({'statuses': _sales_order_sap_entries(order_ids)})


class MartResendSapView(APIView):
    """Retry pushing an already-approved distributor order to SAP as a Sales
    Order, without re-editing or re-approving it.

    Used when the initial SAP push (at Mart approval) failed: the order is left
    in 'Mart Approved' and never reached 'Completed'. Mart approver / admin only.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)

        return resend_sales_order_to_sap(order_id, request.user)


def _can_cancel_mart(user):
    """Who may cancel a completed Mart order (reversing it in SAP).

    Gated by the dedicated `orders.mart.cancel` key — a heavier authority than
    the `orders.mart.decide` desk, since cancelling reverses a document already
    booked into SAP. `has_role('mart_approval')` is kept as the transitional
    fallback for databases where users/0037 (which grants the key to the role)
    has not run yet, exactly as `_is_mart_approver` falls back for 0032.
    """
    from core.permissions import effective_keys, has_role, is_admin

    if is_admin(user):
        return True
    if 'orders.mart.cancel' in effective_keys(user):
        return True
    return has_role(user, *MART_APPROVER_ROLES)


class MartCancelView(APIView):
    """Cancel a completed distributor order, reversing its SAP Sales Order.

    Distinct from Reject (which sends a still-pending order back): this cancels
    an order that already reached 'Completed' — approved AND booked into SAP.
    The reversal happens in SAP first; see
    `orders.services.mart_posting.cancel_mart_order` for the full sequence.
    Requires the `orders.mart.cancel` authority and a mandatory reason.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        if not _can_cancel_mart(request.user):
            return Response({'error': 'Not authorized to cancel Mart orders'},
                            status=status.HTTP_403_FORBIDDEN)

        return cancel_mart_order(order_id, request.user, request.data.get('reason'))


# ── Distributor Report ───────────────────────────────────────────────────────
def _report_date(value):
    """Parse a YYYY-MM-DD query param into a date, or None if absent/malformed.

    Malformed is treated as "no filter" rather than an error: a report should
    render the full set instead of 400-ing on a stray date string.
    """
    try:
        return datetime.strptime((value or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _f(value):
    """HANA/ORM Sum() returns Decimal (or None on an empty group); the report
    ships plain numbers, so coerce once here."""
    return float(value or 0)


class DistributorReportView(APIView):
    """Completed distributor (Mart) orders, aggregated two ways for the
    Distributor Report — distributor-wise and SKU/item-wise.

    Reads OMS's OWN order tables (not SAP), and counts ONLY orders that reached
    'Completed' (status id 9) — approved AND successfully posted to SAP — so the
    figures reflect what was actually booked, not half-finished drafts. Both
    aggregations are returned in one payload; the page shows them as two tabs.

    Access is the per-user `Distributor_Report` grant (like the Inventory
    Report), so it can be handed to one person without touching anyone else.

    NOTE ON "SKU": OMS carries no dedicated SKU field on order lines — the SKU
    UDF lives only in SAP, which the user asked us not to read here. So the
    SKU-wise view keys on `item_code` (with `item_name`), which is the closest
    per-product identity OMS holds.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKey('Distributor_Report')]

    def get(self, request):
        from_date = _report_date(request.query_params.get('from_date'))
        to_date = _report_date(request.query_params.get('to_date'))

        # Orders whose SAP Sales Order was cancelled must never count. Normally
        # cancelling flips the order to 'SO_CANCELLED' (so the status filter
        # below already drops it), but if the SAP cancel succeeded and the OMS
        # status-save then failed/raced (see cancel_mart_order), the order can
        # linger at 'Completed' while carrying a SUCCESS cancellation log. Read
        # those ids and exclude them explicitly, so a cancelled SO is out of the
        # report by either path.
        from sap_sync.models import SalesCancelledLog
        cancelled_order_ids = [
            int(oid)
            for oid in SalesCancelledLog.objects
            .filter(status='SUCCESS')
            .values_list('order_id', flat=True)
            .distinct()
            if str(oid).isdigit()
        ]

        # One base queryset of the line items on completed distributor orders;
        # both aggregations and the grand totals derive from it, so they can
        # never disagree about which orders are in scope.
        items = OrderItem.objects.filter(
            order__order_type='DISTRIBUTOR',
            order__status_id=MART_STATUS_COMPLETED_ID,
        ).exclude(order_id__in=cancelled_order_ids)
        if from_date:
            items = items.filter(order__created_at__date__gte=from_date)
        if to_date:
            items = items.filter(order__created_at__date__lte=to_date)

        distributors = [
            {
                'card_code': row['order__card_code'],
                'card_name': row['order__card_name'],
                'order_count': row['order_count'],
                'sku_count': row['sku_count'],
                'qty': _f(row['qty']),
                'boxes': _f(row['boxes']),
                'pcs': _f(row['pcs']),
                'value': _f(row['value']),
            }
            for row in items
            .values('order__card_code', 'order__card_name')
            .annotate(
                order_count=Count('order', distinct=True),
                sku_count=Count('item_code', distinct=True),
                qty=Sum('qty'),
                boxes=Sum('boxes'),
                pcs=Sum('pcs'),
                value=Sum('total'),
            )
            .order_by('order__card_name')
        ]

        skus = [
            {
                'item_code': row['item_code'],
                'item_name': row['item_name'] or '',
                'order_count': row['order_count'],
                'distributor_count': row['distributor_count'],
                'qty': _f(row['qty']),
                'boxes': _f(row['boxes']),
                'pcs': _f(row['pcs']),
                'value': _f(row['value']),
            }
            for row in items
            .values('item_code', 'item_name')
            .annotate(
                order_count=Count('order', distinct=True),
                distributor_count=Count('order__card_code', distinct=True),
                qty=Sum('qty'),
                boxes=Sum('boxes'),
                pcs=Sum('pcs'),
                value=Sum('total'),
            )
            .order_by('item_code')
        ]

        totals = items.aggregate(
            order_count=Count('order', distinct=True),
            qty=Sum('qty'),
            boxes=Sum('boxes'),
            pcs=Sum('pcs'),
            value=Sum('total'),
        )

        return Response({
            'from_date': from_date.isoformat() if from_date else None,
            'to_date': to_date.isoformat() if to_date else None,
            'distributors': distributors,
            'skus': skus,
            'totals': {
                'distributor_count': len(distributors),
                'sku_count': len(skus),
                'order_count': totals['order_count'] or 0,
                'qty': _f(totals['qty']),
                'boxes': _f(totals['boxes']),
                'pcs': _f(totals['pcs']),
                'value': _f(totals['value']),
            },
        })
