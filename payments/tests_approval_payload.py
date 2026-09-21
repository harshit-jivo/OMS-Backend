"""What the clients are told about a document's approval.

The apps draw the approval ladder, the current approver and the action buttons
from this payload, so what it says has to be what the server will actually
enforce. Three things matter here and each has its own reason:

  * `document_kind` — receipts and deposits are separate business workflows,
    and a client that inferred the difference from a number prefix would get it
    wrong the first time a number was typed by hand.
  * `stages` — the ladder, with who decided each rung (from the append-only
    history) and who must act on the open one (resolved live). Detail responses
    only: a list would pay for it once per row to show something no row shows.
  * `can_retry_sap` — whether the action to offer is a SAP retry rather than a
    first approval. Server-decided, because a client testing
    `status == PENDING_ERROR` would also offer it after a SAP TIMEOUT, where a
    second post can pay the same money twice.

PostgreSQL only.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from . import workflow_flow
from .models import BankDeposit, PaymentMethodEntry, PaymentReceipt
from .permissions import (DEPOSIT_APPROVE, DEPOSIT_CREATE, PAYMENTS_APPROVE,
                          PAYMENTS_CREATE)
from .sap_client import SapError
from .tests_support import uniq
from .tests_workflow_fixtures import payments_workflow

SAP_OK = {'DocEntry': 5150, 'DocNum': 771122}
SAP_REFUSED = SapError('Posting period locked', status_code=400,
                       sap_code='-4013')
SAP_SILENT = SapError('Connection timed out', status_code=None)


def _user(prefix, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(username=uniq(prefix), name=prefix.title(),
                               role=role, extra_pages=list(keys))


class _Base(TestCase):
    def setUp(self):
        super().setUp()
        for target, value in (
            ('payments.services._bank_accounts_for', {'CASH': '100001'}),
            ('payments.hana_queries.fetch_incoming_payment_series', 1),
            ('payments.services.resolve_bpl_id', 1),
            ('payments.services.resolve_company_db', 'TESTDB'),
            ('payments.hana_queries.fetch_payment_trans_id', None),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.creator = _user('ap_creator-', [PAYMENTS_CREATE, DEPOSIT_CREATE])
        self.first = _user('ap_first-', [PAYMENTS_APPROVE])
        self.final = _user('ap_final-', [PAYMENTS_APPROVE])
        self.client = APIClient()

    def _receipt(self):
        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-AP-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        return receipt

    def _detail(self, receipt, as_user):
        self.client.force_authenticate(as_user)
        resp = self.client.get(
            reverse('payment-receipt-detail', args=[receipt.pk]))
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data['data']


class ApprovalPayloadTests(_Base):
    def setUp(self):
        super().setUp()
        self.workflow, self.stages = payments_workflow(
            self.first, self.final, company='OIL', code='PAY_AP',
            documents='receipts')

    def test_an_unsubmitted_document_has_no_approval(self):
        data = self._detail(self._receipt(), self.creator)
        self.assertIsNone(data['approval'])

    def test_the_payload_names_the_business_workflow(self):
        receipt = self._receipt()
        workflow_flow.start(receipt, user=self.creator)

        data = self._detail(receipt, self.creator)
        self.assertEqual(data['approval']['document_kind'], 'RECEIPT')

    def test_the_ladder_reports_every_stage_in_order(self):
        receipt = self._receipt()
        workflow_flow.start(receipt, user=self.creator)

        stages = self._detail(receipt, self.creator)['approval']['stages']
        self.assertEqual([s['sequence'] for s in stages], [1, 2])
        self.assertEqual([s['state'] for s in stages], ['CURRENT', 'PENDING'])

    def test_the_open_rung_names_who_must_act_today(self):
        receipt = self._receipt()
        workflow_flow.start(receipt, user=self.creator)

        approval = self._detail(receipt, self.creator)['approval']
        self.assertEqual(approval['current_approver'], self.first.username)
        self.assertEqual(approval['current_approver_name'], self.first.name)
        self.assertEqual(approval['stages'][0]['approver'],
                         self.first.username)

    def test_a_decided_rung_names_who_decided_it(self):
        receipt = self._receipt()
        flow = workflow_flow.start(receipt, user=self.creator)
        workflow_flow.approve(flow, user=self.first)

        stages = self._detail(receipt, self.creator)['approval']['stages']
        self.assertEqual(stages[0]['state'], 'APPROVED')
        self.assertEqual(stages[0]['decided_by'], self.first.username)
        self.assertIsNotNone(stages[0]['decided_at'])
        # ...and the ladder has moved on.
        self.assertEqual(stages[1]['state'], 'CURRENT')

    def test_a_reassignment_moves_the_pending_rung_with_no_write(self):
        """The waiting approver is resolved live, not stored on the flow."""
        receipt = self._receipt()
        workflow_flow.start(receipt, user=self.creator)
        stand_in = _user('ap_standin-', [PAYMENTS_APPROVE])

        stage = self.stages[0]
        stage.user = stand_in
        stage.save(update_fields=['user'])

        approval = self._detail(receipt, self.creator)['approval']
        self.assertEqual(approval['current_approver'], stand_in.username)

    def test_the_list_does_not_carry_the_ladder(self):
        """It costs a query per row to show something no row displays."""
        receipt = self._receipt()
        workflow_flow.start(receipt, user=self.creator)

        self.client.force_authenticate(self.creator)
        resp = self.client.get(reverse('payment-receipt-list') + '?mine=true')
        rows = [r for r in resp.data['data']['results']
                if r['id'] == receipt.pk]
        self.assertEqual(len(rows), 1)
        self.assertNotIn('stages', rows[0]['approval'])
        # The label a row DOES need is still there.
        self.assertTrue(rows[0]['approval']['stage_label'])


class RetryFlagTests(_Base):
    """`can_retry_sap` — offered only when a retry is genuinely safe."""

    def setUp(self):
        super().setUp()
        self.workflow, self.stages = payments_workflow(
            self.final, company='OIL', code='PAY_AP_RETRY',
            documents='receipts')

    def _final_approve(self, sap):
        receipt = self._receipt()
        flow = workflow_flow.start(receipt, user=self.creator)
        target = 'payments.sap_poster.sap_post_payment'
        with patch(target) as mock:
            if isinstance(sap, Exception):
                mock.side_effect = sap
            else:
                mock.return_value = sap
            flow_ = flow
            workflow_flow.approve(flow, user=self.final)
            workflow_flow.settle_after_approval(
                workflow_flow.document_of(flow_), user=self.final)
        receipt.refresh_from_db()
        return receipt

    def test_no_retry_is_offered_before_anything_was_tried(self):
        receipt = self._receipt()
        workflow_flow.start(receipt, user=self.creator)

        perms = self._detail(receipt, self.final)['permissions']
        self.assertTrue(perms['can_decide'])
        self.assertFalse(perms['can_retry_sap'])

    def test_a_refused_posting_offers_a_retry_to_the_final_approver(self):
        receipt = self._final_approve(SAP_REFUSED)
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)

        perms = self._detail(receipt, self.final)['permissions']
        self.assertTrue(perms['can_decide'])
        self.assertTrue(perms['can_retry_sap'])

    def test_a_timed_out_posting_OFFERS_a_verified_retry(self):
        """THE DANGEROUS ONE — and it is now offered, safely.

        SAP may hold the document, so this used to offer nothing at all. That
        removed the duplicate risk by removing every way forward: a timed-out
        receipt had no approve, no reject and no retry, which is a permanently
        stuck document.

        The retry is offered now because it is no longer blind.
        `sap_settlement.settle` looks the document up in ORCT by the
        `OMS <receipt_no>` reference every payload carries, and either ADOPTS
        what SAP already has or posts knowing it is absent. If SAP cannot be
        reached the lookup raises and nothing is posted — the retry simply
        stays available.
        """
        receipt = self._final_approve(SAP_SILENT)
        self.assertEqual(receipt.status, PaymentReceipt.Status.SAP_UNKNOWN)

        perms = self._detail(receipt, self.final)['permissions']
        self.assertTrue(perms['can_decide'])
        self.assertTrue(perms['can_retry_sap'])

    def test_a_posted_document_offers_no_retry(self):
        receipt = self._final_approve(SAP_OK)
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)

        perms = self._detail(receipt, self.final)['permissions']
        self.assertFalse(perms['can_decide'])
        self.assertFalse(perms['can_retry_sap'])

    def test_no_retry_is_offered_to_anyone_else(self):
        receipt = self._final_approve(SAP_REFUSED)
        other = _user('ap_other-', [PAYMENTS_APPROVE])

        perms = self._detail(receipt, other)['permissions']
        self.assertFalse(perms['can_decide'])
        self.assertFalse(perms['can_retry_sap'])

    def test_no_retry_is_offered_to_the_raiser(self):
        """Nobody approves their own money, retry included."""
        receipt = self._final_approve(SAP_REFUSED)

        perms = self._detail(receipt, self.creator)['permissions']
        self.assertFalse(perms['can_retry_sap'])


class DepositPayloadTests(_Base):
    """The deposit half — its own workflow, its own ladder."""

    def setUp(self):
        super().setUp()
        self.dep_approver = _user('ap_dep-', [DEPOSIT_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.dep_approver, company='OIL', code='DEP_AP',
            documents='deposits')

    def _deposit(self):
        return BankDeposit.objects.create(
            deposit_no=uniq('DEP-AP-'), company='OIL',
            deposit_date=date.today(), collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'), bank_key='INB:2201101',
            bank_code='INB', bank_gl_account='2201101',
            bank_display_name='INDIAN BANK', source_gl_account='1105001',
            status=BankDeposit.Status.DRAFT, created_by=self.creator)

    def test_a_deposit_reports_itself_as_a_deposit(self):
        deposit = self._deposit()
        workflow_flow.start(deposit, user=self.creator)

        self.client.force_authenticate(self.creator)
        resp = self.client.get(
            reverse('bank-deposit-detail', args=[deposit.pk]))
        approval = resp.data['data']['approval']

        self.assertEqual(approval['document_kind'], 'DEPOSIT')
        self.assertEqual(approval['current_approver'],
                         self.dep_approver.username)

    def test_a_receipt_approver_is_not_offered_a_deposit_decision(self):
        deposit = self._deposit()
        workflow_flow.start(deposit, user=self.creator)

        self.client.force_authenticate(self.first)   # holds Payments_Approve
        resp = self.client.get(
            reverse('bank-deposit-detail', args=[deposit.pk]))
        perms = resp.data['data']['permissions']

        self.assertFalse(perms['can_decide'])
        self.assertFalse(perms['can_retry_sap'])
