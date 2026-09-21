"""The final approval is not complete until SAP accepts the document.

THE RULE THIS SUITE EXISTS FOR
------------------------------
Clicking Approve at the last stage is an AUTHORISATION. It does not finish the
workflow. The flow stays PENDING at that final stage until SAP has actually
accepted the posting, and only then becomes APPROVED.

    final approve -> POSTING_TO_SAP, flow still PENDING at the final stage
        SAP accepts  -> document POSTED,        flow APPROVED (complete)
        SAP refuses  -> document PENDING_ERROR, flow still at the final stage,
                        the same approver retries
        SAP silent   -> document SAP_UNKNOWN,   the retry is REFUSED, because
                        SAP may already hold the document

Intermediate stages never touch SAP at all.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
The lifecycle it replaced completed the flow first and reopened it if SAP
refused. That left a document reading as fully approved while nothing had been
posted, and — because `transaction.on_commit` is not durable — a restart
between the commit and the SAP call stranded the document permanently with no
way for anyone to retry it.

Nothing here calls SAP. `sap_post_payment` / `sap_post_deposit` are stubbed at
the seam, so every "SAP accepted" and "SAP refused" below is a decision this
suite makes, not a network call.

PostgreSQL only: workflow selection runs the configured queries for real.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from users.models import User, UserRole

from . import services, workflow_flow
from .models import (BankDeposit, BankDepositLine, FlowStatus,
                     PaymentMethodEntry, PaymentReceipt,
                     PaymentStatusHistory)
from .permissions import (DEPOSIT_APPROVE, DEPOSIT_CREATE, PAYMENTS_APPROVE,
                          PAYMENTS_CREATE, may_act_on)
from .sap_client import SapError
from .tests_support import history_of, uniq
from .tests_workflow_fixtures import payments_workflow

Action = PaymentStatusHistory.Action

#: What SAP returns when it accepts a document.
SAP_OK = {'DocEntry': 4242, 'DocNum': 918273}

#: A clean refusal: SAP answered, and the answer was no. Distinct from a
#: timeout, which carries no status code and means something entirely
#: different (see `SAP_SILENT`).
SAP_REFUSED = SapError('Posting period locked', status_code=400,
                       sap_code='-4013')

#: SAP never answered. The document may or may not exist there.
SAP_SILENT = SapError('Connection timed out', status_code=None)


def _user(prefix, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=uniq(prefix), name=prefix.title(), role=role,
        extra_pages=list(keys))


def _cash_deposit(creator, *, company='OIL'):
    """A deposit banking one CASH receipt — the case that reaches SAP."""
    receipt = PaymentReceipt.objects.create(
        receipt_no=uniq('RCP-DEPSRC-'), company=company, card_code='CUST1',
        payment_date=date.today(), total_amount=Decimal('100.00'),
        is_advance=True, sap_branch_id=1,
        status=PaymentReceipt.Status.POSTED,
        verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
        created_by=creator)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.CASH,
        amount=Decimal('100.00'), account_key='1105001',
        gl_account='1105001', receiving_bank_name='CASH SALE')
    deposit = BankDeposit.objects.create(
        deposit_no=uniq('DEP-FS-'), company=company,
        deposit_date=date.today(), collected_amount=Decimal('100.00'),
        deposit_amount=Decimal('100.00'), bank_key='INB:2201101',
        bank_code='INB', bank_gl_account='2201101',
        bank_display_name='INDIAN BANK', source_gl_account='1105001',
        status=BankDeposit.Status.DRAFT, created_by=creator)
    BankDepositLine.objects.create(
        deposit=deposit, receipt=receipt, amount=Decimal('100.00'))
    return deposit


def _cheque_deposit(creator, *, company='OIL'):
    """A deposit banking one CHEQUE receipt — it must NOT reach SAP."""
    receipt = PaymentReceipt.objects.create(
        receipt_no=uniq('RCP-CHQ-'), company=company, card_code='CUST1',
        payment_date=date.today(), total_amount=Decimal('100.00'),
        is_advance=True, sap_branch_id=1,
        status=PaymentReceipt.Status.POSTED,
        verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
        created_by=creator)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.CHEQUE,
        amount=Decimal('100.00'), cheque_number='000123',
        cheque_date=date.today(), bank_name='PNB',
        account_key='INB:1104106', gl_account='1104106',
        receiving_bank_name='INDIAN BANK')
    deposit = BankDeposit.objects.create(
        deposit_no=uniq('DEP-CHQ-'), company=company,
        # ZERO, and it has to be. Both figures count CASH, and this deposit
        # carries none: its cheque was banked by its own receipt and rides
        # here only as a record of the day it was handed in. A non-zero amount
        # would claim cash that does not exist and send SAP a document for it
        # — exactly what this fixture's test asserts cannot happen.
        deposit_date=date.today(), collected_amount=Decimal('0.00'),
        deposit_amount=Decimal('0.00'), bank_key='INB:2201101',
        bank_code='INB', bank_gl_account='2201101',
        bank_display_name='INDIAN BANK',
        status=BankDeposit.Status.DRAFT, created_by=creator)
    BankDepositLine.objects.create(
        deposit=deposit, receipt=receipt, amount=Decimal('100.00'))
    return deposit


class _SapStubbed(TestCase):
    """Everything the posting path reads from SAP, stubbed at the seam."""

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

    # -- driving SAP -------------------------------------------------------

    def _sap(self, outcome, *, entity='payment'):
        """Patch the ONE function that talks to SAP for this document type."""
        target = ('payments.sap_poster.sap_post_payment' if entity == 'payment'
                  else 'payments.sap_poster.sap_post_deposit')
        patcher = patch(target)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        if isinstance(outcome, Exception):
            mock.side_effect = outcome
        else:
            mock.return_value = outcome
        return mock

    def _approve(self, flow, user, **kwargs):
        """Approve, then settle against SAP exactly as the request does.

        The SAP post used to ride on `transaction.on_commit`, so this helper
        ran the captured callbacks. It does not any more — a lost callback is
        what stranded a receipt for 43 hours — so the caller makes the call
        explicitly and this mirrors `_ApproveView.post`.

        Approving WITHOUT this second line is now the honest way to simulate a
        process that died between the commit and the SAP call; several tests
        below do exactly that.
        """
        result = workflow_flow.approve(flow, user=user, **kwargs)
        document = workflow_flow.document_of(flow)
        workflow_flow.settle_after_approval(document, user=user)
        return result

    def _actions(self, document):
        return list(history_of(document).order_by('created_at', 'id')
                    .values_list('action', flat=True))


# ===========================================================================
# PAYMENT RECEIPTS
# ===========================================================================

class PaymentFinalStageTests(_SapStubbed):
    """Cases 1-15, for a payment receipt."""

    def setUp(self):
        super().setUp()
        self.creator = _user('fs_creator-', [PAYMENTS_CREATE])
        self.first = _user('fs_first-', [PAYMENTS_APPROVE])
        self.final = _user('fs_final-', [PAYMENTS_APPROVE])
        self.outsider = _user('fs_outsider-', [PAYMENTS_APPROVE])
        self.unprivileged = _user('fs_nokey-')
        self.workflow, self.stages = payments_workflow(
            self.first, self.final, company='OIL', code='PAYMENT_OIL_FS',
            documents='receipts')

    def _receipt(self, no=None):
        receipt = PaymentReceipt.objects.create(
            receipt_no=no or uniq('RCP-FS-'), company='OIL',
            card_code='CUST1', payment_date=date.today(),
            total_amount=Decimal('100.00'), is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        return receipt

    def _submit(self, receipt):
        """Submit the way `services.submit_receipt` does: status, then route."""
        receipt.status = PaymentReceipt.Status.PENDING_APPROVAL
        receipt.save(update_fields=['status'])
        return workflow_flow.start(receipt, user=self.creator)

    def _at_final_stage(self):
        """A receipt approved through stage 1 and waiting at the final one."""
        receipt = self._receipt()
        flow = self._submit(receipt)
        workflow_flow.approve(flow, user=self.first)
        flow.refresh_from_db()
        receipt.refresh_from_db()
        return receipt, flow

    # -- 1: selection ------------------------------------------------------

    def test_submit_selects_the_payment_workflow(self):
        receipt = self._receipt()
        flow = workflow_flow.start(receipt, user=self.creator)
        self.assertEqual(flow.workflow_id, self.workflow.id)
        self.assertEqual(flow.current_stage_id, self.stages[0].id)
        self.assertEqual(flow.total_stage, 2)

    # -- 2 + 3: intermediate approval --------------------------------------

    def test_stage_one_approval_advances_to_the_next_stage(self):
        receipt = self._receipt()
        flow = workflow_flow.start(receipt, user=self.creator)

        workflow_flow.approve(flow, user=self.first)

        flow.refresh_from_db()
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertEqual(flow.current_stage_id, self.stages[1].id)
        self.assertFalse(workflow_flow.is_at_final_stage(flow) is False)

    def test_an_intermediate_approval_never_calls_sap(self):
        """Only the final stage posts. This is the whole of stage 1's job."""
        sap = self._sap(SAP_OK)
        receipt = self._receipt()
        flow = self._submit(receipt)

        self._approve(flow, self.first)

        sap.assert_not_called()
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_APPROVAL)
        self.assertNotIn(Action.SAP_POST_STARTED.value, self._actions(receipt))

    # -- 4 + 5: the final approval -----------------------------------------

    def test_only_the_final_stage_user_may_approve_there(self):
        _receipt, flow = self._at_final_stage()

        allowed, _ = may_act_on(self.final, flow)
        self.assertTrue(allowed)
        for other in (self.first, self.outsider, self.unprivileged):
            allowed, reason = may_act_on(other, flow)
            self.assertFalse(allowed, f'{other.username} should not act')
            self.assertTrue(reason)

    def test_the_final_approval_calls_sap(self):
        sap = self._sap(SAP_OK)
        _receipt, flow = self._at_final_stage()

        self._approve(flow, self.final)

        sap.assert_called_once()

    # -- 6: SAP success ----------------------------------------------------

    def test_sap_success_completes_the_workflow_and_posts_the_document(self):
        self._sap(SAP_OK)
        receipt, flow = self._at_final_stage()

        self._approve(flow, self.final)

        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(receipt.sap_doc_entry, SAP_OK['DocEntry'])
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertIsNone(flow.current_stage_id)

    # -- 7 + 8: SAP failure ------------------------------------------------

    def test_sap_failure_does_not_complete_the_workflow(self):
        """THE RULE. An approval SAP refused is not a completed approval."""
        self._sap(SAP_REFUSED)
        receipt, flow = self._at_final_stage()

        self._approve(flow, self.final)

        flow.refresh_from_db()
        self.assertEqual(flow.status, FlowStatus.PENDING)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)
        self.assertIsNone(receipt.sap_doc_entry)

    def test_sap_failure_leaves_the_document_at_the_final_stage(self):
        self._sap(SAP_REFUSED)
        _receipt, flow = self._at_final_stage()

        self._approve(flow, self.final)

        flow.refresh_from_db()
        self.assertEqual(flow.current_stage_id, self.stages[-1].id)
        self.assertTrue(workflow_flow.is_at_final_stage(flow))
        # NOT restarted at stage 1 — the earlier approval still stands.
        self.assertNotEqual(flow.current_stage_id, self.stages[0].id)

    # -- 9, 10, 11: who may retry ------------------------------------------

    def test_the_final_approver_may_retry_after_a_sap_failure(self):
        self._sap(SAP_REFUSED)
        _receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        allowed, reason = may_act_on(self.final, flow)
        self.assertTrue(allowed, reason)

    def test_a_user_from_another_stage_cannot_retry(self):
        self._sap(SAP_REFUSED)
        _receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        allowed, reason = may_act_on(self.first, flow)
        self.assertFalse(allowed)
        self.assertIn('not awaiting your approval', reason)

    def test_a_user_without_the_approve_key_cannot_retry(self):
        """Holding the stage is not enough; the permission is checked too."""
        self._sap(SAP_REFUSED)
        _receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        self.final.extra_pages = []
        self.final.save(update_fields=['extra_pages'])

        allowed, reason = may_act_on(self.final, flow)
        self.assertFalse(allowed)
        self.assertIn('permission', reason)

    def test_an_unrelated_approver_cannot_retry(self):
        """Seeing the document is not the same as holding it."""
        self._sap(SAP_REFUSED)
        _receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        allowed, _ = may_act_on(self.outsider, flow)
        self.assertFalse(allowed)

    # -- 12 + 13: the retry itself -----------------------------------------

    def test_a_retry_that_sap_accepts_completes_the_workflow(self):
        self._sap(SAP_REFUSED)
        receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        with patch('payments.sap_poster.sap_post_payment',
                   return_value=SAP_OK):
            self._approve(flow, self.final)

        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(flow.status, FlowStatus.APPROVED)

    def test_a_retry_that_sap_refuses_again_stays_pending(self):
        self._sap(SAP_REFUSED)
        receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()
        self._approve(flow, self.final)

        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertTrue(workflow_flow.is_at_final_stage(flow))

    # -- the timeout case: retryable, because the retry ASKS SAP first -----

    def test_a_sap_timeout_leaves_the_retry_OPEN(self):
        """SAP may hold the document — so the retry checks before posting.

        THIS RULE WAS INVERTED, DELIBERATELY. It used to refuse: SAP might
        already have the payment and a second post would pay twice. That was
        the right call while OMS had no way to ask. It also meant a timed-out
        document had no way forward at all — no approve, no reject, no retry —
        which is a permanently stuck receipt by another name.

        Every payload now carries `OMS <receipt_no>` in its Comments
        (`sap_payloads.with_reference`), so `sap_settlement.settle` looks the
        document up in ORCT and either ADOPTS what SAP already has or posts
        knowing it is absent. The duplicate this guard protected against cannot
        happen, so the guard costs only the stuck state.
        """
        self._sap(SAP_SILENT)
        receipt, flow = self._at_final_stage()

        self._approve(flow, self.final)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.SAP_UNKNOWN)
        flow.refresh_from_db()
        allowed, _reason = may_act_on(self.final, flow)
        self.assertTrue(
            allowed,
            'a timed-out post must stay recoverable — the retry verifies '
            'against SAP before it posts anything')

    def test_a_timed_out_document_is_classified_as_UNCERTAIN(self):
        """So the retry goes through verification rather than posting blind."""
        from . import sap_settlement

        self._sap(SAP_SILENT)
        receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)

        receipt.refresh_from_db()
        self.assertEqual(sap_settlement.attempt_state(receipt),
                         sap_settlement.UNCERTAIN)

    def test_a_posting_in_flight_blocks_a_second_approval(self):
        """Two approvals must not each post the same receipt.

        The flow no longer completes on approval, so nothing structural stops
        a second click; POSTING_TO_SAP is what does.
        """
        receipt, flow = self._at_final_stage()
        # Approve WITHOUT settling: the document is left exactly as a request
        # that has committed but died before reaching SAP.
        workflow_flow.approve(flow, user=self.final)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTING_TO_SAP)
        flow.refresh_from_db()
        allowed, reason = may_act_on(self.final, flow)
        self.assertFalse(allowed)
        self.assertIn('already in progress', reason)

        with self.assertRaises(workflow_flow.PaymentFlowError):
            workflow_flow.approve(flow, user=self.final)

    # -- 14 + 15: history --------------------------------------------------

    def test_history_is_appended_and_never_rewritten(self):
        self._sap(SAP_REFUSED)
        receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)

        after_failure = self._actions(receipt)
        ids_before = list(history_of(receipt).order_by('id').values_list('id', flat=True))

        flow.refresh_from_db()
        with patch('payments.sap_poster.sap_post_payment',
                   return_value=SAP_OK):
            self._approve(flow, self.final)

        after_retry = self._actions(receipt)
        # Every earlier row survives, in the same order, with the same ids.
        self.assertEqual(after_retry[:len(after_failure)], after_failure)
        ids_after = list(history_of(receipt).order_by('id').values_list('id', flat=True))
        self.assertEqual(ids_after[:len(ids_before)], ids_before)

    def test_the_failed_attempt_stays_visible_after_a_successful_retry(self):
        self._sap(SAP_REFUSED)
        receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()
        with patch('payments.sap_poster.sap_post_payment',
                   return_value=SAP_OK):
            self._approve(flow, self.final)

        actions = self._actions(receipt)
        self.assertIn(Action.SAP_FAILED.value, actions)
        self.assertIn(Action.SAP_POSTED.value, actions)
        # Two approvals were given, and both are recorded.
        self.assertEqual(actions.count(Action.APPROVED.value), 3)

    def test_both_stage_approvals_are_recorded(self):
        self._sap(SAP_OK)
        receipt, flow = self._at_final_stage()
        self._approve(flow, self.final)

        approvals = list(history_of(receipt)
                         .filter(action=Action.APPROVED)
                         .order_by('created_at', 'id')
                         .values_list('changed_by_username', 'level'))
        self.assertEqual([u for u, _ in approvals],
                         [self.first.username, self.final.username])
        self.assertEqual([lvl for _, lvl in approvals], [1, 2])


