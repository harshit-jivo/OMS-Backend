"""The payment history reads as a business timeline, not a state machine.

Every technical column is still WRITTEN and still stored — this suite asserts
what the business-facing API chooses to show, and that the events which carry
meaning (created, verified, approved, rejected, SAP outcome) are actually
recorded rather than collapsing into anonymous STATUS_CHANGED rows.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from . import services
from .models import PaymentMethodEntry, PaymentReceipt, PaymentStatusHistory
from .permissions import PAYMENTS_APPROVE, PAYMENTS_CREATE, PAYMENTS_VERIFY

Action = PaymentStatusHistory.Action


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role,
        extra_pages=list(keys))


def _receipt(no, creator, *, verification='PENDING',
             status=PaymentReceipt.Status.DRAFT):
    receipt = PaymentReceipt.objects.create(
        receipt_no=no, company='OIL', card_code='CUST1',
        payment_date=date.today(), total_amount=Decimal('100.00'),
        is_advance=True, sap_branch_id=1,
        status=status, verification_status=verification, created_by=creator)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.CASH,
        amount=Decimal('100.00'))
    return receipt


def _rows(receipt):
    return PaymentStatusHistory.objects.filter(
        content_type=ContentType.objects.get_for_model(PaymentReceipt),
        object_id=receipt.pk).order_by('created_at', 'id')


class BusinessHistoryApiTests(TestCase):
    """What the /history/ endpoint shows, and what it deliberately hides."""

    def setUp(self):
        self.creator = _user('h_creator', [PAYMENTS_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.creator)
        self.receipt = _receipt('RC-H-1', self.creator)
        services.log_status(
            self.receipt, to_status='DRAFT', user=self.creator,
            action=Action.CREATED, reason='Receipt created.')

    def _history(self, query=''):
        resp = self.client.get(
            reverse('payment-receipt-history', args=[self.receipt.pk]) + query)
        self.assertEqual(resp.status_code, 200)
        return resp.data['data']

    # 10 — the technical columns are absent from the business representation.
    def test_technical_fields_are_not_exposed(self):
        row = self._history()[0]
        # `actor_kind`, `ip_address` and `sap_doc_entry` are no longer columns
        # at all (migration 0031); `from_status`/`to_status`/`level_label` are
        # still stored and still hidden from this view.
        for hidden in ('from_status', 'to_status', 'actor_kind',
                       'ip_address', 'sap_doc_entry', 'level_label'):
            self.assertNotIn(hidden, row)

    def test_business_fields_are_exposed(self):
        row = self._history()[0]
        for shown in ('action', 'action_display', 'stage', 'stage_display',
                      'level', 'performed_by', 'reason', 'created_at'):
            self.assertIn(shown, row)

    def test_created_event_reads_as_business_language(self):
        row = self._history()[0]
        self.assertEqual(row['action'], 'CREATED')
        self.assertEqual(row['action_display'], 'Created')
        self.assertEqual(row['stage'], 'Payments_Create')
        self.assertEqual(row['stage_display'], 'Payments — Create')
        self.assertEqual(row['performed_by'], 'h_creator')

    # A bare STATUS_CHANGED row is stored but not shown.
    def test_status_changed_rows_are_hidden_but_retained(self):
        services.log_status(self.receipt, from_status='DRAFT',
                            to_status='PENDING_APPROVAL', user=self.creator)
        self.assertEqual(
            _rows(self.receipt).filter(action=Action.STATUS_CHANGED).count(), 1)
        actions = [r['action'] for r in self._history()]
        self.assertNotIn('STATUS_CHANGED', actions)

    def test_full_flag_returns_the_technical_rows(self):
        """Support needs the raw timeline; it is one query param away."""
        services.log_status(self.receipt, from_status='DRAFT',
                            to_status='PENDING_APPROVAL', user=self.creator)
        actions = [r['action'] for r in self._history('?full=true')]
        self.assertIn('STATUS_CHANGED', actions)

    def test_rows_are_oldest_first(self):
        services.log_status(self.receipt, to_status='DRAFT',
                            user=self.creator, action=Action.UPDATED,
                            reason='Receipt edited.')
        self.assertEqual([r['action'] for r in self._history()],
                         ['CREATED', 'UPDATED'])

    # 19 — a system event says "System", not an actor_kind code.
    def test_system_events_are_attributed_to_system(self):
        services.log_status(self.receipt, to_status='POSTED', user=None,
                            actor_kind='SAP', action=Action.SAP_POSTED,
                            reason='Posted to SAP.')
        row = [r for r in self._history() if r['action'] == 'SAP_POSTED'][0]
        self.assertEqual(row['performed_by'], 'System')
        self.assertEqual(row['stage_display'], 'SAP Posting')

    # 20 — the DocEntry belongs to the receipt, not to every history row.
    def test_sap_docentry_is_not_repeated_in_history(self):
        services.log_status(self.receipt, to_status='POSTED', user=None,
                            actor_kind='SAP', action=Action.SAP_POSTED,
                            sap_doc_entry=20802, reason='Posted.')
        row = [r for r in self._history() if r['action'] == 'SAP_POSTED'][0]
        self.assertNotIn('sap_doc_entry', row)
        # ...but it IS still stored, for the forensic view.
        stored = _rows(self.receipt).filter(action=Action.SAP_POSTED).first()
        # The column is gone: log_status still ACCEPTS sap_doc_entry so the
        # SAP writers need no edit, but it is no longer persisted. The receipt
        # remains the authoritative home for the document key.
        self.assertFalse(hasattr(stored, 'sap_doc_entry'))

    def test_stage_maps_each_action(self):
        cases = {
            Action.VERIFIED: 'Payments — Verify',
            Action.APPROVED: 'Payments — Approve',
            Action.REJECTED: 'Payments — Approve',
            Action.SAP_FAILED: 'SAP Posting',
        }
        for action, expected in cases.items():
            services.log_status(self.receipt, to_status='X',
                                user=self.creator, action=action, reason='.')
        shown = {r['action']: r['stage_display'] for r in self._history()}
        for action, expected in cases.items():
            self.assertEqual(shown[action.value], expected)


class WriterActionTests(TestCase):
    """The events that carry meaning are recorded with a real action."""

    def setUp(self):
        patcher = patch('payments.services._bank_accounts_for',
                        return_value={'CASH': '100001'})
        patcher.start()
        self.addCleanup(patcher.stop)

        from approvals.models import (
            ApprovalLevel,
            ApprovalLevelApprover,
            ApprovalWorkflow,
        )
        self.creator = _user('w_creator', [PAYMENTS_CREATE])
        self.verifier = _user('w_verifier', [PAYMENTS_VERIFY])
        # Both are needed: the KEY says they may approve payments at all, the
        # level grant says which rung they hold.
        self.approver = _user('w_approver', [PAYMENTS_APPROVE])
        workflow = ApprovalWorkflow.objects.create(
            code='PAYMENT_OIL_HIST', name='OIL payments',
            document_type='PAYMENT', company='OIL')
        level = ApprovalLevel.objects.create(
            workflow=workflow, sequence=1, name='Accountant Review')
        ApprovalLevelApprover.objects.create(
            level=level, user=self.approver, company='OIL')

    # 3 + 5 — verification is ONE business event, at the verify stage.
    def test_verification_records_one_verified_event(self):
        receipt = _receipt('RC-W-1', self.creator)
        services.verify_receipt(receipt.pk, self.verifier, remarks='Counted.')

        verified = _rows(receipt).filter(action=Action.VERIFIED)
        self.assertEqual(verified.count(), 1)
        self.assertEqual(verified.first().changed_by_username,
                         self.verifier.username)

    # 4 — attribution: creator created, verifier verified.
    def test_attribution_is_correct_across_the_timeline(self):
        receipt = _receipt('RC-W-2', self.creator)
        services.log_status(receipt, to_status='DRAFT', user=self.creator,
                            action=Action.CREATED, reason='Receipt created.')
        services.verify_receipt(receipt.pk, self.verifier)

        by_action = {r.action: r.changed_by_username for r in _rows(receipt)}
        self.assertEqual(by_action[Action.CREATED], 'w_creator')
        self.assertEqual(by_action[Action.VERIFIED], 'w_verifier')

    # 6 + 7 — approval is recorded on the payment timeline, with its rung.
    def test_approval_records_an_approved_event_with_its_level(self):
        from approvals import services as approval_services

        receipt = _receipt('RC-W-3', self.creator)
        services.verify_receipt(receipt.pk, self.verifier)

        request = receipt.approvals.first()
        approval_services.approve(
            request_id=request.pk, user=self.approver, remarks='Looks right.')

        approved = _rows(receipt).filter(action=Action.APPROVED)
        self.assertEqual(approved.count(), 1)
        row = approved.first()
        self.assertEqual(row.level, 1)
        self.assertEqual(row.changed_by_username, 'w_approver')
        self.assertEqual(row.reason, 'Looks right.')

    def test_rejection_records_a_rejected_event(self):
        from approvals import services as approval_services

        receipt = _receipt('RC-W-4', self.creator)
        services.verify_receipt(receipt.pk, self.verifier)

        request = receipt.approvals.first()
        approval_services.reject(
            request_id=request.pk, user=self.approver, remarks='Short by 50.')

        rejected = _rows(receipt).filter(action=Action.REJECTED)
        self.assertEqual(rejected.count(), 1)
        self.assertEqual(rejected.first().reason, 'Short by 50.')
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.REJECTED)

    # 1 + 2 — creation and edit are business events, not STATUS_CHANGED.
    def test_create_and_edit_use_business_actions(self):
        client = APIClient()
        client.force_authenticate(self.creator)
        receipt = _receipt('RC-W-5', self.creator)
        services.log_status(receipt, to_status='DRAFT', user=self.creator,
                            action=Action.CREATED, reason='Receipt created.')

        resp = client.patch(
            reverse('payment-receipt-detail', args=[receipt.pk]),
            {'remarks': 'Corrected note.'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)

        actions = list(_rows(receipt).values_list('action', flat=True))
        self.assertIn(Action.CREATED, actions)
        self.assertIn(Action.UPDATED, actions)
        self.assertNotIn(Action.STATUS_CHANGED, actions)

class LifecycleSequenceTests(TestCase):
    """The WHOLE timeline, in order, with no generic rows between events.

    Asserted as a SEQUENCE rather than as "contains X": the defect these
    guard against was an extra anonymous STATUS_CHANGED sitting between two
    real events, which a membership check cannot see.
    """

    def setUp(self):
        # Everything the posting path reads from SAP is stubbed: the G/L
        # account per tender, the document series and the branch. All are
        # payload preparation, unreachable from a test box, and none of them
        # is what these tests are about — the HISTORY the posting writes is.
        for target, value in (
            ('payments.services._bank_accounts_for', {'CASH': '100001'}),
            ('payments.hana_queries.fetch_incoming_payment_series', 1),
            ('payments.services.resolve_bpl_id', 1),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        from approvals.models import (
            ApprovalLevel,
            ApprovalLevelApprover,
            ApprovalWorkflow,
        )
        # The poster resolves the SAP company database from this mapping.
        self.creator = _user('seq_creator', [PAYMENTS_CREATE])
        self.verifier = _user('seq_verifier', [PAYMENTS_VERIFY])
        self.approver = _user('seq_approver', [PAYMENTS_APPROVE])
        workflow = ApprovalWorkflow.objects.create(
            code='PAYMENT_OIL_SEQ', name='OIL payments',
            document_type='PAYMENT', company='OIL')
        level = ApprovalLevel.objects.create(
            workflow=workflow, sequence=1, name='Accountant Review')
        ApprovalLevelApprover.objects.create(
            level=level, user=self.approver, company='OIL')

    def _actions(self, receipt):
        return [str(a) for a in
                _rows(receipt).values_list('action', flat=True)]

    def test_verify_records_pending_approval_not_status_changed(self):
        receipt = _receipt('RC-SEQ-1', self.creator)
        services.log_status(receipt, to_status='DRAFT', user=self.creator,
                            action=Action.CREATED.value, reason='Receipt created.')
        services.verify_receipt(receipt.pk, self.verifier)

        self.assertEqual(
            self._actions(receipt),
            [Action.CREATED.value, Action.VERIFIED.value, Action.PENDING_APPROVAL.value])
        receipt.refresh_from_db()
        self.assertEqual(receipt.status,
                         PaymentReceipt.Status.PENDING_APPROVAL)

    def test_sap_success_sequence(self):
        """CREATED -> VERIFIED -> PENDING_APPROVAL -> APPROVED -> started -> posted."""
        from approvals import services as approval_services

        receipt = _receipt('RC-SEQ-2', self.creator)
        services.log_status(receipt, to_status='DRAFT', user=self.creator,
                            action=Action.CREATED.value, reason='Receipt created.')
        services.verify_receipt(receipt.pk, self.verifier)

        request = receipt.approvals.first()
        with patch('payments.sap_poster.sap_post_payment') as post:
            post.return_value = {'DocEntry': 999, 'DocNum': 555, 'TransId': 77}
            # The SAP call is deferred to transaction.on_commit so a 5-second
            # posting does not hold the approval's row locks. A TestCase rolls
            # back and never commits, so the callbacks must be run explicitly.
            with self.captureOnCommitCallbacks(execute=True):
                approval_services.approve(request_id=request.pk,
                                          user=self.approver, remarks='ok')

        self.assertEqual(self._actions(receipt), [
            Action.CREATED.value, Action.VERIFIED.value, Action.PENDING_APPROVAL.value,
            Action.APPROVED.value, Action.SAP_POST_STARTED.value, Action.SAP_POSTED.value,
        ])
        self.assertNotIn(Action.STATUS_CHANGED.value, self._actions(receipt))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
        self.assertEqual(receipt.sap_doc_entry, 999)

    def test_sap_failure_sequence(self):
        """A SAP rejection reads as SAP_FAILED, with no anonymous row."""
        from approvals import services as approval_services

        from .sap_client import SapError

        receipt = _receipt('RC-SEQ-3', self.creator)
        services.log_status(receipt, to_status='DRAFT', user=self.creator,
                            action=Action.CREATED.value, reason='Receipt created.')
        services.verify_receipt(receipt.pk, self.verifier)

        request = receipt.approvals.first()
        with patch('payments.sap_poster.sap_post_payment') as post:
            post.side_effect = SapError('Posting period locked',
                                        status_code=400, sap_code='-4013')
            with self.captureOnCommitCallbacks(execute=True):
                approval_services.approve(request_id=request.pk,
                                          user=self.approver, remarks='ok')

        self.assertEqual(self._actions(receipt), [
            Action.CREATED.value, Action.VERIFIED.value, Action.PENDING_APPROVAL.value,
            Action.APPROVED.value, Action.SAP_POST_STARTED.value, Action.SAP_FAILED.value,
        ])
        self.assertNotIn(Action.STATUS_CHANGED.value, self._actions(receipt))
        receipt.refresh_from_db()
        # The state machine is untouched by this change.
        self.assertEqual(receipt.status, PaymentReceipt.Status.PENDING_ERROR)

    def test_retry_after_failure_appends_a_clean_second_attempt(self):
        """The exact sequence from the reported receipt."""
        from approvals import services as approval_services

        from .sap_client import SapError

        receipt = _receipt('RC-SEQ-4', self.creator)
        services.log_status(receipt, to_status='DRAFT', user=self.creator,
                            action=Action.CREATED.value, reason='Receipt created.')
        services.verify_receipt(receipt.pk, self.verifier)

        request = receipt.approvals.first()
        with patch('payments.sap_poster.sap_post_payment') as post:
            post.side_effect = SapError('Posting period locked',
                                        status_code=400, sap_code='-4013')
            with self.captureOnCommitCallbacks(execute=True):
                approval_services.approve(request_id=request.pk,
                                          user=self.approver, remarks='first')

        # The failure reopens the approval at its final rung; the approver
        # retries from their own queue. Existing behaviour, unchanged.
        request.refresh_from_db()
        with patch('payments.sap_poster.sap_post_payment') as post:
            post.return_value = {'DocEntry': 21971, 'DocNum': 826246664}
            with self.captureOnCommitCallbacks(execute=True):
                approval_services.approve(request_id=request.pk,
                                          user=self.approver, remarks='retry')

        self.assertEqual(self._actions(receipt), [
            Action.CREATED.value, Action.VERIFIED.value, Action.PENDING_APPROVAL.value,
            Action.APPROVED.value, Action.SAP_POST_STARTED.value, Action.SAP_FAILED.value,
            Action.APPROVED.value, Action.SAP_POST_STARTED.value, Action.SAP_POSTED.value,
        ])
        self.assertNotIn(Action.STATUS_CHANGED.value, self._actions(receipt))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.POSTED)
