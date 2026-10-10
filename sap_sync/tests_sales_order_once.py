"""A party order gets at most one SAP sales order, and only at the auditor.

Ten live orders had two or three sales orders each (ORD-20261006-0011 had
three in five minutes) and several rejected orders had one: the SO endpoints
took any order, at any stage, from any signed-in user, as often as asked.
`_create_sap_document_once` now claims the order under a row lock first.

Run with::

    python manage.py test sap_sync.tests_sales_order_once --settings=OMS.test_settings
"""
from unittest.mock import patch

import requests
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from orders.models import Order, OrderStatus
from sap_sync.models import SalesOrderLog
from users.models import User, UserRole


def _user(username, role, **extra):
    role_obj, _ = UserRole.objects.get_or_create(name=role, defaults={'display_name': role})
    return User.objects.create_user(username=username, password='pw', name=username,
                                    role=role_obj, **extra)


class _SalesOrderTestCase(TestCase):

    def setUp(self):
        self.auditor_stage = OrderStatus.objects.create(name='Auditor Approval', code='AUDITOR_APPROVAL')
        self.completed = OrderStatus.objects.create(name='Completed', code='COMPLETED')
        self.rejected = OrderStatus.objects.create(name='Rejected', code='REJECTED')
        self.auditor = _user('so-auditor', 'auditor')
        self.billing = _user('so-billing', 'billing')
        self.admin = _user('so-admin', 'manager', is_staff=True)

    def _order(self, status, **fields):
        return Order.objects.create(order_number=f'ORD-SO-{Order.objects.count() + 1}',
                                    status=status, created_by=self.billing, **fields)

    def _post(self, url_name, user, order):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(reverse(url_name), {'order_id': order.id}, format='json')


@patch('sap_sync.views.SyncService')
class ApproveSalesOrderTests(_SalesOrderTestCase):

    def test_creates_the_sales_order_at_the_auditor_stage(self, service):
        service.return_value.create_sales_order.return_value = {'DocEntry': 1, 'DocNum': 11}
        order = self._order(self.auditor_stage)

        res = self._post('approve-sales-order', self.auditor, order)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['data']['DocNum'], 11)
        order.refresh_from_db()
        self.assertTrue(order.sap_created)

    def test_a_second_request_does_not_book_a_second_sales_order(self, service):
        service.return_value.create_sales_order.return_value = {'DocEntry': 1, 'DocNum': 11}
        order = self._order(self.auditor_stage)
        self._post('approve-sales-order', self.auditor, order)
        SalesOrderLog.objects.create(order_id=str(order.id), status='SUCCESS',
                                     sap_doc_entry=1, sap_doc_num=11)

        res = self._post('approve-sales-order', self.auditor, order)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['data']['DocNum'], 11, 'the existing SO is returned')
        self.assertEqual(service.return_value.create_sales_order.call_count, 1)

    def test_a_claim_without_a_recorded_sales_order_blocks_a_retry(self, service):
        order = self._order(self.auditor_stage, sap_created=True)

        res = self._post('approve-sales-order', self.auditor, order)

        self.assertEqual(res.status_code, 409, res.content)
        service.return_value.create_sales_order.assert_not_called()

    def test_refused_for_an_order_not_at_the_auditor(self, service):
        # ORD-20261006-0013: rejected in OMS, yet booked in SAP.
        order = self._order(self.rejected)

        res = self._post('approve-sales-order', self.auditor, order)

        self.assertEqual(res.status_code, 409, res.content)
        service.return_value.create_sales_order.assert_not_called()

    def test_refused_for_a_user_who_is_not_the_auditor(self, service):
        order = self._order(self.auditor_stage)

        res = self._post('approve-sales-order', self.billing, order)

        self.assertEqual(res.status_code, 403, res.content)
        service.return_value.create_sales_order.assert_not_called()

    def test_a_definite_sap_refusal_releases_the_claim(self, service):
        service.return_value.create_sales_order.side_effect = Exception('SAP said no')
        order = self._order(self.auditor_stage)

        res = self._post('approve-sales-order', self.auditor, order)

        self.assertEqual(res.status_code, 500)
        order.refresh_from_db()
        self.assertFalse(order.sap_created, 'nothing was created, so a retry must be possible')

    def test_an_unknown_sap_outcome_keeps_the_claim(self, service):
        service.return_value.create_sales_order.side_effect = requests.exceptions.ConnectionError()
        order = self._order(self.auditor_stage)

        res = self._post('approve-sales-order', self.auditor, order)

        self.assertEqual(res.status_code, 500)
        self.assertIn('check SAP', res.json()['message'])
        order.refresh_from_db()
        self.assertTrue(order.sap_created, 'SAP may hold the SO; a retry could duplicate it')


@patch('sap_sync.views.SyncService')
class RepairEndpointTests(_SalesOrderTestCase):
    """`push-order` is the repair for an order completed without its SO."""

    def test_admin_can_create_the_missing_sales_order_of_a_completed_order(self, service):
        service.return_value.create_sales_order.return_value = {'DocEntry': 2, 'DocNum': 22}
        order = self._order(self.completed, sap_created=False)

        res = self._post('push-order', self.admin, order)

        self.assertEqual(res.status_code, 200, res.content)
        order.refresh_from_db()
        self.assertTrue(order.sap_created)

    def test_refused_for_a_non_admin(self, service):
        order = self._order(self.completed, sap_created=False)

        res = self._post('push-order', self.auditor, order)

        self.assertEqual(res.status_code, 403, res.content)
        service.return_value.create_sales_order.assert_not_called()

    def test_quotation_endpoint_is_admin_only(self, service):
        order = self._order(self.auditor_stage)

        res = self._post('approve-order', self.auditor, order)

        self.assertEqual(res.status_code, 403, res.content)