# ===========================================================================
# BANK DEPOSITS — the same rule, its own workflow
# ===========================================================================

class DepositFinalStageTests(_SapStubbed):
    """The deposit half of cases 1-15."""

    def setUp(self):
        super().setUp()
        self.creator = _user('ds_creator-', [DEPOSIT_CREATE])
        self.first = _user('ds_first-', [DEPOSIT_APPROVE])
        self.final = _user('ds_final-', [DEPOSIT_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.first, self.final, company='OIL', code='DEPOSIT_OIL_FS',
            documents='deposits')

    def _deposit(self):
        """A deposit with a CASH line, so it genuinely posts to SAP.

        A deposit whose lines are all cheques posts NOTHING — the cheques
        reached SAP when their receipts did — and is completed as an OMS
        record. That path is covered by `ChequeOnlyDepositTests`; these tests
        are about the SAP one, so they carry cash.
        """
        return _cash_deposit(self.creator)

    def _submit(self, deposit):
        deposit.status = BankDeposit.Status.PENDING_APPROVAL
        deposit.save(update_fields=['status'])
        return workflow_flow.start(deposit, user=self.creator)

    def _at_final_stage(self):
        deposit = self._deposit()
        flow = self._submit(deposit)
        workflow_flow.approve(flow, user=self.first)
        flow.refresh_from_db()
        deposit.refresh_from_db()
        return deposit, flow

    def test_submit_selects_the_deposit_workflow(self):
        deposit = self._deposit()
        flow = self._submit(deposit)
        self.assertEqual(flow.workflow_id, self.workflow.id)

    def test_an_intermediate_deposit_approval_never_calls_sap(self):
        sap = self._sap(SAP_OK, entity='deposit')
        deposit = self._deposit()
        flow = self._submit(deposit)

        self._approve(flow, self.first)

        sap.assert_not_called()
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, BankDeposit.Status.PENDING_APPROVAL)

    def test_sap_success_completes_the_deposit_workflow(self):
        self._sap(SAP_OK, entity='deposit')
        deposit, flow = self._at_final_stage()

        self._approve(flow, self.final)

        deposit.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(deposit.status, BankDeposit.Status.POSTED)
        self.assertEqual(deposit.sap_doc_entry, SAP_OK['DocEntry'])
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertIsNone(flow.current_stage_id)

    def test_sap_failure_leaves_the_deposit_at_its_final_stage(self):
        self._sap(SAP_REFUSED, entity='deposit')
        deposit, flow = self._at_final_stage()

        self._approve(flow, self.final)

        deposit.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(deposit.status, BankDeposit.Status.PENDING_ERROR)
        self.assertEqual(flow.status, FlowStatus.PENDING)
        self.assertTrue(workflow_flow.is_at_final_stage(flow))

    def test_the_final_deposit_approver_may_retry_and_succeed(self):
        self._sap(SAP_REFUSED, entity='deposit')
        deposit, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        allowed, reason = may_act_on(self.final, flow)
        self.assertTrue(allowed, reason)

        with patch('payments.sap_poster.sap_post_deposit',
                   return_value=SAP_OK):
            self._approve(flow, self.final)

        deposit.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(deposit.status, BankDeposit.Status.POSTED)
        self.assertEqual(flow.status, FlowStatus.APPROVED)

    def test_a_deposit_approver_from_another_stage_cannot_retry(self):
        self._sap(SAP_REFUSED, entity='deposit')
        _deposit, flow = self._at_final_stage()
        self._approve(flow, self.final)
        flow.refresh_from_db()

        allowed, _ = may_act_on(self.first, flow)
        self.assertFalse(allowed)

    def test_deposit_history_is_appended_not_rewritten(self):
        self._sap(SAP_REFUSED, entity='deposit')
        deposit, flow = self._at_final_stage()
        self._approve(flow, self.final)
        before = list(history_of(deposit).order_by('id').values_list('id', flat=True))

        flow.refresh_from_db()
        with patch('payments.sap_poster.sap_post_deposit',
                   return_value=SAP_OK):
            self._approve(flow, self.final)

        after = list(history_of(deposit).order_by('id').values_list('id', flat=True))
        self.assertEqual(after[:len(before)], before)
        self.assertIn(Action.SAP_FAILED.value, self._actions(deposit))


