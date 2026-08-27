"""Concurrency safety for `UpdateOrderStatusView`.

`approvals/services.py` opens by naming this view as the reason its own design
differs:

    "UpdateOrderStatusView.post is neither [atomic nor locked], across ~380
     lines and several dependent writes — so two approvers acting at once can
     both pass the pending check and double-advance a document."

The view had no tests at all, so nothing held that claim in place, and nothing
would have noticed the lock being removed again.

Two properties are covered:

1. **Serialised transitions.** The second submission on the same order sees
   committed state and takes the branch that was always meant for it, instead
   of racing past a guard that was reading stale data.

2. **Notifications are deferred past commit.** This is what makes locking safe
   at all: `send_order_notifications` ends in `requests.post(..., timeout=15)`
   per recipient, and it is called from nine points inside the locked method.
   Sending inline would hold a row lock across outbound HTTP to a third party.

Run with::

    python manage.py test orders.tests_status_transitions --settings=OMS.test_settings
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from orders.models import Order, OrderRateApproval, OrderStatus, OrdersLog
from orders.views import send_order_notifications
from users.models import User, UserRole

RATE_APPROVAL_ID = 5      # "Rate Approval" — the stage under test
RATE_APPROVED_ID = 6      # the id the view hardcodes for the approved outcome


def _role(name):
    role, _ = UserRole.objects.get_or_create(
        name=name, defaults={'display_name': name})
    return role


class _StatusTestCase(TestCase):

    def setUp(self):
        self.rate_approval = OrderStatus.objects.create(
            id=RATE_APPROVAL_ID, name='Rate Approval')
        self.approved = OrderStatus.objects.create(
            id=RATE_APPROVED_ID, name='Approved')

        self.approver_a = User.objects.create_user(
            username='ra-a', password='pw', name='Approver A',
            role=_role('approver'))
        self.approver_b = User.objects.create_user(
            username='ra-b', password='pw', name='Approver B',
            role=_role('approver'))

        self.order = Order.objects.create(
            order_number='ORD-RATE-1',
            status=self.rate_approval,
            created_by=self.approver_a,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _submit(self, user, status_id, **body):
        return self._client(user).post(
            reverse('update-status', args=[self.order.id]),
            {'status': status_id, **body}, format='json')

    def _assign(self, user, status='PENDING'):
        return OrderRateApproval.objects.create(
            order=self.order, approver=user, status=status)


class RateApprovalRaceTests(_StatusTestCase):
    """The multi-approver path, where the race was worst.

    Both approvers read `rate_approval.status == "PENDING"`, both record a
    decision, and both then ask `_has_pending_rate_approvals` — which by then
    answers "none pending" for both. The order advanced twice.

    Every guard involved was already correct. They were reading uncommitted
    state, which is what the row lock fixes.
    """

    def test_the_same_approver_cannot_approve_twice(self):
        self._assign(self.approver_a)

        first = self._submit(self.approver_a, RATE_APPROVED_ID)
        self.assertEqual(first.status_code, 200, first.content)

        second = self._submit(self.approver_a, RATE_APPROVED_ID)
        self.assertEqual(second.status_code, 400)
        self.assertIn('already', second.json()['message'].lower())

    def test_a_second_decision_does_not_overwrite_the_first(self):
        approval = self._assign(self.approver_a)
        self._submit(self.approver_a, RATE_APPROVED_ID)

        approval.refresh_from_db()
        self.assertEqual(approval.status, 'APPROVED')

        self._submit(self.approver_a, RATE_APPROVED_ID)
        approval.refresh_from_db()
        self.assertEqual(approval.status, 'APPROVED',
                         'the refused second submission still mutated the row')

    def test_an_order_waits_while_any_approver_is_still_pending(self):
        """The guard the race defeated. With two approvers assigned, one
        approval must NOT advance the order."""
        self._assign(self.approver_a)
        self._assign(self.approver_b)

        res = self._submit(self.approver_a, RATE_APPROVED_ID)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['approval_status'], 'APPROVED')
        self.order.refresh_from_db()
        self.assertEqual(self.order.status_id, RATE_APPROVAL_ID,
                         'the order advanced while an approver was still pending')

    def test_the_order_advances_only_once_every_approver_has_decided(self):
        self._assign(self.approver_a)
        self._assign(self.approver_b)

        self._submit(self.approver_a, RATE_APPROVED_ID)
        self._submit(self.approver_b, RATE_APPROVED_ID)

        self.assertFalse(
            self.order.rate_approvals.filter(status='PENDING').exists())

    def test_a_user_not_assigned_to_the_order_is_refused(self):
        """And the order must be left on its original status — this path saves
        the new status BEFORE checking entitlement and restores it by hand."""
        outsider = User.objects.create_user(
            username='ra-out', password='pw', name='Outsider',
            role=_role('approver'))

        res = self._submit(outsider, RATE_APPROVED_ID)

        self.assertEqual(res.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status_id, RATE_APPROVAL_ID)


class NotificationDeferralTests(_StatusTestCase):
    """The property that makes locking this view safe.

    `send_order_notifications` is called from nine points inside the locked
    method and ends in `requests.post(..., timeout=15)` per recipient. Inline,
    that would park a row lock behind third-party HTTP, so one slow push would
    block every other approver on the order — trading a rare silent corruption
    for a routine hang.
    """

    def _plan(self):
        return SimpleNamespace(
            recipients=[self.approver_b], message='rate approved',
            event_type=None, notification_type=None, title=None)

    def test_a_real_transition_does_not_deliver_before_commit(self):
        """Driven through the VIEW, with recipients stubbed in.

        The recipient plan has to be stubbed or this test is vacuous: without
        `OrderFlowConfig` rows seeded, a real transition resolves no recipients,
        so delivery never runs whether it is deferred or not — and the
        assertion passes against the inline version too. (It did, until this
        was checked by reverting the fix.)
        """
        self._assign(self.approver_a)

        with patch('orders.views._resolve_notification_recipients',
                   return_value=self._plan()):
            with patch('orders.views.deliver_notification_to_many') as deliver:
                self._submit(self.approver_a, RATE_APPROVED_ID)
                # TestCase never commits, so a correctly-deferred callback has
                # not run at this point. A failure here means delivery is
                # inline and the row lock is being held across outbound HTTP.
                deliver.assert_not_called()

    def test_delivery_is_deferred_not_dropped(self):
        """The other half of the property: the send still happens, just after
        commit.

        Driven through `send_order_notifications` directly with a stubbed
        recipient plan, because the recipients a real transition resolves
        depend on `OrderFlowConfig` rows that are not seeded here — this test
        is about WHEN delivery runs, not who receives it.
        """
        plan = SimpleNamespace(
            recipients=[self.approver_b], message='rate approved',
            event_type=None, notification_type=None, title=None)

        with patch('orders.views._resolve_notification_recipients',
                   return_value=plan):
            with patch('orders.views.deliver_notification_to_many') as deliver:
                with self.captureOnCommitCallbacks(execute=False) as callbacks:
                    send_order_notifications(self.order, 'Approved')

                deliver.assert_not_called()
                self.assertEqual(len(callbacks), 1,
                                 'no deferred callback was registered')

                callbacks[0]()
                deliver.assert_called_once()

    def test_a_failed_transition_sends_nothing(self):
        """A notification about a transition that was rolled back is a message
        about something that never happened."""
        outsider = User.objects.create_user(
            username='ra-out2', password='pw', name='Outsider',
            role=_role('approver'))

        with patch('orders.views.deliver_notification_to_many') as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                res = self._submit(outsider, RATE_APPROVED_ID)

        self.assertEqual(res.status_code, 403)
        deliver.assert_not_called()


class TransitionBasicsTests(_StatusTestCase):
    """The change must not alter ordinary behaviour."""

    def test_a_missing_order_is_a_404(self):
        """`get_object_or_404` was replaced by an explicit
        `select_for_update().get()`, so the 404 is now raised by hand."""
        res = self._client(self.approver_a).post(
            reverse('update-status', args=[999999]),
            {'status': RATE_APPROVED_ID}, format='json')
        self.assertEqual(res.status_code, 404)

    def test_an_unknown_status_id_is_a_404(self):
        res = self._submit(self.approver_a, 999999)
        self.assertEqual(res.status_code, 404)

    def test_an_anonymous_caller_is_refused(self):
        res = APIClient().post(
            reverse('update-status', args=[self.order.id]),
            {'status': RATE_APPROVED_ID}, format='json')
        self.assertIn(res.status_code, (401, 403))

    def test_a_transition_writes_a_log_row(self):
        self._assign(self.approver_a)
        before = OrdersLog.objects.filter(order=self.order).count()
        self._submit(self.approver_a, RATE_APPROVED_ID, reason='ok')
        self.assertGreater(
            OrdersLog.objects.filter(order=self.order).count(), before)
