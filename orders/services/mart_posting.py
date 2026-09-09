"""Mart / distributor SAP-posting: claiming an order, calling SAP, deciding
what survives a failure.

Lifted out of `orders/views/mart.py` (plan item 3.2) as a pure, mechanical
extraction: `approve_and_post_to_sap` and `resend_sales_order_to_sap` below
are the bodies of `MartApproveView.post` and `MartResendSapView.post`, moved
statement-for-statement — everything after the `_is_mart_approver`
permission check, which stays in the view since it also gates
`MartOrderListView`/`MartOrderDetailView`/`MartRejectView`, none of which
touch SAP. Nothing about the conditions, the branching order, the locking or
the side effects has changed.

`_sap_outcome_is_ambiguous` and `_mart_release_sap_claim` moved with them:
the financial invariant they encode — a SAP rejection is safe to retry, a
SAP timeout is not, because the document may already exist — is the business
rule this extraction is for, not an implementation detail of the view. See
`MartApproveView`'s own docstring (still in `orders/views/mart.py`) for the
fuller account of the race this closes and why the row lock is taken before
the SAP call and released only after an unambiguous rejection.

`MART_STATUS_APPROVED_ID` and `MART_STATUS_COMPLETED_ID` moved here too,
since both posting functions key off them; `orders/views/mart.py` imports
them back for its own use (the queue's tab filter, `MartApproveView`'s status
lookup) exactly as `orders/views/lifecycle.py` already imports constants back
from `orders.services.rate_approval` and `orders.services.order_flow`.
`MART_STATUS_REJECTED_ID` stayed in the view: only `MartRejectView` and the
tab map use it, and neither posts to SAP.

`from sap_sync.services.sync_service import SyncService` stays a local
import inside each function, exactly where it sat inside the view methods —
moving it to module level is an import-timing change, out of scope for a
mechanical move.
"""
import requests
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from orders.models import Order, log_order_action
from orders.views._shared import _mart_status, logger


MART_STATUS_APPROVED_ID = 6
MART_STATUS_COMPLETED_ID = 9   # set after a successful SAP/HANA push (Phase 2)


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


def approve_and_post_to_sap(order_id, user):
    """The body of `MartApproveView.post`, moved verbatim (after the
    `_is_mart_approver` check, which stays in the view).

    `user` is exactly `request.user` — passed through rather than
    pre-filtered, since the view read `request.user.is_authenticated` and
    `request.user.username` at different points and this preserves both call
    sites unchanged.
    """
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
        order.approved_by = user if user.is_authenticated else None
        order.approved_at = timezone.now()
        order.sap_created = True
        order.save()
        log_order_action(order, approved.name, user=user, remarks='Mart order approved')

    # Push the approved distributor order into SAP as a Sales Order. The
    # company DB is resolved from order.company inside the service — a
    # company-3 (Mart) order books into the Mart company DB.
    from sap_sync.services.sync_service import SyncService

    try:
        service = SyncService(triggered_by=getattr(user, 'username', None))
        sap_result = service.create_sales_order(order)
    except Exception as exc:
        logger.exception("SAP sales order creation failed for order %s", order.order_number)
        return _mart_release_sap_claim(order, exc, retry=False)

    # SAP order created → move the order to Completed.
    completed = _mart_status(MART_STATUS_COMPLETED_ID)
    if completed:
        order.status = completed
        order.save(update_fields=['status'])
        log_order_action(order, completed.name, user=user, remarks='SAP sales order created')

    return Response({
        'message': f'Order {order.order_number} approved and SAP sales order created',
        'order_number': order.order_number,
        'status': order.status.name,
        'sap': {
            'doc_entry': sap_result.get('DocEntry') if isinstance(sap_result, dict) else None,
            'doc_num': sap_result.get('DocNum') if isinstance(sap_result, dict) else None,
        },
    })


def resend_sales_order_to_sap(order_id, user):
    """The body of `MartResendSapView.post`, moved verbatim (after the
    `_is_mart_approver` check, which stays in the view). See
    `approve_and_post_to_sap` for why `user` is passed through unfiltered.
    """
    # Same claim-under-lock as approve_and_post_to_sap, for the same reason:
    # this path also ends in `create_sales_order`, so pressing Retry twice
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
        service = SyncService(triggered_by=getattr(user, 'username', None))
        sap_result = service.create_sales_order(order)
    except Exception as exc:
        logger.exception("SAP sales order retry failed for order %s", order.order_number)
        return _mart_release_sap_claim(order, exc, retry=True)

    # SAP order created → move the order to Completed (mirrors approve_and_post_to_sap).
    completed = _mart_status(MART_STATUS_COMPLETED_ID)
    if completed:
        order.status = completed
        order.save(update_fields=['status'])
        log_order_action(order, completed.name, user=user,
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
