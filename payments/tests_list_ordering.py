"""Newest first, by the serial the entry was created with.

The model's default orders by the date a user TYPES (`payment_date`,
`deposit_date`), so an entry raised today for last week's date sorts below one
raised a week ago — a list nobody has filtered does not read newest-first. And
under "All" the rows are grouped by what must be done about them, so they
appear to move up and down as their statuses change.

These pin the opt-in `?ordering=` that lets a client ask for serial order, and
pin that asking for nothing still gets the old behaviour — the web app and the
reports read that default.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from payments.models import PaymentReceipt

User = get_user_model()


class ReceiptListOrderingTests(TestCase):
    """`GET /api/payments/receipts/?ordering=-id`."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='order_probe', password='x', is_superuser=True,
            is_staff=True)

        today = date.today()
        # THE CASE THAT BREAKS DATE ORDERING: created last (so the highest id)
        # but back-dated, so `-payment_date` buries it.
        cls.newest_backdated = cls._receipt(
            'ZZ-NEWEST-BACKDATED', today - timedelta(days=30))
        cls.oldest_recent_date = cls._receipt(
            'AA-OLDEST-RECENT-DATE', today)
        # Created last of all, with today's date.
        cls.newest = cls._receipt('ZZ-NEWEST', today)

    @classmethod
    def _receipt(cls, no, payment_date):
        return PaymentReceipt.objects.create(
            receipt_no=no,
            company='OIL',
            card_code='TEST',
            card_name='Test Party',
            payment_date=payment_date,
            total_amount=Decimal('1.00'),
            created_by=cls.user,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _ids(self, query=''):
        response = self.client.get(f'/api/payments/receipts/{query}')
        self.assertEqual(response.status_code, 200, response.content[:300])
        rows = response.json()['data']['results']
        ours = {self.newest.id, self.oldest_recent_date.id,
                self.newest_backdated.id}
        # Membership, not equality: this database is shared with real records.
        return [r['id'] for r in rows if r['id'] in ours]

    def test_ordering_by_serial_puts_the_newest_entry_first(self):
        ids = self._ids('?ordering=-id&page_size=200')
        self.assertEqual(
            ids,
            [self.newest.id, self.oldest_recent_date.id,
             self.newest_backdated.id],
        )

    def test_a_back_dated_entry_created_last_still_comes_first(self):
        """The whole point. `-payment_date` would bury it 30 days down."""
        ids = self._ids('?ordering=-id&page_size=200')
        self.assertEqual(ids[0], self.newest.id)
        self.assertLess(
            ids.index(self.newest_backdated.id),
            len(ids),
            'the back-dated receipt should still be listed',
        )
        # Its id is the second highest, so it must not be last by date.
        self.assertEqual(ids[-1], self.newest_backdated.id)

    def test_ascending_serial_is_available_too(self):
        ids = self._ids('?ordering=id&page_size=200')
        self.assertEqual(ids, sorted(ids))

    def test_asking_for_nothing_keeps_the_default_the_web_app_reads(self):
        """No `ordering=` must not change what any existing client sees."""
        ids = self._ids('?page_size=200')
        # Default is `-payment_date, -id`: both of today's rows precede the
        # back-dated one, newest id first within the same date.
        self.assertEqual(
            ids,
            [self.newest.id, self.oldest_recent_date.id,
             self.newest_backdated.id],
        )

    def test_ordering_overrides_the_status_grouping_of_the_All_view(self):
        """`group_by_status` is what makes rows appear to move up and down."""
        self.newest.status = PaymentReceipt.Status.POSTED
        self.newest.save(update_fields=['status'])
        self.oldest_recent_date.status = (
            PaymentReceipt.Status.PENDING_APPROVAL)
        self.oldest_recent_date.save(update_fields=['status'])

        grouped = self._ids('?group_by_status=true&page_size=200')
        self.assertEqual(grouped[0], self.oldest_recent_date.id,
                         'grouping should put the pending row first')

        serial = self._ids(
            '?group_by_status=true&ordering=-id&page_size=200')
        self.assertEqual(serial[0], self.newest.id,
                         'an explicit ordering must win over the grouping')

    def test_a_hostile_ordering_value_is_ignored_not_executed(self):
        for hostile in ['id; DROP TABLE payments_paymentreceipt',
                        'password', '-created_by__password', '../etc/passwd']:
            response = self.client.get(
                f'/api/payments/receipts/?ordering={hostile}&page_size=200')
            self.assertEqual(response.status_code, 200, hostile)


class DepositListOrderingTests(TestCase):
    """The deposits list takes the same param, for the same reason."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='order_probe_dep', password='x', is_superuser=True,
            is_staff=True)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_endpoint_accepts_ordering_by_serial(self):
        response = self.client.get(
            '/api/payments/deposits/?ordering=-id&page_size=200')
        self.assertEqual(response.status_code, 200, response.content[:300])
        rows = response.json()['data']['results']
        ids = [r['id'] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_a_hostile_value_falls_back_rather_than_ordering_by_it(self):
        response = self.client.get(
            '/api/payments/deposits/?ordering=-created_by__password')
        self.assertEqual(response.status_code, 200)
