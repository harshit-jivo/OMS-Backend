"""Duplicate-prevention for the one order endpoint that creates money.

`MartApproveView` is the only view in `orders` that books a financial document:
it approves a distributor order and posts a Sales Order into SAP. A second
submission there is not a duplicate log row, it is a second sales order against
the same customer.

Nothing prevented that. There was no transaction, no lock, and no check of
`order.sap_created` — a flag `create_sales_order` already SET on success and
that nothing ever READ.

The only obstacle was `_assert_num_at_card_available`, a HANA pre-check for a
duplicate customer reference. It cannot close this:

  * it is check-then-act — both requests query HANA before either posts;
  * it fails OPEN, so it disappears when HANA is unhealthy;
  * it returns early when `NumAtCard` is blank, and SAP's own -5002 duplicate
    rejection keys off the same field — so an order with no customer PO had no
    protection from either.

Every test here stubs `SyncService.create_sales_order`. Nothing contacts SAP.

Run with::

    python manage.py test orders.tests_mart_sap --settings=OMS.test_settings
"""
from unittest.mock import patch

import requests
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from orders.models import Order, OrderStatus
from users.models import User, UserRole

from .views import (
    MART_STATUS_APPROVED_ID,
    MART_STATUS_COMPLETED_ID,
    MART_STATUS_PENDING_ID,
)

SERVICE = 'sap_sync.services.sync_service.SyncService'


class _MartTestCase(TestCase):

    def setUp(self):
        for status_id, name in (
            (MART_STATUS_PENDING_ID, 'Mart Approval'),
            (MART_STATUS_APPROVED_ID, 'Mart Approved'),
            (MART_STATUS_COMPLETED_ID, 'Completed'),
        ):
            OrderStatus.objects.get_or_create(
                id=status_id, defaults={'name': name})

        role, _ = UserRole.objects.get_or_create(
            name='admin', defaults={'display_name': 'Admin'})
        self.approver = User.objects.create_user(
            username='m-approver', password='pw', name='Approver',
            role=role, is_staff=True)

        self.client = APIClient()
        self.client.force_authenticate(self.approver)

    def _order(self, **kw):
        return Order.objects.create(
            order_number=kw.pop('order_number', 'MART-0001'),
            order_type='DISTRIBUTOR',
            status=OrderStatus.objects.get(id=MART_STATUS_PENDING_ID),
            created_by=self.approver,
            **kw,
        )

    def _approve(self, order):
        return self.client.post(
            reverse('mart-approve', args=[order.id]), {}, format='json')

    def _resend(self, order):
        return self.client.post(
            reverse('mart-resend-sap', args=[order.id]), {}, format='json')


class ApproveSuccessTests(_MartTestCase):
    """The change must not break the ordinary path."""

    def test_a_first_approval_books_the_order(self):
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order',
                   return_value={'DocEntry': 42, 'DocNum': 1001}) as push:
            res = self._approve(order)

        self.assertEqual(res.status_code, 200, res.content)
        push.assert_called_once()
        order.refresh_from_db()
        self.assertTrue(order.sap_created)
        self.assertEqual(order.status_id, MART_STATUS_COMPLETED_ID)
        self.assertEqual(res.json()['sap']['doc_entry'], 42)

    def test_approval_records_who_approved_and_when(self):
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order', return_value={}):
            self._approve(order)
        order.refresh_from_db()
        self.assertEqual(order.approved_by_id, self.approver.pk)
        self.assertIsNotNone(order.approved_at)


class DoubleSubmissionTests(_MartTestCase):
    """The bug: two SAP sales orders for one OMS order."""

    def test_approving_twice_pushes_to_sap_only_once(self):
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order',
                   return_value={'DocEntry': 42}) as push:
            first = self._approve(order)
            second = self._approve(order)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(push.call_count, 1,
                         'the second approval booked a second SAP sales order')

    def test_the_claim_is_set_before_the_sap_call_not_after(self):
        """The subtle half of the fix.

        Checking `sap_created` under the lock and releasing it before the SAP
        call leaves the race untouched: the flag is only written once a post has
        SUCCEEDED, so two concurrent requests would both read False and both
        post. The claim has to be written inside the same locked block that
        reads it.

        Asserted by observing the row from inside the SAP call itself — the
        exact moment a concurrent request would look at it.
        """
        order = self._order()
        seen = {}

        def _observe(self_, order_arg):
            seen['sap_created'] = Order.objects.get(pk=order_arg.pk).sap_created
            return {'DocEntry': 7}

        with patch(f'{SERVICE}.create_sales_order', _observe):
            self._approve(order)

        self.assertTrue(
            seen['sap_created'],
            'the claim was not committed before the SAP call — a concurrent '
            'request would still see an unbooked order and post a duplicate')

    def test_resending_a_booked_order_is_refused(self):
        order = self._order(sap_created=True)
        with patch(f'{SERVICE}.create_sales_order') as push:
            res = self._resend(order)
        self.assertEqual(res.status_code, 409)
        push.assert_not_called()

    def test_resend_catches_the_state_the_status_check_missed(self):
        """`create_sales_order` sets `sap_created` BEFORE the caller moves the
        order to Completed, so `sap_created=True, status='Mart Approved'` is
        reachable — the push succeeded and the status save did not.

        The old guard tested only the status, so it called that order retryable
        and a retry booked a duplicate.
        """
        order = self._order(sap_created=True)
        order.status_id = MART_STATUS_APPROVED_ID
        order.save(update_fields=['status'])

        with patch(f'{SERVICE}.create_sales_order') as push:
            res = self._resend(order)

        self.assertEqual(res.status_code, 409)
        push.assert_not_called()


