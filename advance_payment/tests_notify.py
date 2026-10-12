"""Who is told what as a payment request moves (`services/notify.py`)."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from advance_payment.models import AdvanceRequest, Department, FlowStatus, RequestFlow, RequestStatus
from advance_payment.services import flow as flow_service
from advance_payment.services import notify
from notifications.models import Notification
from workflow.models import WorkflowModule
from workflow.tests.factories import make_stage, make_user, make_workflow


@mock.patch('notifications.services.dispatcher._deliver')
class AsTheRequestMoves(TestCase):
    def setUp(self):
        self.creator = make_user('ap-n-creator')
        self.hod = make_user('ap-n-hod')
        self.payer = make_user('ap-n-payer')
        self.auditor = make_user('ap-n-auditor')
        self.final = make_user('ap-n-final')
        module, _ = WorkflowModule.objects.get_or_create(
            code=flow_service.MODULE_CODE, defaults={'name': 'Advance Payment'})
        workflow = make_workflow(module, code='AP_NOTIFY')
        self.hod_stage = make_stage(workflow, 1, self.hod, name='HOD Approval')
        self.payment_stage = make_stage(workflow, 2, self.payer, name='Payment Approval')
        self.audit_stage = make_stage(workflow, 3, self.auditor, name='Audit Approval')
        make_stage(workflow, 4, self.final, name='Final Approval')
        self.advance = AdvanceRequest.objects.create(
            request_no='AP-TEST-0901', company='OIL', request_type='VENDOR', payment_against='ADVANCE',
            department=Department.objects.create(name='Finance'), partner_code='V001',
            partner_name='A Vendor', amount=Decimal('25000'), payment_date=date(2026, 10, 12),
            created_by=self.creator, status=RequestStatus.IN_APPROVAL)
        RequestFlow.objects.create(request=self.advance, workflow=workflow, status=FlowStatus.PENDING,
                                   current_stage=self.hod_stage, current_role='APPROVAL',
                                   current_user=self.hod, total_stages=4)

    def at(self, stage, role):
        RequestFlow.objects.filter(request=self.advance).update(
            current_stage=stage, current_role=role, current_user=stage.user)

    def told(self, user):
        return list(Notification.objects.filter(user=user).values_list('event_type', 'title', 'message'))

    def test_an_approval_tells_the_next_desk_and_nobody_else(self, _deliver):
        with self.captureOnCommitCallbacks(execute=True):
            flow_service.approve(self.advance, user=self.hod)
        [(event, title, message)] = self.told(self.payer)
        self.assertEqual((event, title), (notify.AWAITING, 'Payment request awaiting you'))
        self.assertIn('AP-TEST-0901 · A Vendor · ₹25,000 (OIL) · Payment Approval', message)
        self.assertEqual(self.told(self.hod), [])
        self.assertEqual(self.told(self.creator), [])
        note = Notification.objects.get(user=self.payer)
        self.assertEqual((note.content_type.model, note.object_id), ('advancerequest', self.advance.pk))

    def test_a_rejection_tells_the_creator_why(self, _deliver):
        with self.captureOnCommitCallbacks(execute=True):
            flow_service.reject(self.advance, user=self.hod, remarks='No budget left')
        [(event, _title, message)] = self.told(self.creator)
        self.assertEqual(event, notify.REJECTED)
        self.assertIn('Reason: No budget left', message)

    def test_a_return_tells_the_creator_to_edit_and_resubmit(self, _deliver):
        with self.captureOnCommitCallbacks(execute=True):
            flow_service.return_to_creator(self.advance, user=self.hod, remarks='Attach the PO')
        [(event, _title, message)] = self.told(self.creator)
        self.assertEqual(event, notify.RETURNED)
        self.assertIn('Attach the PO', message)

    def test_sent_back_tells_payment_who_sent_it_and_why(self, _deliver):
        self.at(self.audit_stage, 'AUDIT')
        with self.captureOnCommitCallbacks(execute=True):
            flow_service.send_back(self.advance, user=self.auditor, remarks='Wrong IFSC')
        [(event, _title, message)] = self.told(self.payer)
        self.assertEqual(event, notify.AWAITING)
        self.assertIn('Sent back by Audit Approval: Wrong IFSC', message)

    def test_a_refused_action_tells_nobody(self, _deliver):
        with self.captureOnCommitCallbacks(execute=True), self.assertRaises(flow_service.FlowError):
            flow_service.approve(self.advance, user=self.payer)  # not their stage
        self.assertFalse(Notification.objects.exists())

    def test_a_failed_notification_never_fails_the_decision(self, _deliver):
        with mock.patch.object(notify, 'notify', side_effect=RuntimeError('push down')), \
                self.captureOnCommitCallbacks(execute=True):
            advance, error = flow_service.approve(self.advance, user=self.hod)
        self.assertEqual(error, '')
        self.assertEqual(RequestFlow.objects.get(request=advance).current_stage, self.payment_stage)


class BillsSentToRaiseFrom(SimpleTestCase):
    def test_one_notification_per_send(self):
        me, them = SimpleNamespace(pk=1), SimpleNamespace(pk=2)
        made = [SimpleNamespace(company='OIL', sap_doc_num=n, card_name='ARNAV', assigned_to=them,
                                get_kind_display=lambda: 'Bill') for n in ('826880024', '826880025')]
        with mock.patch.object(notify.transaction, 'on_commit', side_effect=lambda fn: fn()), \
                mock.patch.object(notify, 'notify') as sent:
            notify.assigned(made, actor=me)
        sent.assert_called_once()
        kwargs = sent.call_args.kwargs
        self.assertEqual((kwargs['event_type'], kwargs['recipients'], kwargs['title']),
                         (notify.ASSIGNED, [them], '2 bills / POs were sent to you'))
        self.assertIn('Bill 826880024, Bill 826880025 · ARNAV', kwargs['message'])

    def test_nobody_is_told_about_their_own_action(self):
        me = SimpleNamespace(pk=1)
        made = [SimpleNamespace(company='OIL', sap_doc_num='1', card_name='X', assigned_to=me,
                                get_kind_display=lambda: 'PO')]
        with mock.patch.object(notify.transaction, 'on_commit') as later:
            notify.assigned(made, actor=me)
        later.assert_not_called()