# ===========================================================================
# REJECTION — never reaches SAP
# ===========================================================================

class RejectionTests(_SapStubbed):
    def setUp(self):
        super().setUp()
        self.creator = _user('rj_creator-', [PAYMENTS_CREATE])
        self.final = _user('rj_final-', [PAYMENTS_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.final, company='OIL', code='PAYMENT_OIL_RJ',
            documents='receipts')

    def _receipt(self):
        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-RJ-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        return receipt

    def test_rejecting_at_the_final_stage_never_posts_to_sap(self):
        sap = self._sap(SAP_OK)
        receipt = self._receipt()
        flow = workflow_flow.start(receipt, user=self.creator)

        with self.captureOnCommitCallbacks(execute=True):
            workflow_flow.reject(flow, user=self.final, remarks='No.')

        sap.assert_not_called()
        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.REJECTED)
        self.assertEqual(flow.status, FlowStatus.REJECTED)


# ===========================================================================
# THE TWO WORKFLOWS — cases 16-20
# ===========================================================================

class WorkflowSeparationTests(_SapStubbed):
    """PAYMENT and DEPOSIT are two workflows under one module."""

    def setUp(self):
        super().setUp()
        self.creator = _user('sep_creator-',
                             [PAYMENTS_CREATE, DEPOSIT_CREATE])
        self.pay_approver = _user('sep_pay-', [PAYMENTS_APPROVE])
        self.dep_approver = _user('sep_dep-', [DEPOSIT_APPROVE])
        self.pay_wf, self.pay_stages = payments_workflow(
            self.pay_approver, company='OIL', code='PAYMENT_OIL_SEP',
            documents='receipts')
        self.dep_wf, self.dep_stages = payments_workflow(
            self.dep_approver, company='OIL', code='DEPOSIT_OIL_SEP',
            documents='deposits')

    def _receipt(self, no=None):
        receipt = PaymentReceipt.objects.create(
            receipt_no=no or uniq('RCP-SEP-'), company='OIL',
            card_code='CUST1', payment_date=date.today(),
            total_amount=Decimal('100.00'), is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        return receipt

    def _deposit(self, no=None):
        return BankDeposit.objects.create(
            deposit_no=no or uniq('DEP-SEP-'), company='OIL',
            deposit_date=date.today(), collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'), bank_key='INB:2201101',
            bank_code='INB', bank_gl_account='2201101',
            bank_display_name='INDIAN BANK', source_gl_account='1105001',
            status=BankDeposit.Status.DRAFT, created_by=self.creator)

    # -- 16 ----------------------------------------------------------------

    def test_a_receipt_and_a_deposit_select_different_workflows(self):
        receipt_flow = workflow_flow.start(self._receipt(), user=self.creator)
        deposit_flow = workflow_flow.start(self._deposit(), user=self.creator)

        self.assertEqual(receipt_flow.workflow_id, self.pay_wf.id)
        self.assertEqual(deposit_flow.workflow_id, self.dep_wf.id)
        self.assertNotEqual(receipt_flow.workflow_id, deposit_flow.workflow_id)
        # ...and therefore different approvers.
        self.assertEqual(receipt_flow.current_user_id, self.pay_approver.id)
        self.assertEqual(deposit_flow.current_user_id, self.dep_approver.id)

    def test_one_module_serves_both(self):
        """Two workflows, ONE module registration — never PAYMENT + DEPOSIT."""
        from workflow.models import WorkflowModule

        from .apps import MODULE_CODE

        self.assertEqual(self.pay_wf.module_id, self.dep_wf.module_id)
        self.assertEqual(
            WorkflowModule.objects.filter(code=MODULE_CODE).count(), 1)
        self.assertEqual(self.pay_wf.module.code, MODULE_CODE)

    # -- 17 ----------------------------------------------------------------

    def test_a_receipt_and_a_deposit_sharing_an_id_do_not_collide(self):
        """Why selection keys on the NUMBER and not the id.

        Receipt 5 and deposit 5 both exist. Keyed on `id` each would match the
        other's query and every submit would be ambiguous.
        """
        receipt = self._receipt()
        deposit = self._deposit()

        self.assertNotEqual(workflow_flow.document_number(receipt),
                            workflow_flow.document_number(deposit))
        self.assertTrue(
            workflow_flow.document_number(receipt).startswith('RCP-'))
        self.assertTrue(
            workflow_flow.document_number(deposit).startswith('DEP-'))

        # Both route cleanly even though the fixtures are the same shape.
        self.assertEqual(
            workflow_flow.start(receipt, user=self.creator).workflow_id,
            self.pay_wf.id)
        self.assertEqual(
            workflow_flow.start(deposit, user=self.creator).workflow_id,
            self.dep_wf.id)

    # -- 18 ----------------------------------------------------------------

    def test_a_query_without_doc_no_cannot_route_a_document(self):
        """The alias every payments query must project.

        A query that omits it fails at SUBMIT, not at configuration — which is
        why the runbook's check exists. Nothing is left half-submitted.
        """
        from workflow.models import WorkflowQuery

        # Rewrite the payment workflow's query to drop the alias, exactly the
        # shape found configured in TEST.
        WorkflowQuery.objects.filter(workflow=self.pay_wf).update(
            query_text='SELECT * FROM payments.payment_receipt '
                       "WHERE company = 'OIL'")
        receipt = self._receipt()

        # It fails at SUBMIT, as the engine's condition error — configuration
        # validation cannot catch it, which is exactly why the deployment
        # runbook ships a query to detect it instead.
        with self.assertRaises(Exception) as caught:
            workflow_flow.start(receipt, user=self.creator)
        self.assertIn('could not be evaluated', str(caught.exception).lower())

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.DRAFT)
        self.assertIsNone(workflow_flow.flow_of(receipt))

    # -- 19 ----------------------------------------------------------------

    def test_two_workflows_matching_one_document_roll_back(self):
        """The engine has no company precedence: two matches is ambiguous."""
        rival, _stages = payments_workflow(
            self.pay_approver, company='OIL', code='PAYMENT_OIL_RIVAL',
            documents='receipts')
        self.assertNotEqual(rival.id, self.pay_wf.id)
        receipt = self._receipt()

        with self.assertRaises(workflow_flow.PaymentFlowError):
            workflow_flow.start(receipt, user=self.creator)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.DRAFT)
        self.assertIsNone(workflow_flow.flow_of(receipt))

    # -- 20 ----------------------------------------------------------------

    def test_no_workflow_configured_rolls_back(self):
        from workflow.models import Workflow

        Workflow.objects.filter(pk=self.pay_wf.pk).update(is_active=False)
        receipt = self._receipt()

        with self.assertRaises(workflow_flow.PaymentFlowError):
            workflow_flow.start(receipt, user=self.creator)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.DRAFT)
        self.assertIsNone(workflow_flow.flow_of(receipt))


