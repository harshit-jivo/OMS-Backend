"""PRDO notification integration — the contract in `NOTIFICATION_INTEGRATION.md`.

WHAT THIS SUITE IS FOR
----------------------
The notification framework is generic: it knows nothing about production
orders, and PRDO owns its events, its recipients and its transaction
boundaries. That split only holds if somebody checks it, because every way of
breaking it is silent — a notification sent to the wrong person, or not sent at
all, looks exactly like one that was never expected.

PRDO also differs from BackDate in the one place it is easiest to get wrong:
there is NO REQUESTER. SAP raised the order, so `sap_created_by` is a string
snapshot of a SAP login and there is nobody to tell when a decision lands.
The outcome therefore goes to whoever acted EARLIER in the flow, minus the
person who just acted — which, on the single-stage configuration that ships
today, is nobody. Several tests below exist only to pin that down, because
"notifies nobody" is indistinguishable from "notification is broken" unless it
is asserted deliberately.
"""
import datetime
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification
from workflow.models import Workflow, WorkflowModule, WorkflowQuery, WorkflowStage
from workflow.services import conditions

from production.models import (
    FlowStatus,
    LogAction,
    ProductionOrder,
    ProductionOrderActionLog,
)
from production.services import flow as flow_service
from production.services import notify as notify_service

User = get_user_model()

DOC_TABLE = 'production.production_order'


def make_user(username):
    return User.objects.create_user(
        username=username, password='x', email=f'{username}@example.com')


class _Base(TestCase):
    """One workflow, two stages — so the multi-stage rules are reachable."""

    @classmethod
    def setUpTestData(cls):
        cls.approver1 = make_user('prdo-notif-approver1')
        cls.approver2 = make_user('prdo-notif-approver2')

        cls.module, _ = WorkflowModule.objects.get_or_create(
            code='PRDO', defaults={'name': 'Production Order'})
        cls.workflow = Workflow.objects.create(
            module=cls.module, code='PRDO_NOTIF', name='notif', company='OIL')
        query = WorkflowQuery.objects.create(
            workflow=cls.workflow, name='oil', company='OIL',
            query_text=f"SELECT id FROM {DOC_TABLE} WHERE company = 'OIL'")
        conditions.validate_and_stamp(query)
        cls.stage1 = WorkflowStage.objects.create(
            workflow=cls.workflow, name='Production Head', sequence=1,
            user=cls.approver1)
        cls.stage2 = WorkflowStage.objects.create(
            workflow=cls.workflow, name='Plant Head', sequence=2,
            user=cls.approver2)

    def _order(self, doc_entry=5001):
        return ProductionOrder.objects.create(
            company='OIL',
            sap_doc_entry=doc_entry,
            sap_doc_num=doc_entry,
            item_code='FG0001',
            item_name='Jivo Canola 1L',
            warehouse='BH-FG',
            planned_qty=Decimal('2400'),
            order_type='S',
            post_date=datetime.date(2026, 9, 1),
            sap_status='P',
            synced_at=timezone.now(),
        )

    def _synced(self, doc_entry=5001):
        """An order the sync has just found and routed — stage 1 is waiting."""
        order = self._order(doc_entry)
        return order, flow_service.open_flow(order)


