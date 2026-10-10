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

from orders.models import (
    Order, OrderFlowConfig, OrderRateApproval, OrderStatus, OrdersLog,
)
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
            id=RATE_APPROVAL_ID, name='Rate Approval', code='RATE_APPROVAL')
        self.approved = OrderStatus.objects.create(
            id=RATE_APPROVED_ID, name='Approved', code='APPROVED')

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

        with patch('orders.views.notifications._resolve_notification_recipients',
                   return_value=self._plan()):
            with patch('orders.views.notifications.deliver_notification_to_many') as deliver:
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

        with patch('orders.views.notifications._resolve_notification_recipients',
                   return_value=plan):
            with patch('orders.views.notifications.deliver_notification_to_many') as deliver:
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

        with patch('orders.views.notifications.deliver_notification_to_many') as deliver:
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




# ─────────────────────────────────────────────────────────────────────────
# Desk rules — `orders.services.order_status.APPROVE_REQUEST_BY_STAGE`.
#
# Statuses carry their production names and codes, and the default ASM flow
# (Rate Approval → Billing → Auditor Approval → Completed) is in force. Each
# refusal test names the live order that went wrong before these rules.
# ─────────────────────────────────────────────────────────────────────────

class _DeskTestCase(TestCase):

    def setUp(self):
        def make(name, code):
            return OrderStatus.objects.create(name=name, code=code)
        self.rate = make('Rate Approval', 'RATE_APPROVAL')
        self.approved = make('Approved', 'APPROVED')
        self.billing = make('Billing', 'BILLING')
        self.auditor = make('Auditor Approval', 'AUDITOR_APPROVAL')
        self.completed = make('Completed', 'COMPLETED')
        self.rejected = make('Rejected', 'REJECTED')
        self.billing_rejected = make('Billing Rejected', 'BILLING_REJECTED')

        self.manager = User.objects.create_user(
            username='desk-manager', password='pw', name='Manager', role=_role('manager'))
        self.biller = User.objects.create_user(
            username='desk-billing', password='pw', name='Billing', role=_role('billing'))
        self.auditor_user = User.objects.create_user(
            username='desk-auditor', password='pw', name='Auditor', role=_role('auditor'))
        self.approver = User.objects.create_user(
            username='desk-approver', password='pw', name='Approver', role=_role('approver'))
        self.approver_b = User.objects.create_user(
            username='desk-approver-b', password='pw', name='Approver B', role=_role('approver'))

    def _order(self, status, creator=None, **fields):
        return Order.objects.create(
            order_number=f'ORD-DESK-{Order.objects.count() + 1}',
            status=status, created_by=creator or self.manager, **fields)

    def _submit(self, order, user, status_obj, **body):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(reverse('update-status', args=[order.id]),
                           {'status': status_obj.id, **body}, format='json')

    def _assert_refused(self, res, order, status_obj):
        self.assertEqual(res.status_code, 409, res.content)
        order.refresh_from_db()
        self.assertEqual(order.status_id, status_obj.id, 'a refused request moved the order')


class BillingDeskTests(_DeskTestCase):

    def test_accept_sends_to_auditor_and_logs_who_accepted(self):
        order = self._order(self.billing)
        OrdersLog.objects.create(order=order, action=self.billing, performed_by=None)

        res = self._submit(order, self.biller, self.auditor)

        self.assertEqual(res.status_code, 200, res.content)
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.auditor.id)
        billing_log = OrdersLog.objects.get(order=order, action=self.billing)
        self.assertEqual(billing_log.performed_by_id, self.biller.id)
        self.assertEqual(billing_log.remarks, 'Accepted by billing')
        self.assertTrue(OrdersLog.objects.filter(
            order=order, action=self.auditor, performed_by__isnull=True).exists())

    def test_a_second_accept_does_not_complete_the_order(self):
        # ORD-20260930-0014: the second Accept landed at the auditor stage and
        # completed the order with no sales order.
        order = self._order(self.billing)
        self._submit(order, self.biller, self.auditor)

        res = self._submit(order, self.biller, self.auditor)

        self._assert_refused(res, order, self.auditor)
        self.assertFalse(OrdersLog.objects.filter(order=order, action=self.completed).exists())

    def test_an_admin_double_click_does_not_skip_the_auditor(self):
        admin = User.objects.create_user(
            username='desk-admin', password='pw', name='Admin', role=_role('manager'),
            is_staff=True)
        order = self._order(self.billing)
        self._submit(order, admin, self.auditor)

        res = self._submit(order, admin, self.auditor)

        self._assert_refused(res, order, self.auditor)

    def test_reject_closes_the_billing_stage_and_records_who_rejected(self):
        order = self._order(self.billing)
        OrdersLog.objects.create(order=order, action=self.billing, performed_by=None)

        res = self._submit(order, self.biller, self.billing_rejected, reason='wrong SKU')

        self.assertEqual(res.status_code, 200, res.content)
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.billing_rejected.id)
        self.assertEqual(order.rejected_by_id, self.biller.id)
        self.assertEqual(order.rejection_reason, 'wrong SKU')
        billing_log = OrdersLog.objects.get(order=order, action=self.billing)
        self.assertEqual(billing_log.performed_by_id, self.biller.id)

    def test_a_billing_rejected_order_cannot_be_accepted(self):
        # ORD-20261008-0001: accepted from a stale queue 45s after rejection.
        order = self._order(self.billing_rejected)

        res = self._submit(order, self.biller, self.auditor)

        self._assert_refused(res, order, self.billing_rejected)
        self.assertIn('resubmitted', res.json()['message'])

    def test_a_rate_rejected_order_cannot_be_accepted_by_billing(self):
        # ORD-20261010-0017: billing creator forwarded a rate-rejected order.
        order = self._order(self.rejected, creator=self.biller)

        res = self._submit(order, self.biller, self.auditor)

        self._assert_refused(res, order, self.rejected)

    def test_another_desk_cannot_act_at_billing(self):
        order = self._order(self.billing)

        res = self._submit(order, self.auditor_user, self.completed)

        self._assert_refused(res, order, self.billing)