# ===========================================================================
# WHO HOLDS A DOCUMENT — cases 21-24
# ===========================================================================

class EffectiveUserTests(_SapStubbed):
    """Authority is resolved from configuration on every read, never stored."""

    def setUp(self):
        super().setUp()
        self.creator = _user('eu_creator-', [PAYMENTS_CREATE])
        self.approver = _user('eu_approver-', [PAYMENTS_APPROVE])
        self.replacement = _user('eu_stand_in-', [PAYMENTS_APPROVE])
        self.reassigned = _user('eu_reassigned-', [PAYMENTS_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.approver, company='OIL', code='PAYMENT_OIL_EU',
            documents='receipts')

    def _submitted(self):
        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-EU-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        return receipt, workflow_flow.start(receipt, user=self.creator)

    # -- 21 ----------------------------------------------------------------

    def test_the_effective_user_comes_from_the_stage_not_the_flow(self):
        _receipt, flow = self._submitted()
        self.assertEqual(
            workflow_flow.effective_user_id(flow.current_stage_id),
            self.approver.id)

    # -- 22 ----------------------------------------------------------------

    def test_reassigning_a_stage_moves_a_waiting_document(self):
        """Nothing on the flow is rewritten; the answer follows the config."""
        _receipt, flow = self._submitted()

        stage = self.stages[0]
        stage.user = self.reassigned
        stage.save(update_fields=['user'])

        allowed, _ = may_act_on(self.reassigned, flow)
        self.assertTrue(allowed)
        allowed, reason = may_act_on(self.approver, flow)
        self.assertFalse(allowed, 'the previous holder must lose it')
        self.assertIn('not awaiting your approval', reason)

    # -- 23 ----------------------------------------------------------------

    def test_a_replacement_stands_in_for_the_stage_user(self):
        from workflow.models import WorkflowUserReplacement

        _receipt, flow = self._submitted()
        today = date.today()
        WorkflowUserReplacement.objects.create(
            old_user=self.approver, new_user=self.replacement,
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=1))

        allowed, _ = may_act_on(self.replacement, flow)
        self.assertTrue(allowed)
        self.assertEqual(
            workflow_flow.effective_user_id(flow.current_stage_id),
            self.replacement.id)

    # -- 24 ----------------------------------------------------------------

    def test_resubmission_reselects_the_current_configuration(self):
        """A rejected document goes round against the config as it is NOW."""
        receipt, flow = self._submitted()
        workflow_flow.reject(flow, user=self.approver, remarks='Fix it.')

        # The configuration changes while the document is back with its
        # creator: a second stage is added.
        from .tests_workflow_fixtures import make_stage

        second = make_stage(self.workflow, 2, self.reassigned)

        receipt.refresh_from_db()
        receipt.status = PaymentReceipt.Status.REJECTED
        receipt.save(update_fields=['status'])
        again = workflow_flow.start(receipt, user=self.creator)

        # SAME flow row, re-selected, and it now knows about the new stage.
        self.assertEqual(again.pk, flow.pk)
        self.assertEqual(again.status, FlowStatus.PENDING)
        self.assertEqual(again.total_stage, 2)
        self.assertEqual(again.current_stage_id, self.stages[0].id)
        self.assertFalse(workflow_flow.is_at_final_stage(again))
        self.assertEqual(
            workflow_flow.stages_for(again)[-1].id, second.id)

    def test_resubmission_appends_rather_than_replacing_history(self):
        receipt, flow = self._submitted()
        workflow_flow.reject(flow, user=self.approver, remarks='Fix it.')
        before = list(history_of(receipt).order_by('id').values_list('id', flat=True))

        receipt.refresh_from_db()
        receipt.status = PaymentReceipt.Status.REJECTED
        receipt.save(update_fields=['status'])
        workflow_flow.start(receipt, user=self.creator)

        after = list(history_of(receipt).order_by('id').values_list('id', flat=True))
        self.assertEqual(after[:len(before)], before)
        self.assertIn(Action.RESUBMITTED.value,
                      list(history_of(receipt).values_list('action', flat=True)))