class SapFailureTests(_MartTestCase):
    """A SAP rejection and a SAP timeout are NOT the same outcome.

    `payments/sap_poster.py` treats the distinction as a financial invariant:
    a rejection committed nothing, so a retry is correct; a timeout may have
    committed, so a retry duplicates. Collapsing them produces duplicate
    documents.
    """

    def test_a_sap_rejection_releases_the_claim_so_retry_works(self):
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order',
                   side_effect=Exception('Invalid item code')):
            res = self._approve(order)

        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json()['sap_state'], 'REJECTED')
        order.refresh_from_db()
        self.assertFalse(order.sap_created, 'a rejected order must be retryable')
        self.assertEqual(order.status_id, MART_STATUS_APPROVED_ID,
                         'the approval stands; only the push failed')

    def test_a_retry_after_a_rejection_succeeds(self):
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order',
                   side_effect=Exception('Invalid item code')):
            self._approve(order)

        with patch(f'{SERVICE}.create_sales_order',
                   return_value={'DocEntry': 99}) as push:
            res = self._resend(order)

        self.assertEqual(res.status_code, 200, res.content)
        push.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.status_id, MART_STATUS_COMPLETED_ID)

    def test_a_sap_timeout_keeps_the_claim_and_blocks_retry(self):
        """The case that costs money if it is got wrong.

        No response means the request never completed, so SAP may hold a sales
        order for this document. Retrying books a second one. A blocked order is
        recoverable by hand; a duplicate financial document is not.
        """
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order',
                   side_effect=requests.exceptions.ConnectTimeout('no answer')):
            res = self._approve(order)

        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json()['sap_state'], 'UNKNOWN')
        order.refresh_from_db()
        self.assertTrue(order.sap_created,
                        'an unknown SAP outcome must NOT be retryable')

        with patch(f'{SERVICE}.create_sales_order') as push:
            retry = self._resend(order)
        self.assertEqual(retry.status_code, 409)
        push.assert_not_called()

    def test_an_http_error_carrying_a_response_counts_as_a_rejection(self):
        """A `requests` exception is only ambiguous when the request never
        completed. One that carries a response means SAP answered and refused,
        which is safe to retry — treating it as unknown would block recoverable
        orders for no reason."""
        exc = requests.exceptions.HTTPError('400 Bad Request')
        exc.response = object()
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order', side_effect=exc):
            res = self._approve(order)

        self.assertEqual(res.json()['sap_state'], 'REJECTED')
        order.refresh_from_db()
        self.assertFalse(order.sap_created)


class AuthorizationTests(_MartTestCase):

    def test_a_non_approver_cannot_approve(self):
        role, _ = UserRole.objects.get_or_create(
            name='salesman', defaults={'display_name': 'Salesman'})
        outsider = User.objects.create_user(
            username='m-outsider', password='pw', name='Outsider', role=role)
        client = APIClient()
        client.force_authenticate(outsider)
        order = self._order()

        with patch(f'{SERVICE}.create_sales_order') as push:
            res = client.post(reverse('mart-approve', args=[order.id]), {},
                              format='json')

        self.assertEqual(res.status_code, 403)
        push.assert_not_called()
        order.refresh_from_db()
        self.assertFalse(order.sap_created)

    def test_an_anonymous_caller_cannot_approve(self):
        order = self._order()
        with patch(f'{SERVICE}.create_sales_order') as push:
            res = APIClient().post(reverse('mart-approve', args=[order.id]), {},
                                   format='json')
        self.assertIn(res.status_code, (401, 403))
        push.assert_not_called()
