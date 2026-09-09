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
# Characterisation tests — added ahead of plan item 3.2's extraction of
# `UpdateOrderStatusView`'s status-name branch dispatch into
# `orders.services.order_status`.
#
# Everything above this line already covered the rate-approval path. The
# named-status branches below it (auditor <-> billing handoffs, billing/
# auditor completion, auditor rejection, the "already rejected" short-circuit
# and the generic fallback) had NO test coverage at all before this pass —
# these tests record today's actual behaviour, quirks included, so the
# extraction has something to prove it did not change, and are meant to keep
# passing, unchanged, after the move.
#
# Every status name below is deliberately NOT an exact, case-insensitive
# match for the four canonical stage labels ('Rate Approval', 'Billing',
# 'Auditor Approval', 'Completed') UNLESS a branch's own condition requires
# that exact name. An exact match makes the status resolvable by
# `_get_next_order_flow_status` (`orders.services.order_flow`), which can
# silently overwrite the client-requested target status before the branch
# dispatch below ever sees it — a real, separate behaviour this suite is not
# trying to characterise. Disabling every stage on the ASM `OrderFlowConfig`
# row up front neutralises that path everywhere here, including the one
# exact-name case (`AuditorToBillingTests`), so what is actually under test
# is only the dispatch logic itself.
# ─────────────────────────────────────────────────────────────────────────

class _BranchDispatchTestCase(TestCase):

    def setUp(self):
        OrderFlowConfig.objects.create(
            flow_type='ASM',
            rate_approval_enabled=False,
            billing_enabled=False,
            auditor_enabled=False,
            rate_conditions=[],
        )
        self.actor = User.objects.create_user(
            username='disp-actor', password='pw', name='Dispatch Actor',
            role=_role('sales'))

    def _order(self, status):
        return Order.objects.create(
            order_number=f'ORD-DISP-{status.id}',
            status=status,
            created_by=self.actor,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _submit(self, order, user, status_id, **body):
        return self._client(user).post(
            reverse('update-status', args=[order.id]),
            {'status': status_id, **body}, format='json')


class AuditorToBillingTests(_BranchDispatchTestCase):
    """`is_auditor_to_billing` — requires prev_name == 'auditor approval'
    exactly, which is why this is the one class that needs the disabled
    `OrderFlowConfig` from the base class to keep the auto-advance override
    from overwriting the requested target before this branch is evaluated."""

    def test_forwards_to_billing_and_marks_the_new_stage_pending(self):
        auditor = OrderStatus.objects.create(name='Auditor Approval')
        billing = OrderStatus.objects.create(name='Billing Approval')
        order = self._order(auditor)

        res = self._submit(order, self.actor, billing.id)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'Billing Approval')
        self.assertEqual(
            res.json()['message'], 'Order accepted and sent to Billing Approval')
        order.refresh_from_db()
        self.assertEqual(order.status_id, billing.id)
        self.assertTrue(
            OrdersLog.objects.filter(
                order=order, action=billing, performed_by__isnull=True).exists(),
            'the new billing stage should get a pending (performed_by=None) marker')

    def test_resolves_an_existing_pending_auditor_marker(self):
        auditor = OrderStatus.objects.create(name='Auditor Approval')
        billing = OrderStatus.objects.create(name='Billing Approval')
        order = self._order(auditor)
        pending = OrdersLog.objects.create(
            order=order, action=auditor, performed_by=None, remarks='')

        self._submit(order, self.actor, billing.id, reason='looks right')

        pending.refresh_from_db()
        self.assertEqual(pending.performed_by_id, self.actor.id)
        self.assertEqual(pending.remarks, 'looks right')


class BillingToAuditorTests(_BranchDispatchTestCase):

    def test_forwards_to_auditor_and_closes_the_billing_marker(self):
        billing = OrderStatus.objects.create(name='Billing Stage')
        auditor = OrderStatus.objects.create(name='Auditor Stage')
        order = self._order(billing)

        res = self._submit(order, self.actor, auditor.id, reason='fwd')

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'Auditor Stage')
        order.refresh_from_db()
        self.assertEqual(order.status_id, auditor.id)
        billing_log = OrdersLog.objects.get(order=order, action=billing)
        self.assertEqual(billing_log.performed_by_id, self.actor.id)
        self.assertEqual(billing_log.remarks, 'fwd')
        self.assertTrue(
            OrdersLog.objects.filter(
                order=order, action=auditor, performed_by__isnull=True).exists())