# ===========================================================================
# NO REGRESSION IN THE ACCOUNT WORK — cases 25-27
# ===========================================================================

class AccountSnapshotSurvivesTheLifecycleTests(_SapStubbed):
    """The receiving account chosen by the user still drives the posting."""

    def setUp(self):
        super().setUp()
        self.creator = _user('sn_creator-', [PAYMENTS_CREATE])
        self.final = _user('sn_final-', [PAYMENTS_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.final, company='OIL', code='PAYMENT_OIL_SN',
            documents='receipts')

    def _receipt_with_snapshot(self):
        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-SN-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        entry = PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'),
            account_key='1105001', gl_account='1105001',
            receiving_bank_name='CASH SALE')
        return receipt, entry

    def test_the_snapshot_is_untouched_by_approval_and_posting(self):
        self._sap(SAP_OK)
        receipt, entry = self._receipt_with_snapshot()
        flow = workflow_flow.start(receipt, user=self.creator)

        self._approve(flow, self.final)

        entry.refresh_from_db()
        self.assertEqual(entry.account_key, '1105001')
        self.assertEqual(entry.gl_account, '1105001')
        self.assertEqual(entry.receiving_bank_name, 'CASH SALE')
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)

    def test_the_snapshot_survives_a_sap_failure_and_a_retry(self):
        self._sap(SAP_REFUSED)
        receipt, entry = self._receipt_with_snapshot()
        flow = workflow_flow.start(receipt, user=self.creator)
        self._approve(flow, self.final)

        entry.refresh_from_db()
        self.assertEqual(entry.gl_account, '1105001')

        flow.refresh_from_db()
        with patch('payments.sap_poster.sap_post_payment',
                   return_value=SAP_OK):
            self._approve(flow, self.final)

        entry.refresh_from_db()
        self.assertEqual(entry.gl_account, '1105001')


