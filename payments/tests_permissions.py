"""Permissions come from grants and workflow assignment — never from the role.

A payments ROLE is identity ("this account works in payments"); it confers no
authority. That means one dummy role serves every payments user, and access is
decided by two things only:

  1. `extra_pages` — the boxes an admin ticks.
  2. Workflow assignment — whether they are an approver at a level.

These tests exist because a role -> permission map is an easy, plausible thing
to reintroduce, and it would silently override the admin's choices: a user
ticked for ONE action would be handed four by their role.
"""
from django.test import TestCase

from users.models import User, UserRole

from .permissions import (
    ACTION_PERMISSION_KEYS,
    DEPOSIT_APPROVE,
    DEPOSIT_CREATE,
    PAYMENTS_APPROVE,
    PAYMENTS_CREATE,
    granted_keys,
    has_permission_key,
)


class RoleConfersNothingTests(TestCase):
    def _user(self, username, role_name, extra_pages):
        role, _ = UserRole.objects.get_or_create(
            name=role_name, defaults={'display_name': role_name})
        return User.objects.create_user(
            username=username, password='pw', name=username, role=role,
            extra_pages=extra_pages)

    def test_a_payments_role_alone_grants_nothing(self):
        """The exact case that made roles misleading: a user carrying the role
        but no ticked boxes must have no permissions at all."""
        user = self._user('zz-role-only', 'payments_user', [])

        self.assertEqual(granted_keys(user), set())

    def test_the_role_name_does_not_change_the_answer(self):
        """A dummy role and an old specific role give identical access when the
        grants match — which is what makes one dummy role sufficient."""
        dummy = self._user('zz-dummy', 'payments_user', [DEPOSIT_APPROVE])
        legacy = self._user('zz-legacy', 'payments_and_deposit',
                            [DEPOSIT_APPROVE])
        unrelated = self._user('zz-manager', 'manager', [DEPOSIT_APPROVE])

        self.assertEqual(granted_keys(dummy), {DEPOSIT_APPROVE})
        self.assertEqual(granted_keys(dummy), granted_keys(legacy))
        self.assertEqual(granted_keys(dummy), granted_keys(unrelated))

    def test_one_ticked_box_grants_exactly_one_permission(self):
        """A role map would have widened this to four. It must stay at one."""
        user = self._user('zz-one', 'payments_user', [PAYMENTS_CREATE])

        self.assertEqual(granted_keys(user), {PAYMENTS_CREATE})
        self.assertTrue(has_permission_key(user, PAYMENTS_CREATE))
        for other in (PAYMENTS_APPROVE, DEPOSIT_CREATE, DEPOSIT_APPROVE):
            self.assertFalse(has_permission_key(user, other))

    def test_unknown_keys_in_extra_pages_are_ignored(self):
        """extra_pages also holds page names, so only real action keys count."""
        user = self._user('zz-mixed', 'payments_user',
                          [PAYMENTS_CREATE, 'Some_Page', 'Nonsense'])

        self.assertEqual(granted_keys(user), {PAYMENTS_CREATE})

    def test_admin_still_holds_everything(self):
        admin = User.objects.create_superuser(
            username='zz-admin', password='pw', name='Admin')

        self.assertEqual(granted_keys(admin), set(ACTION_PERMISSION_KEYS))

    def test_no_role_permission_map_exists(self):
        """Guards the design decision itself: reintroducing a role map would
        make the role a third source of authority."""
        from payments import permissions

        self.assertFalse(hasattr(permissions, 'ROLE_PERMISSION_MAP'))


class DepositListFilterTests(TestCase):
    """The deposit list must honour the same filters as the receipt list.

    `approval_view` was accepted and silently IGNORED here, so an approver who
    picked "Pending" on Deposit Tracking got every deposit back — POSTED and
    PENDING_ERROR included. A filter that looks applied but is not is worse
    than one that visibly fails.
    """

    def setUp(self):
        from datetime import date
        from decimal import Decimal

        from payments.models import BankDeposit, CollectionPerson

        role, _ = UserRole.objects.get_or_create(
            name='payments_user', defaults={'display_name': 'Payments'})
        self.user = User.objects.create_user(
            username='zz-dep-filter', password='pw', name='Dep',
            role=role, extra_pages=[DEPOSIT_CREATE])
        person = CollectionPerson.objects.create(
            name='P', code='ZZDEPF', company='OIL')

        self.statuses = ['PENDING_APPROVAL', 'POSTED', 'REJECTED']
        for index, status in enumerate(self.statuses):
            BankDeposit.objects.create(
                deposit_no=f'ZZ-DEP-{index}', company='OIL',
                deposit_date=date(2026, 8, 13), deposited_by=person,
                bank_gl_account='1104107', bank_key='ICICI:1104107',
                collected_amount=Decimal('100'),
                deposit_amount=Decimal('100'),
                status=status, created_by=self.user)

    def _get(self, query):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get(f'/api/payments/deposits/?{query}')
        data = response.json().get('data') or {}
        rows = (data.get('results') if isinstance(data, dict) else []) or []
        return {row['status'] for row in rows}

    def test_pending_filter_excludes_completed(self):
        """The reported bug: POSTED showed up under 'Pending'."""
        statuses = self._get(
            'status=PENDING_APPROVAL,APPROVED,POSTING_TO_SAP&mine=true')

        self.assertEqual(statuses, {'PENDING_APPROVAL'})
        self.assertNotIn('POSTED', statuses)

    def test_completed_filter_returns_only_posted(self):
        self.assertEqual(self._get('status=POSTED&mine=true'), {'POSTED'})

    def test_approval_view_is_honoured_not_ignored(self):
        """A user who can act on nothing must get an EMPTY list — previously
        the parameter was dropped and every deposit came back."""
        self.assertEqual(self._get('approval_view=awaiting_me'), set())

    def test_group_by_status_does_not_filter(self):
        """'All' orders the list; it must not drop rows."""
        self.assertEqual(
            self._get('group_by_status=true&mine=true'), set(self.statuses))
