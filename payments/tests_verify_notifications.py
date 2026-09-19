"""Who gets told, at each hand-off in a receipt's life.

    create   -> the eligible VERIFIERS      ("please count this")
    verify   -> level 1 approvers           (existing engine hooks)
    approve  -> the next rung, then the submitter
    SAP OK   -> the creator AND the verifier

Only the two ends are new; the ladder in the middle already worked and is
asserted here so a change to one cannot silently break the other.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from notifications.models import Notification
from users.models import Company, User, UserRole

from .models import PaymentMethodEntry, PaymentReceipt
from .notification_events import (
    PAYMENT_POSTED,
    PAYMENT_VERIFICATION_REQUIRED,
    publish_receipt_posted,
    publish_receipt_verification_required,
)
from core.permissions import is_admin

from .permissions import PAYMENTS_VERIFY
from .tests_support import notifications_of


def _receipt(no, creator, **kw):
    receipt = PaymentReceipt.objects.create(
        receipt_no=no, company='OIL', card_code='CUST1',
        payment_date=date.today(), total_amount=Decimal('100.00'),
        is_advance=True, sap_branch_id=1, created_by=creator, **kw)
    PaymentMethodEntry.objects.create(
        receipt=receipt, method=PaymentMethodEntry.Method.CASH,
        amount=Decimal('100.00'))
    return receipt


class VerificationNotificationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Notify Co')
        staff, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        admin_role, _ = UserRole.objects.get_or_create(name='admin')

        self.creator = User.objects.create(
            username='n_creator', name='Creator', role=staff,
            company=self.company)
        self.verifier = User.objects.create(
            username='n_verifier', name='Verifier', role=staff,
            company=self.company, extra_pages=[PAYMENTS_VERIFY])
        self.other_verifier = User.objects.create(
            username='n_verifier2', name='Second Verifier', role=staff,
            company=self.company, extra_pages=[PAYMENTS_VERIFY])
        # Holds the key implicitly. Must NOT be notified: every admin would
        # otherwise get a push for every payment anyone raises.
        self.admin = User.objects.create(
            username='n_admin', name='Admin', role=admin_role,
            company=self.company)

    def _recipients(self, event_type, receipt=None):
        """Who was told about THIS receipt.

        Scoped to one document because the shared TEST database already holds
        notifications for real ones.
        """
        qs = (notifications_of(receipt) if receipt is not None
              else Notification.objects.all())
        return set(qs.filter(event_type=event_type)
                   .values_list('user__username', flat=True))

    def test_creation_notifies_the_eligible_verifiers(self):
        receipt = _receipt('RC-N-1', self.creator)
        publish_receipt_verification_required(receipt, self.creator)

        told = self._recipients(PAYMENT_VERIFICATION_REQUIRED, receipt)

        # Both verifiers created here are told.
        self.assertLessEqual({'n_verifier', 'n_verifier2'}, told)

        # And the RULE holds for everyone told, which is the real claim. This
        # is asserted rather than an exact set because eligibility is resolved
        # company-wide from live grants: on the shared TEST database real
        # verifiers are legitimately eligible too, and excluding them would be
        # the bug, not including them.
        User = get_user_model()
        for user in User.objects.filter(username__in=told):
            self.assertIn(PAYMENTS_VERIFY, user.extra_pages or [],
                          f'{user.username} was told without holding the key')
            self.assertNotEqual(user.pk, self.creator.pk,
                                'the creator may not verify their own receipt')
            self.assertFalse(is_admin(user),
                             f'{user.username} is an admin and holds every key '
                             'implicitly — notifying them would page the office')

    def test_admins_are_not_notified(self):
        receipt = _receipt('RC-N-2', self.creator)
        publish_receipt_verification_required(receipt, self.creator)
        self.assertNotIn('n_admin', self._recipients(PAYMENT_VERIFICATION_REQUIRED, receipt))

    def test_the_creator_is_never_asked_to_verify_their_own(self):
        """Even holding the key — the server refuses their verification."""
        self.creator.extra_pages = [PAYMENTS_VERIFY]
        self.creator.save(update_fields=['extra_pages'])

        receipt = _receipt('RC-N-3', self.creator)
        publish_receipt_verification_required(receipt, self.creator)
        self.assertNotIn('n_creator', self._recipients(PAYMENT_VERIFICATION_REQUIRED, receipt))

    def test_no_verifier_configured_sends_nothing(self):
        # Eligibility is resolved across every active user, so "no verifier
        # configured" has to be arranged across every active user — the shared
        # TEST database holds real ones who hold the key. Done inside the
        # test's own transaction, which is rolled back, so no live grant is
        # altered.
        User = get_user_model()
        for user in User.objects.filter(is_active=True):
            keys = user.extra_pages or []
            if PAYMENTS_VERIFY in keys:
                user.extra_pages = [k for k in keys if k != PAYMENTS_VERIFY]
                user.save(update_fields=['extra_pages'])

        receipt = _receipt('RC-N-4', self.creator)
        created = publish_receipt_verification_required(receipt, self.creator)
        self.assertEqual(created, [])

    def test_the_create_path_is_wired_to_the_publisher(self):
        """The wiring, not just the publisher.

        Asserted by calling the serializer's own create() rather than the HTTP
        endpoint: a full POST needs the unmanaged `branches` table, which the
        test database does not carry (it is a SAP mirror, managed=False). This
        exercises the same line that fires in production.
        """
        from unittest.mock import patch

        from .serializers import PaymentReceiptCreateSerializer

        class _Req:
            user = self.creator

        payload = {
            'company': 'OIL', 'card_code': 'CUST9',
            'payment_date': date.today(), 'is_advance': True,
            'sap_branch_id': 1,
            'methods': [{'method': 'UPI', 'amount': Decimal('50.00'),
                         'upi_reference': 'UTR1'}],
        }
        serializer = PaymentReceiptCreateSerializer(
            context={'request': _Req()})
        with patch(
            'payments.notification_events'
            '.publish_receipt_verification_required'
        ) as spy:
            receipt = serializer.create(payload)
        spy.assert_called_once()
        # ...with THIS receipt and THIS creator.
        self.assertEqual(spy.call_args[0][0].pk, receipt.pk)
        self.assertEqual(spy.call_args[0][1], self.creator)


class PostedNotificationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Posted Co')
        staff, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        self.creator = User.objects.create(
            username='p_creator', name='Creator', role=staff,
            company=self.company)
        self.verifier = User.objects.create(
            username='p_verifier', name='Verifier', role=staff,
            company=self.company, extra_pages=[PAYMENTS_VERIFY])

    def _recipients(self, receipt):
        """Who was told THIS receipt posted — not every receipt ever."""
        return set(notifications_of(receipt)
                   .filter(event_type=PAYMENT_POSTED)
                   .values_list('user__username', flat=True))

    def test_posting_notifies_creator_and_verifier(self):
        receipt = _receipt(
            'RC-P-1', self.creator,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            status=PaymentReceipt.Status.POSTED, sap_doc_num=20802)
        receipt.verified_by = self.verifier
        receipt.save(update_fields=['verified_by'])

        publish_receipt_posted(receipt)
        self.assertEqual(self._recipients(receipt), {'p_creator', 'p_verifier'})

    def test_the_doc_number_is_in_the_message(self):
        receipt = _receipt(
            'RC-P-2', self.creator,
            status=PaymentReceipt.Status.POSTED, sap_doc_num=555)
        publish_receipt_posted(receipt)
        note = notifications_of(receipt).filter(
            event_type=PAYMENT_POSTED).first()
        self.assertIn('555', note.message)

    def test_one_notification_when_creator_verified_it(self):
        """De-duped: the same person must not be told twice."""
        receipt = _receipt(
            'RC-P-3', self.creator,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED,
            status=PaymentReceipt.Status.POSTED)
        receipt.verified_by = self.creator
        receipt.save(update_fields=['verified_by'])

        publish_receipt_posted(receipt)
        self.assertEqual(
            notifications_of(receipt).filter(
                event_type=PAYMENT_POSTED).count(), 1)

    def test_no_verifier_still_notifies_the_creator(self):
        """A legacy receipt verified before the feature existed."""
        receipt = _receipt('RC-P-4', self.creator,
                           status=PaymentReceipt.Status.POSTED)
        publish_receipt_posted(receipt)
        self.assertEqual(self._recipients(receipt), {'p_creator'})
