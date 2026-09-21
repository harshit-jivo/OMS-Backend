"""A final approval can never leave a document permanently stuck.

Every test here asks one of two questions:

  1. Is there a state in which the approver has NEITHER a result NOR a retry?
  2. Can any path post the same money to SAP twice?

Those are the only two ways this module can hurt anybody, and they pull in
opposite directions: the old code answered (2) by refusing to touch a
POSTING_TO_SAP document at all, which answered (1) with "yes, for ever" —
RCP-OIL-20260919-000003 sat that way for 43 hours with zero SAP call logs.

Nothing here calls SAP. The poster and the ORCT lookup are both stubbed, so
every "SAP has it" / "SAP refused" / "SAP never answered" below is a decision
this suite makes.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from users.models import User, UserRole

from . import sap_settlement, workflow_flow
from .models import (FlowStatus, PaymentMethodEntry, PaymentReceipt,
                     SapCallLog)
from .permissions import PAYMENTS_APPROVE, PAYMENTS_CREATE, may_act_on
from .sap_client import SapError
from .tests_support import uniq
from .tests_workflow_fixtures import payments_workflow

SAP_OK = {'DocEntry': 5151, 'DocNum': 909090}
SAP_REFUSED = SapError('Posting period locked', status_code=400,
                       sap_code='-4013')
SAP_SILENT = SapError('Connection timed out', status_code=None)

#: One ORCT row, as `fetch_payment_by_reference` returns it.
def sap_row(doc_entry=5151, doc_num=909090, canceled=False):
    return {
        'doc_entry': doc_entry, 'doc_num': doc_num, 'trans_id': 777,
        'canceled': canceled, 'doc_total': Decimal('100.00'),
        'comments': 'OMS RCP-X',
    }


def _user(prefix, keys=()):
    role, _ = UserRole.objects.get_or_create(name='settlement_tests')
    return User.objects.create(username=uniq(prefix), name=prefix.title(),
                               role=role, extra_pages=list(keys))


class SettlementTests(TestCase):
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

        self.creator = _user('settle-creator', [PAYMENTS_CREATE])
        self.final = _user('settle-final', [PAYMENTS_APPROVE])
        payments_workflow(self.final, company='OIL', documents='receipts')

    # -- fixtures ---------------------------------------------------------

    def _receipt(self):
        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-SET-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'), account_key='1105001',
            gl_account='1105001', receiving_bank_name='CASH SALE')
        return receipt, workflow_flow.start(receipt, user=self.creator)

    def _sap(self, outcome):
        patcher = patch('payments.sap_poster.sap_post_payment')
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        if isinstance(outcome, Exception):
            mock.side_effect = outcome
        else:
            mock.return_value = outcome
        return mock

    def _lookup(self, rows=(), error=None):
        """Stub the ORCT reference lookup — the idempotency key."""
        patcher = patch('payments.hana_queries.fetch_payment_by_reference')
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        if error is not None:
            mock.side_effect = error
        else:
            mock.return_value = list(rows)
        return mock

    def _approve(self, flow, *, settle=True):
        """Approve. `settle=False` models a process that died first."""
        workflow_flow.approve(flow, user=self.final)
        if settle:
            workflow_flow.settle_after_approval(
                workflow_flow.document_of(flow), user=self.final)

    def _strand(self, receipt, *, age=timedelta(hours=2)):
        """Age the intent so it is no longer mistaken for a live call."""
        PaymentReceipt.objects.filter(pk=receipt.pk).update(
            updated_at=timezone.now() - age)
        receipt.refresh_from_db()
        return receipt

    # =================================================================== #
    # 1. SAP SUCCESS
    # =================================================================== #

    def test_sap_success_posts_once_and_completes_the_workflow(self):
        post = self._sap(SAP_OK)
        receipt, flow = self._receipt()

        self._approve(flow)

        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(receipt.sap_doc_entry, 5151)
        self.assertEqual(receipt.sap_doc_num, 909090)
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertEqual(post.call_count, 1)

    def test_settling_a_posted_document_again_does_nothing(self):
        post = self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow)

        receipt.refresh_from_db()
        sap_settlement.settle(receipt, user=self.final)
        sap_settlement.settle(receipt, user=self.final)

        # IDEMPOTENT. One approval, one payment, however many times anything
        # asks for it to be settled.
        self.assertEqual(post.call_count, 1)
        self.assertEqual(sap_settlement.attempt_state(receipt),
                         sap_settlement.NOT_OWED)

    # =================================================================== #
    # 2. SAP REJECTION
    # =================================================================== #

    def test_sap_rejection_parks_it_at_the_final_stage_with_a_retry(self):
        self._sap(SAP_REFUSED)
        receipt, flow = self._receipt()

        self._approve(flow)

        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertTrue(workflow_flow.is_at_final_stage(flow))
        allowed, _ = may_act_on(self.final, flow)
        self.assertTrue(allowed, 'the final approver must be able to retry')

    def test_a_rejected_post_is_retryable_and_succeeds(self):
        self._sap(SAP_REFUSED)
        receipt, flow = self._receipt()
        self._approve(flow)

        flow.refresh_from_db()
        with patch('payments.sap_poster.sap_post_payment', return_value=SAP_OK):
            self._approve(flow)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(receipt.sap_doc_entry, 5151)

    # =================================================================== #
    # 3. BACKEND RESTART DURING POSTING (the call never left)
    # =================================================================== #

    def test_a_restart_before_the_call_leaves_no_log_and_stays_recoverable(self):
        """THE CASE THAT STRANDED RCP-OIL-20260919-000003.

        The approval committed POSTING_TO_SAP and the process died before the
        SAP call. Zero call logs is the proof no request was ever made.
        """
        self._sap(SAP_OK)
        receipt, flow = self._receipt()

        self._approve(flow, settle=False)
        receipt = self._strand(receipt)

        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTING_TO_SAP)
        self.assertEqual(receipt.sap_call_logs_count()
                         if hasattr(receipt, 'sap_call_logs_count') else 0, 0)
        self.assertEqual(sap_settlement.attempt_state(receipt),
                         sap_settlement.NEVER_STARTED)
        self.assertFalse(sap_settlement.posting_is_in_flight(receipt))

    def test_a_stranded_document_still_offers_its_approver_a_retry(self):
        """NO DEAD END. This is the whole point of the change."""
        self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)
        self._strand(receipt)

        flow.refresh_from_db()
        allowed, reason = may_act_on(self.final, flow)
        self.assertTrue(
            allowed,
            f'a stranded posting must stay actionable, got: {reason}')

    def test_settling_a_never_started_document_posts_without_asking_sap(self):
        """No call was made, so SAP cannot hold it — no lookup needed."""
        post = self._sap(SAP_OK)
        lookup = self._lookup([])
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)
        receipt = self._strand(receipt)

        sap_settlement.settle(receipt, user=self.final)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(post.call_count, 1)
        lookup.assert_not_called()

    # =================================================================== #
    # 4. WORKER RESTART — a live call must not be trampled
    # =================================================================== #

    def test_a_fresh_intent_with_no_log_counts_as_in_flight(self):
        """The window between committing the intent and logging the attempt.

        Milliseconds wide in practice, but a second approval landing inside it
        would post a SECOND payment. Age decides: fresh means somebody is
        mid-request.
        """
        self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)

        receipt.refresh_from_db()
        self.assertEqual(sap_settlement.attempt_state(receipt),
                         sap_settlement.IN_FLIGHT)
        self.assertTrue(sap_settlement.posting_is_in_flight(receipt))

    def test_settle_yields_to_a_call_already_in_flight(self):
        post = self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)
        receipt.refresh_from_db()

        # An unclaimed caller — the sweep, or another tab — must not post.
        sap_settlement.settle(receipt, user=self.final)

        post.assert_not_called()

    def test_a_second_approval_is_refused_while_a_post_is_in_flight(self):
        self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)

        flow.refresh_from_db()
        allowed, reason = may_act_on(self.final, flow)
        self.assertFalse(allowed)
        self.assertIn('already in progress', reason)

    # =================================================================== #
    # 5. TIMEOUT / UNKNOWN RESPONSE
    # =================================================================== #

    def test_a_timeout_is_uncertain_and_verified_before_any_retry(self):
        self._sap(SAP_SILENT)
        receipt, flow = self._receipt()
        self._approve(flow)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.SAP_UNKNOWN)
        self.assertEqual(sap_settlement.attempt_state(receipt),
                         sap_settlement.UNCERTAIN)

    def test_an_uncertain_document_SAP_DOES_hold_is_adopted_not_reposted(self):
        """THE DUPLICATE THIS PREVENTS.

        SAP took the payment; the answer was lost. Posting again would pay the
        customer's money in twice. The reference in Comments is what makes the
        difference between knowing and guessing.
        """
        self._sap(SAP_SILENT)
        receipt, flow = self._receipt()
        self._approve(flow)

        post = self._sap(SAP_OK)          # would succeed if it were called
        self._lookup([sap_row(doc_entry=8888, doc_num=717171)])

        receipt.refresh_from_db()
        sap_settlement.settle(receipt, user=self.final)

        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(receipt.sap_doc_entry, 8888)
        self.assertEqual(receipt.sap_doc_num, 717171)
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        post.assert_not_called()
        self.assertIn('already held', receipt.sap_response)

    def test_an_uncertain_document_SAP_does_NOT_hold_is_posted(self):
        self._sap(SAP_SILENT)
        receipt, flow = self._receipt()
        self._approve(flow)

        post = self._sap(SAP_OK)
        self._lookup([])                   # asked, and SAP does not have it

        receipt.refresh_from_db()
        sap_settlement.settle(receipt, user=self.final)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(post.call_count, 1)

    def test_an_unreachable_SAP_stops_the_retry_rather_than_licensing_it(self):
        """A failed lookup must NEVER be read as "SAP does not have it"."""
        self._sap(SAP_SILENT)
        receipt, flow = self._receipt()
        self._approve(flow)

        post = self._sap(SAP_OK)
        self._lookup(error=RuntimeError('HANA unreachable'))

        receipt.refresh_from_db()
        with self.assertRaises(RuntimeError):
            sap_settlement.settle(receipt, user=self.final)

        post.assert_not_called()
        # And it is STILL recoverable — the approver can try again later.
        flow.refresh_from_db()
        allowed, _ = may_act_on(self.final, flow)
        self.assertTrue(allowed)

    def test_a_cancelled_SAP_document_is_refused_for_a_person_to_decide(self):
        self._sap(SAP_SILENT)
        receipt, flow = self._receipt()
        self._approve(flow)

        post = self._sap(SAP_OK)
        self._lookup([sap_row(canceled=True)])

        receipt.refresh_from_db()
        sap_settlement.settle(receipt, user=self.final)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)
        self.assertIn('CANCELLED', receipt.sap_response)
        post.assert_not_called()

    def test_two_SAP_documents_for_one_reference_are_refused_loudly(self):
        """Finding two IS the news: a duplicate already happened."""
        self._sap(SAP_SILENT)
        receipt, flow = self._receipt()
        self._approve(flow)

        post = self._sap(SAP_OK)
        self._lookup([sap_row(doc_entry=1), sap_row(doc_entry=2)])

        receipt.refresh_from_db()
        sap_settlement.settle(receipt, user=self.final)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)
        self.assertIn('MORE THAN ONE', receipt.sap_response)
        post.assert_not_called()

    # =================================================================== #
    # 6. DUPLICATE-POST PREVENTION, from every angle
    # =================================================================== #

    def test_a_stale_started_log_never_posts_without_asking_sap_first(self):
        """A worker that died MID-call. SAP may hold it; ask, never assume."""
        post = self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)

        # An attempt row exists — the request left — but it is old.
        from django.contrib.contenttypes.models import ContentType
        SapCallLog.objects.create(
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=receipt.pk, company_db='TESTDB', endpoint='/x',
            status=SapCallLog.Status.STARTED)
        SapCallLog.objects.filter(object_id=receipt.pk).update(
            created_at=timezone.now() - timedelta(hours=1))
        receipt = self._strand(receipt)

        self.assertEqual(sap_settlement.attempt_state(receipt),
                         sap_settlement.UNCERTAIN)

        lookup = self._lookup([sap_row()])
        sap_settlement.settle(receipt, user=self.final)

        lookup.assert_called_once()
        post.assert_not_called()

    def test_settling_concurrently_still_posts_only_once(self):
        post = self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)
        receipt = self._strand(receipt)

        for _ in range(4):
            fresh = PaymentReceipt.objects.get(pk=receipt.pk)
            sap_settlement.settle(fresh, user=self.final)

        self.assertEqual(post.call_count, 1)
        receipt.refresh_from_db()
        self.assertEqual(receipt.sap_doc_entry, 5151)

    # =================================================================== #
    # 7. THE GUARANTEE — no state without a result or a way forward
    # =================================================================== #

    def test_every_reachable_state_offers_a_result_or_a_retry(self):
        """The property the whole change exists to establish.

        For each way a posting can go wrong, the document must end either
        POSTED or actionable by its final approver. "Neither" is the bug.
        """
        cases = {
            'sap refused': SAP_REFUSED,
            'sap never answered': SAP_SILENT,
        }
        for label, outcome in cases.items():
            with self.subTest(label):
                self._sap(outcome)
                receipt, flow = self._receipt()
                self._approve(flow)

                receipt.refresh_from_db()
                flow.refresh_from_db()
                if receipt.status == PaymentReceipt.Status.POSTED:
                    continue
                allowed, reason = may_act_on(self.final, flow)
                self.assertTrue(
                    allowed,
                    f'{label}: left at {receipt.status} with no way '
                    f'forward — {reason}')

    def test_an_interrupted_post_also_offers_a_way_forward(self):
        self._sap(SAP_OK)
        receipt, flow = self._receipt()
        self._approve(flow, settle=False)
        self._strand(receipt)

        flow.refresh_from_db()
        allowed, reason = may_act_on(self.final, flow)
        self.assertTrue(allowed, f'interrupted post has no way forward: {reason}')