class SyncNotificationTests(_Base):
    """Finding an order tells the person who now has to decide it."""

    def test_sync_notifies_the_first_stages_approver(self):
        order, _ = self._synced()
        sent = Notification.objects.filter(
            event_type=notify_service.PRDO_AWAITING_APPROVAL)
        self.assertEqual(sent.count(), 1)
        self.assertEqual(sent.first().user_id, self.approver1.pk)

    def test_the_second_approver_is_not_told_yet(self):
        """Stage 2 is not their problem until stage 1 has said yes."""
        self._synced()
        self.assertFalse(
            Notification.objects.filter(user=self.approver2).exists())

    def test_the_recipient_comes_from_the_ENGINE_not_a_stored_copy(self):
        """Reassign the stage; the next notification follows it.

        Nothing in `production` is updated by this — which is the whole point
        of holding a stage id rather than a user.
        """
        stand_in = make_user('prdo-notif-standin')
        self.stage1.user = stand_in
        self.stage1.save(update_fields=['user'])

        self._synced()
        sent = Notification.objects.get(
            event_type=notify_service.PRDO_AWAITING_APPROVAL)
        self.assertEqual(sent.user_id, stand_in.pk)

    def test_a_stage_with_nobody_assigned_notifies_nobody_and_does_not_raise(self):
        """A misconfigured stage is an operations problem, not a crash."""
        self.stage1.user = None
        self.stage1.save(update_fields=['user'])

        order, flow = self._synced()

        self.assertIsNotNone(flow.pk)
        self.assertFalse(Notification.objects.filter(
            event_type=notify_service.PRDO_AWAITING_APPROVAL).exists())


class AdvanceNotificationTests(_Base):
    """Each approval hands the order to the next person."""

    def test_approving_stage_1_notifies_stage_2(self):
        order, flow = self._synced()
        Notification.objects.all().delete()

        flow_service.approve(flow, user=self.approver1)

        sent = Notification.objects.filter(
            event_type=notify_service.PRDO_AWAITING_APPROVAL)
        self.assertEqual(sent.count(), 1)
        self.assertEqual(sent.first().user_id, self.approver2.pk)

    def test_the_earlier_approver_is_told_when_it_is_finally_approved(self):
        """Stage 1 signed this off and then heard nothing under JSAP."""
        order, flow = self._synced()
        flow_service.approve(flow, user=self.approver1)
        Notification.objects.all().delete()

        # No SAP mock: the write-back lives in the VIEW, after this
        # transition commits. `flow_service.approve` touches only OMS.
        flow_service.approve(flow, user=self.approver2)

        sent = Notification.objects.filter(
            event_type=notify_service.PRDO_APPROVED)
        self.assertEqual([n.user_id for n in sent], [self.approver1.pk])

    def test_the_earlier_approver_is_told_when_it_is_rejected(self):
        order, flow = self._synced()
        flow_service.approve(flow, user=self.approver1)
        Notification.objects.all().delete()

        flow_service.reject(flow, user=self.approver2, remarks='wrong batch')

        sent = Notification.objects.get(
            event_type=notify_service.PRDO_REJECTED)
        self.assertEqual(sent.user_id, self.approver1.pk)
        # The reason travels with it — being overruled without being told why
        # is the complaint this replaces.
        self.assertIn('wrong batch', sent.message)

    def test_the_decider_is_not_told_about_their_own_decision(self):
        """They just made it; an alert saying so is noise."""
        order, flow = self._synced()
        flow_service.approve(flow, user=self.approver1)
        Notification.objects.all().delete()

        flow_service.reject(flow, user=self.approver2, remarks='no')

        self.assertFalse(
            Notification.objects.filter(user=self.approver2).exists())


class NoRequesterTests(_Base):
    """PRDO has nobody to tell, and that must be asserted, not assumed."""

    def test_a_single_stage_approval_notifies_nobody(self):
        """The one live OIL configuration. Silence here is CORRECT.

        Nobody in OMS raised the order and nobody else acted on it, so there
        is no one left to inform. Asserting it stops a future change from
        quietly inventing a recipient — the SAP creator is a login string, not
        a user row, and mapping one to the other is the mistake the data model
        exists to prevent.
        """
        self.stage2.is_active = False
        self.stage2.save(update_fields=['is_active'])

        order, flow = self._synced()
        Notification.objects.all().delete()

        flow_service.approve(flow, user=self.approver1)

        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertFalse(Notification.objects.exists())

    def test_nothing_is_sent_for_an_order_retired_by_the_sync(self):
        """SAP moved on. Nobody refused anything and nothing is owed."""
        order, flow = self._synced()
        Notification.objects.all().delete()

        flow_service.retire(flow, reason='OWOR.Status moved to R')

        self.assertFalse(Notification.objects.exists())
        self.assertTrue(ProductionOrderActionLog.objects.filter(
            production_order=order, action=LogAction.OBSOLETE).exists())


