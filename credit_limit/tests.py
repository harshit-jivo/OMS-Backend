"""Credit Limit — the rules worth holding down.

* the customer facts come from SAP, not the client;
* a request no workflow matches is not stored (JSAP left 30 such rows);
* approval needs the key AND the current stage;
* the final approval writes `CreditLimit` to SAP FIRST — a refusal leaves the
  request pending, with SAP's answer recorded;
* rejection is final;
* the invoice-review entry point raises an OMS request for the invoice's own
  customer, once.

Query matching is stubbed (`selection.evaluate`): condition SQL needs Postgres
and is covered by the workflow engine's own tests. Everything around it — the
real workflow, stage and replacement rows — runs for real.
"""
import datetime
import json
import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.companies import OIL
from invoice.models import InvoiceLog
from payments import sap_client
from workflow.models import Workflow, WorkflowModule, WorkflowStage
from workflow.services import selection

from credit_limit.models import (
    CreditLimitActionLog,
    CreditLimitFlow,
    CreditLimitRequest,
    FlowStatus,
    LogAction,
)

User = get_user_model()
MEDIA = tempfile.mkdtemp()

CARD = {'card_code': 'CUSTA000001', 'card_name': 'Real Name From SAP',
        'card_type': 'C', 'main_group': 'GT', 'balance': Decimal('5000'),
        'credit_limit': Decimal('10000')}


def make_user(username, *keys):
    user = User.objects.create_user(username=username, password='x-12345678')
    user.extra_pages = list(keys)
    user.save(update_fields=['extra_pages'])
    return user


def upload():
    return SimpleUploadedFile('proof.pdf', b'%PDF-1.4', 'application/pdf')


@override_settings(MEDIA_ROOT=MEDIA)
class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        module, _ = WorkflowModule.objects.get_or_create(
            code='CREDIT_LIMIT', defaults={'name': 'Credit Limit'})
        cls.requester = make_user('cl-req', 'Credit_Limit')
        cls.approver1 = make_user('cl-a1', 'Credit_Limit_Approval')
        cls.approver2 = make_user('cl-a2', 'Credit_Limit_Approval')
        cls.outsider = make_user('cl-out', 'Credit_Limit_Approval')
        cls.workflow = Workflow.objects.create(
            module=module, code='CL_OIL', name='Oil', company=OIL)
        WorkflowStage.objects.create(workflow=cls.workflow, name='Head',
                                     sequence=1, user=cls.approver1)
        WorkflowStage.objects.create(workflow=cls.workflow, name='Director',
                                     sequence=2, user=cls.approver2)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.matched = [mock.Mock(workflow_id=self.workflow.pk,
                                  workflow=self.workflow)]
        for target, kwargs in (
                (mock.patch.object(selection, 'evaluate'),
                 {'side_effect': lambda *a, **k: self.matched}),
                (mock.patch('credit_limit.services.sap.customer'),
                 {'return_value': dict(CARD)}),
                (mock.patch('payments.sap_company.resolve_company_db'),
                 {'return_value': 'TEST_OIL'}),
                (mock.patch('credit_limit.services.sap._commitment_limit'),
                 {'return_value': Decimal('10')}),
                (mock.patch('credit_limit.services.notify.notify'), {})):
            patched = target.start()
            for key, value in kwargs.items():
                setattr(patched, key, value)
            self.addCleanup(target.stop)
        self.sap_request = mock.patch.object(
            sap_client, 'request', return_value=(204, {})).start()
        self.addCleanup(mock.patch.stopall)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def raise_request(self, **overrides):
        data = {'company': OIL, 'card_code': 'CUSTA000001',
                'new_credit_limit': '25000',
                'valid_till': (datetime.date.today()
                               + datetime.timedelta(days=30)).isoformat(),
                'attachment': upload(), **overrides}
        return self.client_for(self.requester).post(
            reverse('credit-limit-request-list'), data, format='multipart')

    def act(self, user, request_id, action, remarks=''):
        return self.client_for(user).post(
            reverse(f'credit-limit-request-{action}', args=[request_id]),
            {'remarks': remarks}, format='json')


class SubmissionTests(_Base):
    def test_customer_facts_come_from_sap(self):
        response = self.raise_request(card_name='Forged', current_balance='1')
        self.assertEqual(response.status_code, 201, response.content)
        req = CreditLimitRequest.objects.get()
        self.assertEqual(req.card_name, 'Real Name From SAP')
        self.assertEqual(req.main_group, 'GT')
        self.assertEqual(req.current_credit_limit, Decimal('10000'))
        self.assertEqual(req.created_by, self.requester)
        self.assertEqual(req.flow.current_user, self.approver1)
        self.assertTrue(req.attachment)

    def test_attachment_is_optional(self):
        response = self.raise_request(attachment='')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(CreditLimitRequest.objects.get().attachment)

    def test_unroutable_request_is_not_stored(self):
        self.matched = []
        response = self.raise_request()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(CreditLimitRequest.objects.exists())

    def test_unknown_customer_is_refused(self):
        with mock.patch('credit_limit.services.sap.customer',
                        return_value=None):
            response = self.raise_request()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(CreditLimitRequest.objects.exists())

    def test_requester_key_is_required(self):
        response = self.client_for(self.approver1).post(
            reverse('credit-limit-request-list'), {}, format='multipart')
        self.assertEqual(response.status_code, 403)


