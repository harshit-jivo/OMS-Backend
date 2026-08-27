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
"""
from urllib import request
from orders.models import Order, log_order_action
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime
from django.utils import timezone
from django.shortcuts import get_object_or_404
from sap_sync.models import SalesOrderLog
from django.http import Http404
from django.db import transaction
import requests
from ._shared import (
    MART_STATUS_PENDING_ID,
    _mart_status,
    logger,
)


MART_APPROVER_ROLES = {'mart_approval', 'admin'}
MART_STATUS_APPROVED_ID = 6
MART_STATUS_REJECTED_ID = 7
MART_STATUS_COMPLETED_ID = 9   # set after a successful SAP/HANA push (Phase 2)
# Tab key (from the Mart Approval queue) -> the status id it maps to.
MART_TAB_STATUS_IDS = {
    'pending': MART_STATUS_PENDING_ID,
    'approved': MART_STATUS_APPROVED_ID,
    'rejected': MART_STATUS_REJECTED_ID,
}


# ── Mart Approval flow (distributor / company 3 orders) ──────────────────────
def _is_mart_approver(user):
    """Only the Mart Approval role (or admin) may work the Mart queue."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_staff', False):
        return True
    role_name = getattr(getattr(user, 'role', None), 'name', '')
    return str(role_name).strip().lower() in MART_APPROVER_ROLES


def _serialize_mart_order(order, *, with_items=False):
    """Compact serialization for the Mart queue list / detail."""
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
        # Filter by the queue tab (pending/approved/rejected) -> status id.
        tab = (request.query_params.get('tab') or '').strip().lower()
        if tab == 'approved':
            # The Approved tab shows both the approved-but-not-yet-in-SAP orders
            # (SAP push failed, still 'Mart Approved') AND the ones that pushed
            # successfully (now 'Completed'), so a successful order stays visible.
            orders = orders.filter(
                status_id__in=[MART_STATUS_APPROVED_ID, MART_STATUS_COMPLETED_ID]
            )
        elif tab in MART_TAB_STATUS_IDS:
            orders = orders.filter(status_id=MART_TAB_STATUS_IDS[tab])

        orders = (orders
                  .select_related('status', 'created_by')
                  .prefetch_related('items')
                  .order_by('-created_at')
                  .distinct())
        return Response([_serialize_mart_order(o) for o in orders])


class MartOrderDetailView(APIView):
    """One distributor order with its items, for the approver's edit screen."""
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
        order = get_object_or_404(Order, id=order_id, order_type='DISTRIBUTOR')
        return Response(_serialize_mart_order(order, with_items=True))


def _sap_outcome_is_ambiguous(exc):
    """True when SAP may have created the document despite raising.

    The distinction `payments/sap_poster.py` calls a financial invariant: a SAP
    *rejection* means nothing was committed and a retry is safe; a SAP
    *timeout* means the document may exist, and retrying duplicates it.
    Collapsing the two produces duplicate documents.

    `SyncService.create_sales_order` signals them differently, though only by
    accident of what it raises:

      * a non-201 response  -> `Exception(response.text)` — SAP answered and
        refused. Definite.
      * `DuplicateCustomerReference` — the pre-check refused before posting.
        Definite, and the safest possible case.
      * a `requests` transport error with no response attached — the request
        never completed. AMBIGUOUS.
    """
    if isinstance(exc, requests.exceptions.RequestException):
        return getattr(exc, 'response', None) is None
    return False