class DepositSourceSurvivesTheLifecycleTests(_SapStubbed):
    """The frozen source G/L is still frozen, and still posted from."""

    def setUp(self):
        super().setUp()
        self.creator = _user('dsg_creator-', [DEPOSIT_CREATE])
        self.final = _user('dsg_final-', [DEPOSIT_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.final, company='OIL', code='DEPOSIT_OIL_SG',
            documents='deposits')

    def test_the_frozen_source_gl_is_unchanged_by_the_lifecycle(self):
        self._sap(SAP_OK, entity='deposit')
        deposit = BankDeposit.objects.create(
            deposit_no=uniq('DEP-SG-'), company='OIL',
            deposit_date=date.today(), collected_amount=Decimal('100.00'),
            deposit_amount=Decimal('100.00'), bank_key='INB:2201101',
            bank_code='INB', bank_gl_account='2201101',
            bank_display_name='INDIAN BANK', source_gl_account='1105001',
            status=BankDeposit.Status.DRAFT, created_by=self.creator)
        flow = workflow_flow.start(deposit, user=self.creator)

        self._approve(flow, self.final)

        deposit.refresh_from_db()
        self.assertEqual(deposit.source_gl_account, '1105001')
        self.assertEqual(deposit.status, BankDeposit.Status.POSTED)


class RecoveryKeepsHistoryTests(_SapStubbed):
    """Case 27: the stranded sweep appends, it never deletes."""

    def setUp(self):
        super().setUp()
        self.creator = _user('rk_creator-', [PAYMENTS_CREATE])
        self.final = _user('rk_final-', [PAYMENTS_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.final, company='OIL', code='PAYMENT_OIL_RK',
            documents='receipts')

    def test_recovering_a_stranded_document_preserves_its_history(self):
        from .sap_recovery import find_stranded, recover_stranded_posts

        receipt = PaymentReceipt.objects.create(
            receipt_no=uniq('RCP-RK-'), company='OIL', card_code='CUST1',
            payment_date=date.today(), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            status=PaymentReceipt.Status.DRAFT,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            created_by=self.creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.CASH,
            amount=Decimal('100.00'))
        flow = workflow_flow.start(receipt, user=self.creator)

        # Approve WITHOUT settling — the interrupted-post case this sweep
        # exists for — then age it past the threshold.
        workflow_flow.approve(flow, user=self.final)
        PaymentReceipt.objects.filter(pk=receipt.pk).update(
            updated_at=timezone.now() - timedelta(hours=2))

        # Membership, not equality. These run against the SHARED TEST
        # database, where a genuinely stranded document raised by somebody
        # using the app is visible too — RCP-OIL-20260919-000003 is one. What
        # else the sweep finds is not this test's business.
        self.assertIn(receipt.pk, [r.pk for r in find_stranded(PaymentReceipt)])
        before = list(history_of(receipt).order_by('id').values_list('id', flat=True))

        self._sap(SAP_OK)
        summary = recover_stranded_posts()

        self.assertEqual(summary['receipts_posted'], 1)
        after = list(history_of(receipt).order_by('id').values_list('id', flat=True))
        self.assertEqual(after[:len(before)], before,
                         'recovery must append, never rewrite')
        receipt.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(flow.status, FlowStatus.APPROVED)


class ChequeOnlyDepositTests(_SapStubbed):
    """A deposit with nothing for SAP still finishes its approval.

    THE TRAP THE FINAL-STAGE RULE SETS. "The flow completes when SAP accepts
    the document" silently assumes there is always a SAP call to make. A
    cheque-only deposit posts nothing — the cheques reached SAP when their
    receipts did — so nothing would ever complete its flow, and the deposit
    would read POSTED while an approver waited on it forever.
    """

    def setUp(self):
        super().setUp()
        self.creator = _user('co_creator-', [DEPOSIT_CREATE])
        self.final = _user('co_final-', [DEPOSIT_APPROVE])
        self.workflow, self.stages = payments_workflow(
            self.final, company='OIL', code='DEPOSIT_OIL_CO',
            documents='deposits')

    def test_a_cheque_only_deposit_completes_without_calling_sap(self):
        sap = self._sap(SAP_OK, entity='deposit')
        deposit = _cheque_deposit(self.creator)
        deposit.status = BankDeposit.Status.PENDING_APPROVAL
        deposit.save(update_fields=['status'])
        flow = workflow_flow.start(deposit, user=self.creator)

        self._approve(flow, self.final)

        sap.assert_not_called()
        deposit.refresh_from_db()
        flow.refresh_from_db()
        self.assertEqual(deposit.status, BankDeposit.Status.POSTED)
        self.assertIsNone(deposit.sap_doc_entry)
        # AND the approval is finished — not left waiting on a post that is
        # never coming.
        self.assertEqual(flow.status, FlowStatus.APPROVED)
        self.assertIsNone(flow.current_stage_id)


class ChequeDepositIsCreatableTests(TestCase):
    """A CHEQUE deposit banks no cash, and must still be creatable.

    A regression guard. When deposits became cash-only, a cheque deposit's
    `collected_amount` and `deposit_amount` both became 0 — correct, since
    every cheque in it was posted to SAP by its own receipt and the deposit
    records only the day the paper was handed in. But zero had been impossible
    until then, so code assuming "an amount is always positive" silently locked
    these deposits out:

      * the model's `bank_deposit_amount_positive` CHECK was `> 0`, so the row
        could not be written at all (now `>= 0`, migration 0042);
      * the mobile form gated Submit on `depositedAmount <= 0`, leaving a dead
        button with nothing on screen explaining why (now `< 0`).

    The serializer declares no `min_value`; these keep it that way.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username=uniq('chq-dep-'), password='pw', name='C')

    def test_a_cheque_deposit_is_written_with_zero_amounts(self):
        deposit = BankDeposit.objects.create(
            deposit_no=uniq('DEP-CHQONLY-'), company='OIL',
            deposit_date=date.today(), bank_key='INB:2201101',
            bank_gl_account='2201101', collected_amount=Decimal('0'),
            deposit_amount=Decimal('0'), created_by=self.user)

        deposit.refresh_from_db()
        self.assertEqual(deposit.deposit_amount, Decimal('0'))
        # Nothing was collected, so nothing is missing — and no reason may be
        # demanded for a shortfall that does not exist.
        self.assertEqual(deposit.shortfall, Decimal('0'))
        self.assertEqual(deposit.shortfall_reason, '')

    def test_a_cheque_deposit_posts_nothing_to_sap(self):
        deposit = BankDeposit.objects.create(
            deposit_no=uniq('DEP-CHQONLY-'), company='OIL',
            deposit_date=date.today(), bank_key='INB:2201101',
            bank_gl_account='2201101', collected_amount=Decimal('0'),
            deposit_amount=Decimal('0'), created_by=self.user)

        self.assertEqual(services.sap_postable_amount(deposit), Decimal('0'))

    def test_a_negative_amount_is_still_refused(self):
        """Relaxing `> 0` to `>= 0` must not have opened the door below zero."""
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BankDeposit.objects.create(
                    deposit_no=uniq('DEP-NEG-'), company='OIL',
                    deposit_date=date.today(), bank_key='INB:2201101',
                    bank_gl_account='2201101', collected_amount=Decimal('0'),
                    deposit_amount=Decimal('-1.00'), created_by=self.user)