class DecisionTests(_Base):
    def setUp(self):
        super().setUp()
        self.request_id = self.raise_request().json()['data']['id']

    def flow(self):
        return CreditLimitFlow.objects.get(request_id=self.request_id)

    def test_only_the_current_stage_user_may_act(self):
        self.assertEqual(self.act(self.outsider, self.request_id,
                                  'approve').status_code, 403)
        self.assertEqual(self.act(self.approver2, self.request_id,
                                  'approve').status_code, 403)
        self.assertEqual(self.act(self.requester, self.request_id,
                                  'approve').status_code, 403)

    def test_final_approval_writes_the_limit_to_sap(self):
        self.assertEqual(self.act(self.approver1, self.request_id,
                                  'approve').status_code, 200)
        self.sap_request.assert_not_called()
        self.assertEqual(self.flow().current_user, self.approver2)

        response = self.act(self.approver2, self.request_id, 'approve')
        self.assertEqual(response.status_code, 200, response.content)
        self.sap_request.assert_called_once_with(
            'PATCH', "/BusinessPartners('CUSTA000001')",
            company_db='TEST_OIL',
            json_body={'CreditLimit': '25000.00', 'MaxCommitment': '25000.00'})
        flow = self.flow()
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertIsNone(flow.current_stage)

    def test_a_higher_commitment_limit_is_never_lowered(self):
        self.act(self.approver1, self.request_id, 'approve')
        with mock.patch('credit_limit.services.sap._commitment_limit',
                        return_value=Decimal('900000')):
            self.act(self.approver2, self.request_id, 'approve')
        self.assertEqual(self.sap_request.call_args.kwargs['json_body'],
                         {'CreditLimit': '25000.00', 'MaxCommitment': '900000'})

    def test_sap_refusal_leaves_the_request_pending_with_the_reason(self):
        self.act(self.approver1, self.request_id, 'approve')
        self.sap_request.side_effect = sap_client.SapError(
            'Limit too high', status_code=400, payload={'error': 'nope'})

        response = self.act(self.approver2, self.request_id, 'approve')
        self.assertEqual(response.status_code, 502)
        flow = self.flow()
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertEqual(flow.current_user, self.approver2)
        self.assertIn('nope', flow.sap_response)
        # The rolled-back approval left no APPROVE row for stage 2.
        self.assertEqual(CreditLimitActionLog.objects.filter(
            action=LogAction.APPROVE).count(), 1)

    def test_rejection_is_final_and_needs_a_reason(self):
        self.assertEqual(self.act(self.approver1, self.request_id,
                                  'reject').status_code, 400)
        self.assertEqual(self.act(self.approver1, self.request_id, 'reject',
                                  'Over exposure').status_code, 200)
        self.assertEqual(self.flow().status, FlowStatus.REJECTED)
        self.assertEqual(self.act(self.approver2, self.request_id,
                                  'approve').status_code, 403)
        self.sap_request.assert_not_called()

    def test_queue_and_history_follow_the_stage(self):
        queue = reverse('credit-limit-approval-queue')
        ids = [r['id'] for r in
               self.client_for(self.approver1).get(queue).json()['data']]
        self.assertEqual(ids, [self.request_id])
        self.assertEqual(
            self.client_for(self.approver2).get(queue).json()['data'], [])

        self.act(self.approver1, self.request_id, 'approve')
        ids = [r['id'] for r in
               self.client_for(self.approver2).get(queue).json()['data']]
        self.assertEqual(ids, [self.request_id])
        history = self.client_for(self.approver1).get(
            reverse('credit-limit-approval-history')).json()['data']
        self.assertEqual([r['id'] for r in history], [self.request_id])

    def test_progress_lists_every_stage(self):
        self.act(self.approver1, self.request_id, 'approve')
        stages = self.client_for(self.requester).get(
            reverse('credit-limit-request-history', args=[self.request_id])
        ).json()['data']['stages']
        self.assertEqual([s['status'] for s in stages],
                         ['APPROVED', 'AWAITING'])


class InvoiceEntryPointTests(_Base):
    def setUp(self):
        super().setUp()
        self.log = InvoiceLog.objects.create(
            so_number='SO1', party_name='Party', total_amount=Decimal('1'),
            branch='OIL', status='ERROR', created_by=self.requester,
            invoice_payload={'CardCode': 'CUSTA000001'})

    def post(self, card_code='SOMEONE_ELSE'):
        return self.client_for(self.requester).post(
            reverse('credit-limit-request'),
            {'documentData': json.dumps({'customerCode': card_code,
                                         'newCreditLimit': 30000,
                                         'validTill': '2099-01-01 23:59:59'}),
             'attachment': upload(), 'invoice_log_id': self.log.pk},
            format='multipart')

    def test_raises_an_oms_request_for_the_invoice_customer_once(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)
        req = CreditLimitRequest.objects.get()
        self.assertEqual(req.card_code, 'CUSTA000001')
        self.assertEqual(req.invoice_log, self.log)
        self.assertEqual(self.post().status_code, 409)

        stages = self.client_for(self.requester).get(
            reverse('credit-limit-flow'), {'invoice_id': self.log.pk}
        ).json()['data']
        self.assertEqual([s['stageName'] for s in stages],
                         ['Head', 'Director'])