class PayloadTests(_Base):
    """`entity=` is passed generically, so the clients can route on it."""

    def test_the_notification_points_at_the_order_generically(self):
        order, _ = self._synced()
        sent = Notification.objects.get(
            event_type=notify_service.PRDO_AWAITING_APPROVAL)
        self.assertEqual(sent.object_id, order.pk)
        self.assertEqual(
            sent.content_type, ContentType.objects.get_for_model(ProductionOrder))

    def test_the_canonical_payload_carries_entity_type_productionorder(self):
        """`productionorder` is the string both routers key on.

        If this ever changes, the apps' ENTITY_ROUTES entries stop matching
        and a tapped push lands in the inbox instead of the order — silently.
        """
        from notifications.services.payloads import build_payload

        order, _ = self._synced()
        payload = build_payload(Notification.objects.get(
            event_type=notify_service.PRDO_AWAITING_APPROVAL))
        self.assertEqual(payload['entity_type'], 'productionorder')
        self.assertEqual(payload['entity_id'], order.pk)
        self.assertEqual(payload['event_type'],
                         notify_service.PRDO_AWAITING_APPROVAL)

    def test_the_mobile_provider_sends_that_payload(self):
        from notifications.providers import mobile

        with mock.patch.object(mobile.MobileProvider, 'get_tokens',
                               return_value=['ExponentPushToken[abc]']), \
                mock.patch.object(mobile, 'requests') as requests:
            requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                order, _ = self._synced()

        data = requests.post.call_args.kwargs['json'][0]['data']
        self.assertEqual(data['entity_type'], 'productionorder')
        self.assertEqual(data['entity_id'], order.pk)

    def test_the_web_provider_sends_the_same_payload(self):
        from notifications.providers import web

        subscription = {'endpoint': 'https://push.example/x',
                        'keys': {'p256dh': 'a', 'auth': 'b'}}
        with mock.patch.object(web.WebProvider, 'get_subscriptions',
                               return_value=[subscription]), \
                mock.patch('pywebpush.webpush') as webpush:
            with self.captureOnCommitCallbacks(execute=True):
                order, _ = self._synced()

        data = json.loads(webpush.call_args.kwargs['data'])
        self.assertEqual(data['entity_type'], 'productionorder')
        self.assertEqual(data['entity_id'], order.pk)

    def test_the_serializer_labels_it_as_the_production_module(self):
        """Otherwise it groups under "other" in both inboxes."""
        from notifications.serializers import NotificationSerializer

        self._synced()
        row = NotificationSerializer(Notification.objects.get(
            event_type=notify_service.PRDO_AWAITING_APPROVAL)).data
        self.assertEqual(row['module'], 'production')


class IsolationTests(_Base):
    """A notification must never be the reason a decision fails or sticks."""

    def test_a_rolled_back_sync_notifies_nobody(self):
        """The order and the routing commit together, or neither does."""
        from django.db import transaction

        class Rollback(Exception):
            pass

        try:
            with transaction.atomic():
                self._synced()
                raise Rollback()
        except Rollback:
            pass

        self.assertFalse(Notification.objects.exists())
        self.assertFalse(ProductionOrder.objects.exists())

    def test_a_failing_provider_does_not_fail_the_approval(self):
        """Delivery is a side effect. The decision is the business outcome."""
        from notifications.providers import mobile

        order, flow = self._synced()

        with mock.patch.object(mobile.MobileProvider, 'get_tokens',
                               side_effect=RuntimeError('push is down')):
            with self.captureOnCommitCallbacks(execute=True):
                flow_service.approve(flow, user=self.approver1)

        flow.refresh_from_db()
        self.assertEqual(flow.current_stage_id, self.stage2.id)
