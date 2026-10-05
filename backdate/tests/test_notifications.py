"""BackDate notification integration — the contract in `NOTIFICATION_INTEGRATION.md`.

WHAT THIS SUITE IS FOR
----------------------
The notification framework is generic: it knows nothing about BackDate, and
BackDate owns its events, its recipients and its transaction boundaries. That
split only holds if somebody checks it, because every way of breaking it is
silent — a notification sent to the wrong person, or not sent at all, looks
exactly like one that was never expected.

So these tests assert the module's half of the contract (§T):

* events are owned here, not by the framework
* recipients come from the ENGINE's current stage assignment, never a stored
  copy — the reason a reassignment or a stand-in re-routes with no code here
* `entity=` is passed generically, so `entity_type` / `entity_id` reach the
  clients and the mobile deep link resolves
* a rolled-back decision notifies NOBODY
* a provider that throws never fails the approval
"""
import datetime
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification
from workflow.models import Workflow, WorkflowModule, WorkflowQuery, WorkflowStage
from workflow.services import conditions

from backdate.models import BackDate
from backdate.services import flow as flow_service
from backdate.services import notify as notify_service

User = get_user_model()

DOC_TABLE = 'backdate.backdate'
TIME_LIMIT = timezone.now() + datetime.timedelta(days=30)


def make_user(username):
    return User.objects.create_user(
        username=username, password='x', email=f'{username}@example.com')


class _Base(TestCase):
    """One workflow, two stages — the shape a real BKDT template has."""

    @classmethod
    def setUpTestData(cls):
        cls.requester = make_user('bkdt-notif-requester')
        cls.approver1 = make_user('bkdt-notif-approver1')
        cls.approver2 = make_user('bkdt-notif-approver2')

        cls.module, _ = WorkflowModule.objects.get_or_create(
            code='BKDT', defaults={'name': 'BackDate'})
        cls.workflow = Workflow.objects.create(
            module=cls.module, code='BKDT_NOTIF', name='notif', company='OIL')
        query = WorkflowQuery.objects.create(
            workflow=cls.workflow, name='oil', company='OIL',
            query_text=f"SELECT * FROM {DOC_TABLE} WHERE company = 'OIL'")
        conditions.validate_and_stamp(query)
        cls.stage1 = WorkflowStage.objects.create(
            workflow=cls.workflow, name='Manager', sequence=1,
            user=cls.approver1)
        cls.stage2 = WorkflowStage.objects.create(
            workflow=cls.workflow, name='Finance', sequence=2,
            user=cls.approver2)

    def _request(self):
        return BackDate.objects.create(
            company='OIL', sap_username='USER12',
            document_type_name='A/R Invoice',
            from_date=datetime.date(2026, 5, 1),
            to_date=datetime.date(2026, 5, 20),
            time_limit=TIME_LIMIT, action='A', created_by=self.requester)

    def _submit(self):
        request = self._request()
        return request, flow_service.submit(request, user=self.requester)


class SubmissionNotificationTests(_Base):
    """Submitting tells the person who now has to act — and only them."""

    def test_submission_notifies_the_first_stages_approver(self):
        request, _ = self._submit()
        sent = Notification.objects.filter(
            event_type=notify_service.BACKDATE_AWAITING_APPROVAL)
        self.assertEqual(sent.count(), 1)
        self.assertEqual(sent.first().user_id, self.approver1.pk)

    def test_the_requester_is_not_told_about_their_own_request(self):
        """They just raised it; an alert saying so is noise."""
        self._submit()
        self.assertFalse(
            Notification.objects.filter(user=self.requester).exists())

    def test_the_second_approver_is_not_told_yet(self):
        """Stage 2 is not their problem until stage 1 has said yes."""
        self._submit()
        self.assertFalse(
            Notification.objects.filter(user=self.approver2).exists())

    def test_the_recipient_comes_from_the_ENGINE_not_a_stored_copy(self):
        """Reassign the stage; the next notification follows it.

        Nothing in `backdate` is updated by this — which is the whole point of
        holding a stage id rather than a user.
        """
        stand_in = make_user('bkdt-notif-standin')
        self.stage1.user = stand_in
        self.stage1.save(update_fields=['user'])

        self._submit()
        sent = Notification.objects.get(
            event_type=notify_service.BACKDATE_AWAITING_APPROVAL)
        self.assertEqual(sent.user_id, stand_in.pk)


