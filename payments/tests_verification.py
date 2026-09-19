"""Payment verification / handover - the gate between creation and approval.

Covers the permission key, the queue filter, the submit guard, self- and
duplicate-verification, the material-edit reset, and that verification enters
the EXISTING approval path rather than a parallel one.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.permissions import effective_keys
from users.models import User, UserRole

from . import services
from .models import (
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
)
from .permissions import (PAYMENTS_APPROVE, PAYMENTS_CREATE,
                          PAYMENTS_VERIFY)


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role,
        extra_pages=list(keys))


#: Fixture receipts are dated in a year no real row occupies. This suite runs
#: against the shared TEST database, and the list assertions below need to know
#: which receipts are theirs; a date window is the endpoint's own way of saying
#: so, and `date.today()` is not usable because real receipts are raised today.
FIXTURE_DATE = date(2035, 6, 1)
FIXTURE_WINDOW = '?date_from=2035-01-01&date_to=2035-12-31'


def _receipt(no, creator, *, amount='100.00', verification='PENDING',
             status=PaymentReceipt.Status.DRAFT, company='OIL'):
    # An advance with a branch: `validate_receipt` then passes without invoice
    # allocations. This suite is about the verification gate, not about
    # allocation or branch rules, which have their own tests.
    receipt = PaymentReceipt.objects.create(
        receipt_no=no, company=company, card_code='CUST1',
        payment_date=FIXTURE_DATE, total_amount=Decimal(amount),
        is_advance=True, sap_branch_id=1,
        status=status, verification_status=verification,
        created_by=creator)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.CASH,
        amount=Decimal(amount))
    return receipt


class _NoSapMixin:
    """Everything a real submission needs, minus SAP.

    Two pieces of scaffolding, neither of them about verification:

    * The SAP bank-master lookup is stubbed. `validate_receipt` resolves a G/L
      account per tender by calling SAP, which is unreachable from a test box.
      Every other rule in `validate_receipt` still runs for real.
    * A one-rung approval workflow exists, because `submit_receipt` hands the
      document to the real approval engine and a fresh test database has no
      workflow. The engine is not modified or stubbed - these tests run through
      it, which is the point: verification must enter the EXISTING chain.
    """

    def setUp(self):
        super().setUp()
        patcher = patch('payments.services._bank_accounts_for',
                        return_value={'CASH': '100001'})
        patcher.start()
        self.addCleanup(patcher.stop)

        from .tests_workflow_fixtures import payments_workflow

        # One workflow per company, each with a single stage — the shape the
        # new engine allows. A receipt is routed by its document NUMBER, so
        # the query projects `doc_no`; see tests_workflow_fixtures.
        self.approver = _user('wf_approver', [PAYMENTS_APPROVE])
        self.stages_by_company = {}
        for company in ('OIL', 'MART'):
            _workflow, stages = payments_workflow(
                self.approver, company=company,
                code=f'PAYMENT_{company}_TEST', documents='receipts')
            self.stages_by_company[company] = stages


class VerificationPermissionTests(_NoSapMixin, TestCase):
    """The key exists, reaches permissions[], and gates the endpoint."""

    def setUp(self):
        super().setUp()
        self.creator = _user('v_creator', [PAYMENTS_CREATE])
        self.verifier = _user('v_verifier', [PAYMENTS_VERIFY])
        self.client = APIClient()

    def test_key_is_in_the_central_registry(self):
        """Not just payments/permissions.py - effective_keys intersects with
        the registry, so a key missing there is silently dropped."""
        from core.permission_registry import ALL_KEYS
        self.assertIn(PAYMENTS_VERIFY, ALL_KEYS)

    # 3
    def test_permissions_contains_verify_for_holder(self):
        self.assertIn(PAYMENTS_VERIFY, effective_keys(self.verifier))

    # 4
    def test_permissions_omits_verify_for_non_holder(self):
        self.assertNotIn(PAYMENTS_VERIFY, effective_keys(self.creator))

    def test_create_does_not_imply_verify(self):
        """The three payment capabilities are independent."""
        self.assertNotIn(PAYMENTS_VERIFY, effective_keys(self.creator))
        self.assertIn(PAYMENTS_CREATE, effective_keys(self.creator))

    # 1
    def test_verify_without_permission_is_403(self):
        receipt = _receipt('RC-P-1', self.creator)
        self.client.force_authenticate(self.creator)
        resp = self.client.post(
            reverse('payment-receipt-verify', args=[receipt.pk]))
        self.assertEqual(resp.status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'PENDING')

    # 2
    def test_verify_with_permission_is_reachable(self):
        receipt = _receipt('RC-P-2', self.creator)
        self.client.force_authenticate(self.verifier)
        resp = self.client.post(
            reverse('payment-receipt-verify', args=[receipt.pk]))
        self.assertNotIn(resp.status_code, (401, 403))


class VerificationDefaultTests(TestCase):
    # 5
    def test_new_receipt_is_pending(self):
        creator = _user('d_creator', [PAYMENTS_CREATE])
        self.assertEqual(_receipt('RC-D-1', creator).verification_status,
                         'PENDING')

    # 6
    def test_backfill_marks_pre_existing_receipts_verified(self):
        """The 0024 data migration, run against a row as it would have existed."""
        import importlib

        from django.apps import apps

        module = importlib.import_module(
            'payments.migrations.0024_backfill_verification_status')
        creator = _user('d_creator2', [PAYMENTS_CREATE])
        legacy = _receipt('RC-D-2', creator, status=PaymentReceipt.Status.POSTED)
        legacy.sap_doc_entry = 5001
        legacy.save(update_fields=['sap_doc_entry'])

        module.backfill_verified(apps, None)

        legacy.refresh_from_db()
        self.assertEqual(legacy.verification_status, 'VERIFIED')
        self.assertIsNone(legacy.verified_by)
        # Financially inert.
        self.assertEqual(legacy.sap_doc_entry, 5001)
        self.assertEqual(legacy.total_amount, Decimal('100.00'))
        self.assertEqual(legacy.status, PaymentReceipt.Status.POSTED)


class SubmitGuardTests(_NoSapMixin, TestCase):
    """The gate lives in submit_receipt - the one choke point."""

    def setUp(self):
        super().setUp()
        self.creator = _user('g_creator', [PAYMENTS_CREATE])

    # 14
    def test_unverified_receipt_cannot_be_submitted(self):
        receipt = _receipt('RC-G-1', self.creator)
        with self.assertRaises(ValidationError) as ctx:
            services.submit_receipt(receipt, self.creator)
        self.assertIn('must be verified', '; '.join(ctx.exception.messages))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PaymentReceipt.Status.DRAFT)

    # 15
    def test_verified_receipt_can_be_submitted(self):
        receipt = _receipt('RC-G-2', self.creator, verification='VERIFIED')
        services.submit_receipt(receipt, self.creator)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status,
                         PaymentReceipt.Status.PENDING_APPROVAL)

    # 16
    def test_rejected_retry_path_still_works_when_verified(self):
        receipt = _receipt('RC-G-3', self.creator, verification='VERIFIED',
                           status=PaymentReceipt.Status.REJECTED)
        services.submit_receipt(receipt, self.creator)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status,
                         PaymentReceipt.Status.PENDING_APPROVAL)

    # 17
    def test_pending_error_retry_path_still_works_when_verified(self):
        receipt = _receipt('RC-G-4', self.creator, verification='VERIFIED',
                           status=PaymentReceipt.Status.PENDING_ERROR)
        services.submit_receipt(receipt, self.creator)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status,
                         PaymentReceipt.Status.PENDING_APPROVAL)

    def test_status_message_wins_over_verification_message(self):
        """A posted receipt is told it is posted, not that it needs verifying."""
        receipt = _receipt('RC-G-5', self.creator,
                           status=PaymentReceipt.Status.POSTED)
        with self.assertRaises(ValidationError) as ctx:
            services.submit_receipt(receipt, self.creator)
        self.assertNotIn('must be verified', '; '.join(ctx.exception.messages))


class VerifyActionTests(_NoSapMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.creator = _user('a_creator', [PAYMENTS_CREATE])
        self.verifier = _user('a_verifier', [PAYMENTS_VERIFY])
        self.client = APIClient()
        self.client.force_authenticate(self.verifier)

    # 8, 9, 10, 11, 13
    def test_verify_sets_fields_and_submits(self):
        receipt = _receipt('RC-A-1', self.creator)
        resp = self.client.post(
            reverse('payment-receipt-verify', args=[receipt.pk]),
            {'verification_remarks': 'Cash counted, matches.'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)

        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'VERIFIED')
        self.assertEqual(receipt.verified_by_id, self.verifier.id)
        self.assertIsNotNone(receipt.verified_at)
        self.assertEqual(receipt.verification_remarks, 'Cash counted, matches.')
        # ...and it entered the EXISTING approval flow.
        self.assertEqual(receipt.status,
                         PaymentReceipt.Status.PENDING_APPROVAL)

    # 12
    def test_exactly_one_verified_history_row(self):
        receipt = _receipt('RC-A-2', self.creator)
        self.client.post(reverse('payment-receipt-verify', args=[receipt.pk]))
        rows = PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=receipt.pk,
            action=PaymentStatusHistory.Action.VERIFIED)
        self.assertEqual(rows.count(), 1)
        # By username, not a user FK: the FK was dropped in migration 0031
        # and the denormalised name is what the audit trail actually keeps.
        self.assertEqual(rows.first().changed_by_username,
                         self.verifier.username)

    # 7
    def test_creator_cannot_verify_own_receipt(self):
        both = _user('a_both', [PAYMENTS_CREATE, PAYMENTS_VERIFY])
        receipt = _receipt('RC-A-3', both)
        self.client.force_authenticate(both)
        resp = self.client.post(
            reverse('payment-receipt-verify', args=[receipt.pk]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('cannot verify a payment you created', str(resp.data))
        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'PENDING')
        self.assertEqual(receipt.status, PaymentReceipt.Status.DRAFT)

    # 21
    def test_already_verified_cannot_be_verified_again(self):
        receipt = _receipt('RC-A-4', self.creator, verification='VERIFIED')
        receipt.verified_by = _user('a_first')
        receipt.save(update_fields=['verified_by'])
        resp = self.client.post(
            reverse('payment-receipt-verify', args=[receipt.pk]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already been verified', str(resp.data))
        receipt.refresh_from_db()
        # The original verifier is NOT overwritten.
        self.assertEqual(receipt.verified_by.username, 'a_first')

    def test_receipt_already_in_sap_cannot_be_verified(self):
        receipt = _receipt('RC-A-5', self.creator)
        receipt.sap_doc_entry = 9001
        receipt.save(update_fields=['sap_doc_entry'])
        resp = self.client.post(
            reverse('payment-receipt-verify', args=[receipt.pk]))
        self.assertEqual(resp.status_code, 400)
        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'PENDING')

    def test_serializer_exposes_verification_fields(self):
        receipt = _receipt('RC-A-6', self.creator)
        self.client.post(reverse('payment-receipt-verify', args=[receipt.pk]))
        resp = self.client.get(
            reverse('payment-receipt-detail', args=[receipt.pk]))
        data = resp.data['data']
        self.assertEqual(data['verification_status'], 'VERIFIED')
        self.assertEqual(data['verified_by_username'], 'a_verifier')
        self.assertIsNotNone(data['verified_at'])


class AtomicityTests(_NoSapMixin, TestCase):
    """Verification and submission must not come apart."""

    def setUp(self):
        super().setUp()
        self.creator = _user('t_creator', [PAYMENTS_CREATE])
        self.verifier = _user('t_verifier', [PAYMENTS_VERIFY])

    # 17 (the transaction requirement)
    def test_failed_submission_rolls_back_the_verification(self):
        """If submit raises, nothing may remain marked VERIFIED."""
        receipt = _receipt('RC-T-1', self.creator)

        real_submit = services.submit_receipt

        def boom(*args, **kwargs):
            raise ValidationError('Simulated submission failure.')

        services.submit_receipt = boom
        try:
            with self.assertRaises(ValidationError):
                services.verify_receipt(receipt.pk, self.verifier)
        finally:
            services.submit_receipt = real_submit

        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'PENDING')
        self.assertIsNone(receipt.verified_by)
        self.assertIsNone(receipt.verified_at)
        # ...and no orphan history row claiming a verification that was undone.
        self.assertFalse(PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=receipt.pk,
            action=PaymentStatusHistory.Action.VERIFIED).exists())

    # 22 - serialized rather than threaded: SQLite (the test DB) has no real
    # row locking, so a thread test here would assert nothing about the lock.
    # What IS testable is that the second attempt reads the committed VERIFIED
    # state and refuses, which is the behaviour the lock exists to guarantee.
    def test_second_verification_attempt_is_refused(self):
        receipt = _receipt('RC-T-2', self.creator)
        second = _user('t_verifier2', [PAYMENTS_VERIFY])

        services.verify_receipt(receipt.pk, self.verifier)
        with self.assertRaises(ValidationError) as ctx:
            services.verify_receipt(receipt.pk, second)
        self.assertIn('already been verified', '; '.join(ctx.exception.messages))

        receipt.refresh_from_db()
        self.assertEqual(receipt.verified_by_id, self.verifier.id)
        # Exactly one VERIFIED event, and one submission.
        self.assertEqual(PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=receipt.pk,
            action=PaymentStatusHistory.Action.VERIFIED).count(), 1)
        # Exactly one flow, still open at its first stage.
        receipt.refresh_from_db()
        self.assertIsNotNone(getattr(receipt, 'flow', None))
        self.assertTrue(receipt.flow.is_open)


class MaterialEditTests(_NoSapMixin, TestCase):
    """A verified receipt whose figures change is no longer verified."""

    def setUp(self):
        super().setUp()
        self.creator = _user('m_creator', [PAYMENTS_CREATE])
        self.verifier = _user('m_verifier', [PAYMENTS_VERIFY])
        self.client = APIClient()

    def _patch(self, receipt, payload, user):
        self.client.force_authenticate(user)
        return self.client.patch(
            reverse('payment-receipt-detail', args=[receipt.pk]),
            payload, format='json')

    # 18
    def test_verifier_may_edit_a_pending_receipt(self):
        receipt = _receipt('RC-M-1', self.creator)
        resp = self._patch(receipt, {'remarks': 'Checked against cash box.'},
                           self.verifier)
        self.assertEqual(resp.status_code, 200, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.remarks, 'Checked against cash box.')

    # 19
    def test_existing_validation_still_rejects_a_bad_amount(self):
        """The pre-existing serializer validation is untouched by this work.

        A cash line with no denomination breakdown is refused exactly as it was
        before verification existed.
        """
        receipt = _receipt('RC-M-2', self.creator)
        resp = self._patch(receipt, {'methods': [
            {'method': 'CASH', 'amount': '500.00'}]}, self.verifier)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('denomination', str(resp.data).lower())

    # 20
    def test_material_edit_after_verification_resets_it(self):
        receipt = _receipt('RC-M-3', self.creator, verification='VERIFIED')
        receipt.verified_by = self.verifier
        receipt.save(update_fields=['verified_by'])

        resp = self._patch(receipt, {'methods': [{
            'method': 'CASH', 'amount': '250.00',
            'denominations': [{'denomination': 50, 'quantity': 5}],
        }]}, self.creator)
        self.assertEqual(resp.status_code, 200, resp.data)

        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'PENDING')
        self.assertIsNone(receipt.verified_by)
        self.assertIsNone(receipt.verified_at)
        self.assertEqual(receipt.total_amount, Decimal('250.00'))

    def test_immaterial_edit_leaves_verification_intact(self):
        """A remarks fix must not force a re-verification."""
        receipt = _receipt('RC-M-4', self.creator, verification='VERIFIED')
        receipt.verified_by = self.verifier
        receipt.save(update_fields=['verified_by'])

        resp = self._patch(receipt, {'remarks': 'Typo fixed.'}, self.creator)
        self.assertEqual(resp.status_code, 200, resp.data)

        receipt.refresh_from_db()
        self.assertEqual(receipt.verification_status, 'VERIFIED')
        self.assertEqual(receipt.verified_by_id, self.verifier.id)


class QueueFilterTests(TestCase):
    """?verification_status= on the EXISTING list endpoint."""

    def setUp(self):
        super().setUp()
        self.creator = _user('q_creator', [PAYMENTS_CREATE])
        self.verifier = _user('q_verifier', [PAYMENTS_VERIFY])
        self.pending = _receipt('RC-Q-1', self.creator)
        self.verified = _receipt('RC-Q-2', self.creator,
                                 verification='VERIFIED')
        self.other_company = _receipt('RC-Q-3', self.creator, company='MART')
        self.client = APIClient()
        self.client.force_authenticate(self.verifier)

    def _list(self, query=''):
        resp = self.client.get(reverse('payment-receipt-list') + query)
        self.assertEqual(resp.status_code, 200)
        return {r['receipt_no'] for r in resp.data['data']['results']}

    # 23
    def test_pending_filter(self):
        found = self._list('?verification_status=PENDING')
        self.assertIn('RC-Q-1', found)
        self.assertNotIn('RC-Q-2', found)

    # 24
    def test_verified_filter(self):
        found = self._list('?verification_status=VERIFIED')
        self.assertIn('RC-Q-2', found)
        self.assertNotIn('RC-Q-1', found)

    # 26
    def test_company_filter_still_applies_alongside(self):
        found = self._list('?verification_status=PENDING&company=MART')
        self.assertEqual(found, {'RC-Q-3'})

    # 25
    def test_pagination_envelope_is_unchanged(self):
        resp = self.client.get(
            reverse('payment-receipt-list') + FIXTURE_WINDOW
            + '&verification_status=PENDING')
        payload = resp.data['data']
        self.assertIn('results', payload)
        # Two of this suite's three fixtures are PENDING (RC-Q-1 and the MART
        # one); the window keeps the live queue out of the count.
        self.assertEqual(payload['pagination']['total'], 2)
        self.assertEqual(payload['pagination']['page'], 1)

    def test_no_filter_returns_everything(self):
        """The new parameter is opt-in - omitting it changes nothing."""
        self.assertEqual(self._list(FIXTURE_WINDOW),
                         {'RC-Q-1', 'RC-Q-2', 'RC-Q-3'})