class AuditorDeskTests(_DeskTestCase):

    def test_completes_once_the_sales_order_exists(self):
        order = self._order(self.auditor, sap_created=True)
        OrdersLog.objects.create(order=order, action=self.auditor, performed_by=None)

        res = self._submit(order, self.auditor_user, self.completed)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['message'], 'Order completed successfully')
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.completed.id)
        auditor_log = OrdersLog.objects.get(order=order, action=self.auditor)
        self.assertEqual(auditor_log.performed_by_id, self.auditor_user.id)
        completed_log = OrdersLog.objects.get(order=order, action=self.completed)
        self.assertEqual(completed_log.performed_by_id, self.auditor_user.id)

    def test_cannot_complete_without_a_sales_order(self):
        order = self._order(self.auditor, sap_created=False)

        res = self._submit(order, self.auditor_user, self.completed)

        self._assert_refused(res, order, self.auditor)
        self.assertIn('SAP', res.json()['message'])

    def test_a_completed_order_cannot_be_rejected(self):
        # ORD-20260930-0023: rejected 50 minutes after its SO was created.
        order = self._order(self.completed, sap_created=True)

        res = self._submit(order, self.auditor_user, self.rejected, reason='late')

        self._assert_refused(res, order, self.completed)

    def test_reject_records_who_rejected(self):
        order = self._order(self.auditor)

        res = self._submit(order, self.auditor_user, self.rejected, reason='rates')

        self.assertEqual(res.status_code, 200, res.content)
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.rejected.id)
        self.assertEqual(order.rejected_by_id, self.auditor_user.id)
        auditor_log = OrdersLog.objects.get(order=order, action=self.auditor)
        self.assertEqual(auditor_log.performed_by_id, self.auditor_user.id)
        self.assertEqual(auditor_log.remarks, 'rates')

    def test_resubmitting_the_same_rejection_is_a_no_op(self):
        order = self._order(self.rejected)

        res = self._submit(order, self.auditor_user, self.rejected)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['message'], 'Order already rejected')
        self.assertFalse(OrdersLog.objects.filter(order=order).exists())


class RateDeskTests(_DeskTestCase):

    def _assign(self, order, *approvers):
        for approver in approvers:
            OrderRateApproval.objects.create(order=order, approver=approver, status='PENDING')

    def test_the_last_approval_moves_the_order_to_billing(self):
        order = self._order(self.rate)
        self._assign(order, self.approver)

        res = self._submit(order, self.approver, self.approved)

        self.assertEqual(res.status_code, 200, res.content)
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.billing.id)
        self.assertTrue(OrdersLog.objects.filter(
            order=order, action=self.billing, performed_by__isnull=True).exists())

    def test_a_later_approval_cannot_override_a_rejection(self):
        # ORD-20260730-0023: approver A rejected, approver B then approved and
        # the order was left in "Approved".
        order = self._order(self.rate)
        self._assign(order, self.approver, self.approver_b)
        self._submit(order, self.approver, self.rejected, reason='rate revise')

        res = self._submit(order, self.approver_b, self.approved)

        self._assert_refused(res, order, self.rejected)

    def test_a_stale_approval_cannot_skip_billing(self):
        order = self._order(self.rate)
        self._assign(order, self.approver)
        self._submit(order, self.approver, self.approved)

        res = self._submit(order, self.approver, self.approved)

        self._assert_refused(res, order, self.billing)

    def test_a_stale_approval_cannot_complete_a_billing_created_order(self):
        # ORD-20260812-0024: one approver's clicks went Rate Approval →
        # Auditor Approval → Completed in 9 seconds, with no sales order.
        # The billing flow as configured live: rate approval on, no billing stage.
        OrderFlowConfig.objects.create(
            flow_type='BILLING', rate_approval_enabled=True, billing_enabled=False,
            auditor_enabled=True, rate_conditions=[])
        order = self._order(self.rate, creator=self.biller)
        self._assign(order, self.approver)
        self._submit(order, self.approver, self.approved)
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.auditor.id)

        res = self._submit(order, self.approver, self.approved)

        self._assert_refused(res, order, self.auditor)