class AdvanceNotificationTests(_Base):
    """Each approval hands the request to the next person."""

    def test_approving_stage_1_notifies_stage_2(self):
        request, flow = self._submit()
        Notification.objects.all().delete()

        flow_service.approve(flow, user=self.approver1)

        sent = Notification.objects.filter(
            event_type=notify_service.BACKDATE_AWAITING_APPROVAL)
        self.assertEqual(sent.count(), 1)
        self.assertEqual(sent.first().user_id, self.approver2.pk)

    def test_the_requester_is_told_when_it_is_finally_approved(self):
        request, flow = self._submit()
        flow_service.approve(flow, user=self.approver1)
        Notification.objects.all().delete()

        with mock.patch('backdate.services.hana.apply_grant'):
            flow_service.approve(flow, user=self.approver2)

        sent = Notification.objects.get(
            event_type=notify_service.BACKDATE_APPROVED)
        self.assertEqual(sent.user_id, self.requester.pk)

    def test_the_requester_is_told_when_it_is_rejected(self):
        """JSAP sent nothing at all here — a requester was never told."""
        request, flow = self._submit()
        Notification.objects.all().delete()

        flow_service.reject(flow, user=self.approver1, remarks='too far back')

        sent = Notification.objects.get(
            event_type=notify_service.BACKDATE_REJECTED)
        self.assertEqual(sent.user_id, self.requester.pk)
        # The reason travels with it — being refused without being told why is
        # the complaint this replaces.
        self.assertIn('too far back', sent.message)


class PayloadTests(_Base):
    """`entity=` is passed generically, so the clients can route on it."""

    def test_the_notification_points_at_the_request_generically(self):
        request, _ = self._submit()
        sent = Notification.objects.get(
            event_type=notify_service.BACKDATE_AWAITING_APPROVAL)
        self.assertEqual(sent.object_id, request.pk)
        self.assertEqual(
            sent.content_type, ContentType.objects.get_for_model(BackDate))

    def test_the_canonical_payload_carries_entity_type_backdate(self):
        """`backdate` is the string the mobile router keys its route on.

        If this ever changes, the app's ENTITY_ROUTES entry stops matching and
        a tapped push lands in the inbox instead of the request — silently.
        """
        from notifications.services.payloads import build_payload

        request, _ = self._submit()
        payload = build_payload(Notification.objects.get(
            event_type=notify_service.BACKDATE_AWAITING_APPROVAL))
        self.assertEqual(payload['entity_type'], 'backdate')
        self.assertEqual(payload['entity_id'], request.pk)
        self.assertEqual(payload['event_type'],
                         notify_service.BACKDATE_AWAITING_APPROVAL)

    def test_the_mobile_provider_sends_that_payload(self):
        from notifications.providers import mobile

        with mock.patch.object(mobile.MobileProvider, 'get_tokens',
                               return_value=['ExponentPushToken[abc]']), \
                mock.patch.object(mobile, 'requests') as requests:
            requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                request, _ = self._submit()

        data = requests.post.call_args.kwargs['json'][0]['data']
        self.assertEqual(data['entity_type'], 'backdate')
        self.assertEqual(data['entity_id'], request.pk)

    def test_the_web_provider_sends_the_same_payload(self):
        from notifications.providers import web

        subscription = {'endpoint': 'https://push.example/x',
                        'keys': {'p256dh': 'a', 'auth': 'b'}}
        with mock.patch.object(web.WebProvider, 'get_subscriptions',
                               return_value=[subscription]), \
                mock.patch('pywebpush.webpush') as webpush:
            with self.captureOnCommitCallbacks(execute=True):
                request, _ = self._submit()

        data = json.loads(webpush.call_args.kwargs['data'])
        self.assertEqual(data['entity_type'], 'backdate')
        self.assertEqual(data['entity_id'], request.pk)


class IsolationTests(_Base):
    """A notification must never be the reason a decision fails or sticks."""

    def test_a_rolled_back_submission_notifies_nobody(self):
        """The record and the routing commit together, or neither does."""
        from django.db import transaction

        class Rollback(Exception):
            pass

        try:
            with transaction.atomic():
                self._submit()
                raise Rollback()
        except Rollback:
            pass

        self.assertFalse(Notification.objects.exists())
        self.assertFalse(BackDate.objects.exists())

    def test_a_failing_provider_does_not_fail_the_approval(self):
        """Delivery is a side effect. The grant is the business outcome."""
        from notifications.providers import mobile

        request, flow = self._submit()
        flow_service.approve(flow, user=self.approver1)

        with mock.patch.object(mobile.MobileProvider, 'get_tokens',
                               side_effect=RuntimeError('push is down')), \
                mock.patch('backdate.services.hana.apply_grant'):
            with self.captureOnCommitCallbacks(execute=True):
                flow_service.approve(flow, user=self.approver2)

        flow.refresh_from_db()
        self.assertEqual(flow.status, 'APPROVED')

    def test_a_stage_with_nobody_assigned_notifies_nobody_and_does_not_raise(self):
        """A misconfigured stage is an operations problem, not a crash."""
        self.stage1.user = None
        self.stage1.save(update_fields=['user'])

        request, flow = self._submit()

        self.assertIsNotNone(flow.pk)
        self.assertFalse(Notification.objects.filter(
            event_type=notify_service.BACKDATE_AWAITING_APPROVAL).exists())
