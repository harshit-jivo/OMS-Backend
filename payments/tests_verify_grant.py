"""Granting `Payments_Verify` reaches `permissions[]` — the whole chain.

The web Permissions page writes `extra_pages` through
`PUT /auth/users/<id>/page-permissions/`. This asserts that a key written that
way survives every downstream step: the registry intersection in
`effective_keys`, the `permissions[]` the clients read, and
`payments.granted_keys`, which is what the verify endpoint actually enforces.

Written because a permission can be "granted" in the UI and still be inert —
`effective_keys` intersects with the registry, so a key missing from
`core/permission_registry.py` is silently dropped and the checkbox lies.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.permissions import effective_keys
from users.models import User, UserRole

from .permissions import (
    DEPOSIT_APPROVE,
    DEPOSIT_CREATE,
    PAYMENTS_APPROVE,
    PAYMENTS_CREATE,
    PAYMENTS_DASHBOARD,
    PAYMENTS_VERIFY,
    granted_keys,
)


class VerifyGrantChainTests(TestCase):
    def setUp(self):
        admin_role, _ = UserRole.objects.get_or_create(name='admin')
        staff_role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        self.admin = User.objects.create(
            username='vg_admin', name='Admin', role=admin_role)
        self.target = User.objects.create(
            username='vg_target', name='Target', role=staff_role,
            extra_pages=[])
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = reverse('user-page-permissions', args=[self.target.id])

    def _grant(self, keys):
        return self.client.put(self.url, {'extra_pages': keys}, format='json')

    # CASE 4 + 8 — the admin ticks the box, and the key reaches permissions[].
    def test_granting_verify_reaches_effective_keys(self):
        resp = self._grant([PAYMENTS_VERIFY])
        self.assertEqual(resp.status_code, 200, resp.data)

        self.target.refresh_from_db()
        self.assertIn(PAYMENTS_VERIFY, self.target.extra_pages)
        # The registry intersection is the step that would silently drop an
        # unregistered key, so this is the assertion that matters.
        self.assertIn(PAYMENTS_VERIFY, effective_keys(self.target))
        # ...and the check the verify endpoint itself performs.
        self.assertIn(PAYMENTS_VERIFY, granted_keys(self.target))

    # CASE 5 — reload shows it still granted.
    def test_grant_survives_a_reload(self):
        self._grant([PAYMENTS_VERIFY])
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(PAYMENTS_VERIFY, str(resp.data))

    # CASE 6 + 9 — unticking removes it everywhere.
    def test_revoking_verify_removes_it(self):
        self._grant([PAYMENTS_VERIFY])
        # Reload: the grant was written through the API, so this in-memory
        # instance still holds the pre-grant `extra_pages`.
        self.target.refresh_from_db()
        self.assertIn(PAYMENTS_VERIFY, effective_keys(self.target))

        resp = self._grant([])
        self.assertEqual(resp.status_code, 200, resp.data)

        self.target.refresh_from_db()
        self.assertNotIn(PAYMENTS_VERIFY, self.target.extra_pages)
        self.assertNotIn(PAYMENTS_VERIFY, effective_keys(self.target))
        self.assertNotIn(PAYMENTS_VERIFY, granted_keys(self.target))

    # CASE 10 — the other five are unaffected, and each stays independent.
    def test_the_other_five_still_work(self):
        others = [PAYMENTS_CREATE, PAYMENTS_APPROVE, DEPOSIT_CREATE,
                  DEPOSIT_APPROVE, PAYMENTS_DASHBOARD]
        self._grant(others)

        self.target.refresh_from_db()
        keys = effective_keys(self.target)
        for key in others:
            self.assertIn(key, keys)
        # Granting the other five must NOT confer Verify.
        self.assertNotIn(PAYMENTS_VERIFY, keys)

    def test_verify_does_not_confer_the_others(self):
        self._grant([PAYMENTS_VERIFY])
        self.target.refresh_from_db()
        keys = granted_keys(self.target)
        self.assertEqual(keys, {PAYMENTS_VERIFY})

    def test_all_six_can_be_held_together(self):
        every = [PAYMENTS_CREATE, PAYMENTS_VERIFY, PAYMENTS_APPROVE,
                 DEPOSIT_CREATE, DEPOSIT_APPROVE, PAYMENTS_DASHBOARD]
        self._grant(every)
        self.target.refresh_from_db()
        self.assertEqual(granted_keys(self.target), set(every))

    # The registry is what the Role Permissions matrix renders from, so the
    # key must be published there too or that page cannot offer it either.
    def test_registry_endpoint_publishes_verify(self):
        resp = self.client.get(reverse('permission-registry'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(PAYMENTS_VERIFY, str(resp.data))

    def test_eligible_verifiers_names_only_explicit_holders(self):
        """The progress timeline names who to CHASE, not everyone who could act.

        Administrators hold every key implicitly, so including them listed most
        of the office and buried the two or three people actually assigned to
        the job. They can still verify — they are simply not the answer to
        "who do I go and ask".
        """
        from datetime import date
        from decimal import Decimal

        from .models import PaymentReceipt
        from .serializers import PaymentReceiptSerializer

        staff_role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        verifier = User.objects.create(
            username='ev_verifier', name='Explicit Verifier', role=staff_role,
            extra_pages=[PAYMENTS_VERIFY])
        creator = User.objects.create(
            username='ev_creator', name='Creator', role=staff_role,
            extra_pages=[PAYMENTS_VERIFY])

        receipt = PaymentReceipt.objects.create(
            receipt_no='RC-EV-1', company='OIL', card_code='C1',
            payment_date=date.today(), total_amount=Decimal('10.00'),
            created_by=creator,
            verification_status=PaymentReceipt.VerificationStatus.PENDING)

        names = {
            v['username']
            for v in PaymentReceiptSerializer(receipt, context={}).data[
                'eligible_verifiers']
        }
        self.assertIn('ev_verifier', names)
        # The admin from setUp holds the key implicitly — excluded.
        self.assertNotIn(self.admin.username, names)
        # The creator holds it explicitly but may not verify their own.
        self.assertNotIn('ev_creator', names)

    def test_eligible_verifiers_is_empty_once_verified(self):
        """Once done the answer is `verified_by`; candidates would mislead."""
        from datetime import date
        from decimal import Decimal

        from .models import PaymentReceipt
        from .serializers import PaymentReceiptSerializer

        receipt = PaymentReceipt.objects.create(
            receipt_no='RC-EV-2', company='OIL', card_code='C1',
            payment_date=date.today(), total_amount=Decimal('10.00'),
            created_by=self.target,
            verification_status=PaymentReceipt.VerificationStatus.VERIFIED)
        data = PaymentReceiptSerializer(receipt, context={}).data
        self.assertEqual(data['eligible_verifiers'], [])

    # §12 — the editor stays admin-only; the new key changes nothing there.
    def test_a_non_admin_cannot_grant_it(self):
        self.client.force_authenticate(self.target)
        resp = self._grant([PAYMENTS_VERIFY])
        self.assertIn(resp.status_code, (401, 403))
        self.target.refresh_from_db()
        self.assertNotIn(PAYMENTS_VERIFY, self.target.extra_pages or [])