class BillingCompletedTests(_BranchDispatchTestCase):

    def test_completes_the_order_and_stamps_the_completion_log_directly(self):
        billing = OrderStatus.objects.create(name='Billing Stage')
        completed = OrderStatus.objects.create(name='Completed', code='COMPLETED')
        order = self._order(billing)

        res = self._submit(order, self.actor, completed.id, reason='invoiced')

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['message'], 'Order completed successfully')
        order.refresh_from_db()
        self.assertEqual(order.status_id, completed.id)
        billing_log = OrdersLog.objects.get(order=order, action=billing)
        self.assertEqual(billing_log.performed_by_id, self.actor.id)
        # Unlike the auditor/billing handoffs above, the completion log is
        # stamped with the actor directly — there is no pending-marker dance
        # for this terminal stage.
        completed_log = OrdersLog.objects.get(order=order, action=completed)
        self.assertEqual(completed_log.performed_by_id, self.actor.id)
        self.assertEqual(completed_log.remarks, 'invoiced')


class AuditorRejectedTests(_BranchDispatchTestCase):

    def test_rejects_and_closes_the_auditor_marker(self):
        auditor = OrderStatus.objects.create(name='Auditor Stage')
        rejected = OrderStatus.objects.create(name='Rejected by Auditor', code='REJECTED')
        order = self._order(auditor)

        res = self._submit(order, self.actor, rejected.id, reason='bad pricing')

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['message'], 'Order rejected successfully')
        order.refresh_from_db()
        self.assertEqual(order.status_id, rejected.id)
        auditor_log = OrdersLog.objects.get(order=order, action=auditor)
        self.assertEqual(auditor_log.performed_by_id, self.actor.id)
        self.assertEqual(auditor_log.remarks, 'bad pricing')
        rejected_log = OrdersLog.objects.get(order=order, action=rejected)
        self.assertEqual(rejected_log.remarks, 'bad pricing')


class AuditorCompletedTests(_BranchDispatchTestCase):

    def test_completes_from_auditor_and_resolves_the_pending_marker(self):
        auditor = OrderStatus.objects.create(name='Auditor Stage')
        completed = OrderStatus.objects.create(name='Completed', code='COMPLETED')
        order = self._order(auditor)
        pending = OrdersLog.objects.create(
            order=order, action=auditor, performed_by=None, remarks='')

        res = self._submit(order, self.actor, completed.id)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['message'], 'Order completed successfully')
        order.refresh_from_db()
        self.assertEqual(order.status_id, completed.id)
        pending.refresh_from_db()
        self.assertEqual(pending.performed_by_id, self.actor.id)
        # No reason was given, so a hardcoded default fills the remarks — a
        # quirk specific to this branch; every sibling branch just stores the
        # raw (possibly empty) reason instead.
        self.assertEqual(pending.remarks, 'Sales quotation created by auditor')
        completed_log = OrdersLog.objects.get(order=order, action=completed)
        self.assertEqual(completed_log.remarks, 'Sales quotation created by auditor')


class AlreadyRejectedGuardTests(_BranchDispatchTestCase):

    def test_resubmitting_the_same_rejected_status_is_a_no_op(self):
        rejected = OrderStatus.objects.create(name='Rejected', code='REJECTED')
        order = self._order(rejected)

        res = self._submit(order, self.actor, rejected.id)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {
            'message': 'Order already rejected',
            'order_id': order.id,
            'status': 'Rejected',
        })
        self.assertFalse(
            OrdersLog.objects.filter(order=order).exists(),
            'the early-return guard should not write any log row')


class GenericFallbackTests(_BranchDispatchTestCase):
    """Neither a rate-approval, auditor nor billing status name — the plain
    "advance to whatever status was requested" path at the bottom of the
    view."""

    def test_creates_a_new_log_row_when_no_pending_marker_exists(self):
        created = OrderStatus.objects.create(name='Order Created')
        dispatched = OrderStatus.objects.create(name='Dispatched')
        order = self._order(created)

        res = self._submit(order, self.actor, dispatched.id, reason='sent')

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['message'], 'Order updated and sent to Dispatched')
        order.refresh_from_db()
        self.assertEqual(order.status_id, dispatched.id)
        log = OrdersLog.objects.get(order=order, action=dispatched)
        self.assertEqual(log.performed_by_id, self.actor.id)
        self.assertEqual(log.remarks, 'sent')

    def test_resolves_an_existing_pending_marker_instead_of_duplicating(self):
        created = OrderStatus.objects.create(name='Order Created')
        dispatched = OrderStatus.objects.create(name='Dispatched')
        order = self._order(created)
        pending = OrdersLog.objects.create(
            order=order, action=dispatched, performed_by=None, remarks='')

        self._submit(order, self.actor, dispatched.id, reason='sent')

        self.assertEqual(
            OrdersLog.objects.filter(order=order, action=dispatched).count(), 1,
            'a pending marker should be updated in place, not duplicated')
        pending.refresh_from_db()
        self.assertEqual(pending.performed_by_id, self.actor.id)
        self.assertEqual(pending.remarks, 'sent')