def _mart_release_sap_claim(order, exc, *, retry):
    """Decide what happens to the in-flight `sap_created` claim after a failure.

    On a definite rejection the claim is released so the order can be retried
    through `MartResendSapView` — nothing was created, so a retry is correct.

    On an ambiguous failure the claim is DELIBERATELY LEFT SET, which blocks
    the retry. SAP may hold a sales order for this document, and a retry would
    book a second one against the same customer. Someone must look in SAP and
    clear it by hand; a blocked order is recoverable, a duplicate financial
    document is not.
    """
    ambiguous = _sap_outcome_is_ambiguous(exc)
    what = 'retry' if retry else 'creation'

    if not ambiguous:
        with transaction.atomic():
            fresh = Order.objects.select_for_update().get(pk=order.pk)
            fresh.sap_created = False
            fresh.save(update_fields=['sap_created'])
        return Response(
            {
                'message': f'Order {order.order_number} approved, but SAP sales order '
                           f'{what} failed. SAP rejected it, so nothing was created — '
                           f'you can retry the push without re-approving.',
                'order_number': order.order_number,
                'status': order.status.name if order.status else '',
                'sap_error': str(exc),
                'sap_state': 'REJECTED',
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    logger.error(
        'SAP outcome UNKNOWN for order %s — claim left set, retry blocked',
        order.order_number,
    )
    return Response(
        {
            'message': f'SAP did not respond for order {order.order_number}, so it is '
                       f'not known whether the sales order was created. DO NOT '
                       f'resubmit — check SAP first, or this books a second order '
                       f'against the same customer.',
            'order_number': order.order_number,
            'status': order.status.name if order.status else '',
            'sap_error': str(exc),
            'sap_state': 'UNKNOWN',
        },
        status=status.HTTP_502_BAD_GATEWAY,
    )


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

        approved = _mart_status(MART_STATUS_APPROVED_ID)
        if not approved:
            return Response({'error': f"Approved status (id {MART_STATUS_APPROVED_ID}) is not configured in the DB."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # --- claim the order, under a row lock ------------------------------
        # The second concurrent approver blocks on select_for_update, then
        # re-reads and sees the claim, so only one request reaches SAP.
        #
        # The claim must be SET here, not after the push. Checking `sap_created`
        # under the lock and releasing it before the SAP call would leave the
        # original race untouched: both requests would read False, because the
        # flag is only written once a post has already succeeded.
        with transaction.atomic():
            try:
                order = (Order.objects
                         .select_for_update()
                         .get(id=order_id, order_type='DISTRIBUTOR'))
            except Order.DoesNotExist:
                raise Http404('Order not found')

            if order.sap_created:
                return Response(
                    {
                        'error': f'Order {order.order_number} is already booked into SAP, '
                                 f'or a push is in flight.',
                        'order_number': order.order_number,
                        'status': order.status.name if order.status else '',
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            order.status = approved
            order.approved_by = request.user if request.user.is_authenticated else None
            order.approved_at = timezone.now()
            order.sap_created = True
            order.save()
            log_order_action(order, approved.name, user=request.user, remarks='Mart order approved')

        # Push the approved distributor order into SAP as a Sales Order. The
        # company DB is resolved from order.company inside the service — a
        # company-3 (Mart) order books into the Mart company DB.
        from sap_sync.services.sync_service import SyncService

        try:
            service = SyncService(triggered_by=getattr(request.user, 'username', None))
            sap_result = service.create_sales_order(order)
        except Exception as exc:
            logger.exception("SAP sales order creation failed for order %s", order.order_number)
            return _mart_release_sap_claim(order, exc, retry=False)

        # SAP order created → move the order to Completed.
        completed = _mart_status(MART_STATUS_COMPLETED_ID)
        if completed:
            order.status = completed
            order.save(update_fields=['status'])
            log_order_action(order, completed.name, user=request.user, remarks='SAP sales order created')

        return Response({
            'message': f'Order {order.order_number} approved and SAP sales order created',
            'order_number': order.order_number,
            'status': order.status.name,
            'sap': {
                'doc_entry': sap_result.get('DocEntry') if isinstance(sap_result, dict) else None,
                'doc_num': sap_result.get('DocNum') if isinstance(sap_result, dict) else None,
            },
        })


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

        # Same claim-under-lock as MartApproveView, for the same reason: this
        # endpoint also ends in `create_sales_order`, so pressing Retry twice
        # was a second route to two SAP sales orders.
        with transaction.atomic():
            try:
                order = (Order.objects
                         .select_for_update()
                         .get(id=order_id, order_type='DISTRIBUTOR'))
            except Order.DoesNotExist:
                raise Http404('Order not found')

            # The guard was `status_id == COMPLETED`, which missed a real state.
            # `create_sales_order` sets `sap_created` BEFORE the caller moves the
            # order to Completed, so an order can sit at
            # `sap_created=True, status='Mart Approved'` — the push succeeded and
            # the status save did not. That order looks retryable by status and
            # is not, and retrying it books a duplicate.
            #
            # `sap_created` is the authority now; the status check stays only to
            # give the clearer message for the ordinary completed case.
            if order.status_id == MART_STATUS_COMPLETED_ID:
                return Response(
                    {'error': f'Order {order.order_number} is already completed in SAP.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if order.sap_created:
                return Response(
                    {'error': f'Order {order.order_number} is already booked into SAP, '
                              f'or its SAP outcome is unknown. Check SAP before '
                              f'retrying — a retry would create a second order.'},
                    status=status.HTTP_409_CONFLICT,
                )

            order.sap_created = True
            order.save(update_fields=['sap_created'])

        from sap_sync.services.sync_service import SyncService

        try:
            service = SyncService(triggered_by=getattr(request.user, 'username', None))
            sap_result = service.create_sales_order(order)
        except Exception as exc:
            logger.exception("SAP sales order retry failed for order %s", order.order_number)
            return _mart_release_sap_claim(order, exc, retry=True)

        # SAP order created → move the order to Completed (mirrors MartApproveView).
        completed = _mart_status(MART_STATUS_COMPLETED_ID)
        if completed:
            order.status = completed
            order.save(update_fields=['status'])
            log_order_action(order, completed.name, user=request.user,
                             remarks='SAP sales order created (retry)')

        return Response({
            'message': f'Order {order.order_number} sent to SAP successfully',
            'order_number': order.order_number,
            'status': order.status.name,
            'sap': {
                'doc_entry': sap_result.get('DocEntry') if isinstance(sap_result, dict) else None,
                'doc_num': sap_result.get('DocNum') if isinstance(sap_result, dict) else None,
            },
        })