class BillingReviewEditTests(TestCase):
    """`_is_billing_review_edit` — only a billing user's edit of an order AT
    the billing desk skips straight to the auditor. ORD-20261010-0017: the
    billing creator edited their rate-rejected order and it skipped the rate
    check."""

    def _status(self, name, code):
        return OrderStatus.objects.create(name=name, code=code)

    def test_billing_edit_at_the_billing_desk_is_a_review(self):
        from orders.views.lifecycle import _is_billing_review_edit
        self.assertTrue(_is_billing_review_edit('billing', self._status('Billing', 'BILLING')))
        self.assertTrue(_is_billing_review_edit(
            'billing', self._status('Billing Pending', 'BILLING_PENDING')))

    def test_billing_edit_elsewhere_is_a_resubmission(self):
        from orders.views.lifecycle import _is_billing_review_edit
        for name, code in [('Rejected', 'REJECTED'), ('Billing Rejected', 'BILLING_REJECTED'),
                           ('Rate Approval', 'RATE_APPROVAL'), ('Order Created', 'CREATED')]:
            self.assertFalse(_is_billing_review_edit('billing', self._status(name, code)), name)

    def test_other_roles_never_review(self):
        from orders.views.lifecycle import _is_billing_review_edit
        self.assertFalse(_is_billing_review_edit('manager', self._status('Billing', 'BILLING')))


class EditLockTests(_DeskTestCase):
    """A Completed order, or any order with a sales order in SAP, cannot be
    edited back into the flow (ORD-20260725-0007 was)."""

    def _edit(self, order, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.put(reverse('update-order', args=[order.id]), {}, format='json')

    def test_a_completed_order_cannot_be_edited(self):
        order = self._order(self.completed, sap_created=True)

        res = self._edit(order, self.biller)

        self.assertEqual(res.status_code, 409, res.content)
        order.refresh_from_db()
        self.assertEqual(order.status_id, self.completed.id)

    def test_an_order_with_a_sales_order_cannot_be_edited(self):
        order = self._order(self.rejected, sap_created=True)

        res = self._edit(order, self.manager)

        self.assertEqual(res.status_code, 409, res.content)

    def test_a_rejected_order_without_a_sales_order_can_still_be_edited(self):
        from orders.views.lifecycle import _edit_locked_error
        self.assertIsNone(_edit_locked_error(self._order(self.rejected)))


class EditFlowGuardTests(TestCase):
    """Edits cannot switch an order between the party and distributor flows,
    and cannot park an order in Rate Approval with nobody to approve it."""

    def test_switching_into_or_out_of_the_distributor_flow_is_refused(self):
        from orders.views.lifecycle import _switches_distributor_flow
        party = Order(order_type='PARTY')
        distributor = Order(order_type='DISTRIBUTOR')
        self.assertTrue(_switches_distributor_flow(party, 'DISTRIBUTOR'))
        self.assertTrue(_switches_distributor_flow(distributor, 'PARTY'))
        self.assertFalse(_switches_distributor_flow(party, 'PARTY'))
        self.assertFalse(_switches_distributor_flow(party, 'STAFF'))

    def test_rate_approval_with_no_approver_is_refused_and_rolled_back(self):
        from django.db import transaction
        from orders.views.lifecycle import _enter_rate_approval
        rate = OrderStatus.objects.create(name='Rate Approval', code='RATE_APPROVAL')
        creator = User.objects.create_user(username='nr-c', password='pw', name='C', role=_role('manager'))

        with transaction.atomic():
            order = Order.objects.create(order_number='ORD-NR-1', status=rate, created_by=creator)
            res = _enter_rate_approval(order, rate)

        self.assertEqual(res.status_code, 400)
        self.assertFalse(Order.objects.filter(order_number='ORD-NR-1').exists(),
                         'the refused submission should be rolled back')

    def test_rate_approval_resets_earlier_decisions(self):
        from orders.views.lifecycle import _enter_rate_approval
        rate = OrderStatus.objects.create(name='Rate Approval', code='RATE_APPROVAL')
        creator = User.objects.create_user(username='nr-c2', password='pw', name='C', role=_role('manager'))
        approver = User.objects.create_user(username='nr-a', password='pw', name='A', role=_role('approver'))
        order = Order.objects.create(order_number='ORD-NR-2', status=rate, created_by=creator)
        decision = OrderRateApproval.objects.create(order=order, approver=approver, status='REJECTED')

        self.assertIsNone(_enter_rate_approval(order, rate))
        decision.refresh_from_db()
        self.assertEqual(decision.status, 'PENDING')
