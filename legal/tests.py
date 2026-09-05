"""The Legal module's endpoint gate (`views.LegalEndpointGate`).

Legal was the one desk with no grantable permission: every endpoint accepted
any authenticated user, and the frontend's role check on /Label_Checker was
the only thing standing in the way. These tests pin the new gate from the
outside — through the URLs, not the permission class in isolation — because
the failure they guard against is a view quietly dropping the mixin.

Four callers matter:

  * an unrelated authenticated user -> denied (the tightening itself)
  * the `legal` role                -> admitted (the transitional fallback)
  * a user ticked for `Legal`       -> admitted (the point of the change)
  * an admin                        -> admitted (admins hold every key)
"""
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import User, UserRole

from .models import LabelItem
from .views import LEGAL_PAGE_KEY

ITEMS_URL = '/api/legal/item/'


class LegalEndpointGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        legal_role, _ = UserRole.objects.get_or_create(
            name='legal', defaults={'display_name': 'legal'})
        billing_role, _ = UserRole.objects.get_or_create(
            name='billing', defaults={'display_name': 'billing'})

        cls.outsider = User.objects.create_user(
            username='zz-outsider', password='pw', name='Outsider',
            role=billing_role)
        cls.legal_primary = User.objects.create_user(
            username='zz-legal', password='pw', name='Legal',
            role=legal_role)
        # The iron rule (users/models.py): a role held via extra_roles must
        # count everywhere a primary role does.
        cls.legal_extra = User.objects.create_user(
            username='zz-legal-extra', password='pw', name='Legal Extra',
            role=billing_role)
        cls.legal_extra.extra_roles.add(legal_role)
        cls.granted = User.objects.create_user(
            username='zz-granted', password='pw', name='Granted',
            role=billing_role, extra_pages=[LEGAL_PAGE_KEY])
        cls.admin = User.objects.create_superuser(
            username='zz-admin', password='pw', name='Admin')

    def _client(self, user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client

    def test_anonymous_is_denied(self):
        self.assertEqual(self._client().get(ITEMS_URL).status_code, 401)

    def test_an_unrelated_authenticated_user_is_denied(self):
        """The tightening itself: `IsAuthenticated` alone no longer admits."""
        client = self._client(self.outsider)
        self.assertEqual(client.get(ITEMS_URL).status_code, 403)
        self.assertEqual(
            client.post(ITEMS_URL, {'item_name': 'x'}).status_code, 403)

    def test_the_legal_role_still_admits(self):
        """The HasKeyOrRole fallback — no desk breaks before the bundle is
        ticked on the Role Permissions matrix."""
        response = self._client(self.legal_primary).get(ITEMS_URL)
        self.assertEqual(response.status_code, 200)

    def test_legal_held_as_an_extra_role_admits(self):
        response = self._client(self.legal_extra).get(ITEMS_URL)
        self.assertEqual(response.status_code, 200)

    def test_a_ticked_legal_grant_admits_without_the_role(self):
        """The point of the change: `Legal` is a box an admin can tick, so a
        user of any role can be granted the desk."""
        response = self._client(self.granted).get(ITEMS_URL)
        self.assertEqual(response.status_code, 200)

    def test_admin_admits(self):
        response = self._client(self.admin).get(ITEMS_URL)
        self.assertEqual(response.status_code, 200)

    def test_the_gate_covers_the_detail_routes_too(self):
        item = LabelItem.objects.create(item_name='Olive Oil 1L')
        url = f'{ITEMS_URL}{item.id}/'
        self.assertEqual(self._client(self.outsider).get(url).status_code, 403)
        self.assertEqual(
            self._client(self.legal_primary).get(url).status_code, 200)
