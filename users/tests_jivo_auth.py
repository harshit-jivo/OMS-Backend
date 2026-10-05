"""Signing in with Jivo Auth, end to end on OMS's side.

Requests carry a Jivo access token; `JivoJWTAuthentication` verifies it and
hands the view the OMS row whose `auth_id` is the token's subject. These tests
pin what the switch must guarantee: the same row (so the same primary key and
business records) as before, refusals for tokens that don't belong, the admin
no longer accepting OMS passwords, and the "add a Jivo user" flows that
replaced creating users with a password. See docs/jivo-auth-integration.md.

Tokens are real RS256 tokens signed with a throwaway key
(`users.jivo_test_tokens`); `AuthClient` is mocked. Nothing here calls
auth.jivo.in.

Run with::

    python manage.py test users.tests_jivo_auth --settings=OMS.test_settings
"""
import time
import uuid
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from jivo_auth.exceptions import AuthServiceError, AuthServiceUnavailable
from sap_sync.models import Party
from users.jivo_test_tokens import make_access_token, use_test_keys
from users.models import State, User, UserPartyAssignment, UserRole


def _role(name):
    role, _ = UserRole.objects.get_or_create(
        name=name, defaults={'display_name': name.title(), 'is_active': True})
    return role


def _user(username, role_name=None, **kwargs):
    return User.objects.create_user(
        username=username, password='CorrectHorse9!', name=username,
        role=_role(role_name) if role_name else None, **kwargs)


def _jivo(email, auth_id=None, first='', last=''):
    """A user as `AuthClient().get_users()` returns them."""
    return {'id': str(auth_id or uuid.uuid4()), 'email': email, 'first_name': first,
            'last_name': last, 'employee_code': '', 'is_active': True}


def _client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


@use_test_keys
class JivoTokenTests(TestCase):

    def setUp(self):
        self.priya = _user('priya', 'salesman', email='priya@jivo.in', auth_id=uuid.uuid4())
        UserPartyAssignment.objects.create(user=self.priya, card_code='CUSTA000123', is_active=True)
        Party.objects.create(card_code='CUSTA000123', card_name='Test Party')

    def token_for(self, user, **claims):
        return make_access_token(user_id=user.auth_id, email=user.email, **claims)

    def profile(self, token):
        return _client(token).get(reverse('profile'))

    def test_a_linked_users_token_is_that_same_oms_row(self):
        res = self.profile(self.token_for(self.priya))

        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()['data']
        self.assertEqual(data['id'], self.priya.pk)
        self.assertEqual(data['auth_id'], str(self.priya.auth_id))
        self.assertEqual(data['role'], 'salesman')

    def test_their_business_records_come_with_them(self):
        res = _client(self.token_for(self.priya)).get(
            reverse('user-parties', args=[self.priya.pk]))

        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn('CUSTA000123', res.content.decode())

    def test_a_token_without_oms_access_is_403(self):
        res = self.profile(self.token_for(self.priya, apps=['some-other-app']))
        self.assertEqual(res.status_code, 403)

    def test_an_expired_token_is_401(self):
        now = int(time.time())
        res = self.profile(self.token_for(self.priya, iat=now - 3600, exp=now - 600))
        self.assertEqual(res.status_code, 401)

    def test_a_token_signed_by_another_key_is_401(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        forged = jwt.encode({
            'token_type': 'access', 'sub': str(self.priya.auth_id), 'email': self.priya.email,
            'apps': ['oms'], 'iss': 'https://auth.jivo.in', 'iat': now, 'exp': now + 900,
        }, other_key, algorithm='RS256')
        self.assertEqual(self.profile(forged).status_code, 401)

    def test_a_refresh_token_is_not_an_access_token(self):
        res = self.profile(self.token_for(self.priya, token_type='refresh'))
        self.assertEqual(res.status_code, 401)

    def test_a_locally_deactivated_user_is_401(self):
        self.priya.is_active = False
        self.priya.save(update_fields=['is_active'])
        self.assertEqual(self.profile(self.token_for(self.priya)).status_code, 401)

    def test_first_sign_in_links_the_unlinked_row_with_that_email(self):
        """Decision D4: a user the import skipped is linked, not duplicated."""
        old = _user('old-timer', 'manager', email='Old.Timer@jivo.in')
        sub = uuid.uuid4()

        res = self.profile(make_access_token(user_id=sub, email='old.timer@jivo.in'))

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['data']['id'], old.pk)
        old.refresh_from_db()
        self.assertEqual(old.auth_id, sub)
        # Jivo Auth's lowercased email replaces the local spelling.
        self.assertEqual(old.email, 'old.timer@jivo.in')
        self.assertEqual(old.role.name, 'manager')

    def test_first_sign_in_with_no_matching_row_creates_one_with_no_role(self):
        before = User.objects.count()

        res = self.profile(make_access_token(email='new.person@jivo.in'))

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(User.objects.count(), before + 1)
        created = User.objects.get(pk=res.json()['data']['id'])
        self.assertIsNone(created.role)
        self.assertFalse(created.has_usable_password())

    def test_a_linked_row_is_never_claimed_by_email(self):
        res = self.profile(make_access_token(email=self.priya.email))

        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotEqual(res.json()['data']['id'], self.priya.pk)
        self.priya.refresh_from_db()
        self.assertIsNotNone(self.priya.auth_id)

    def test_the_audit_log_names_the_token_holder(self):
        from audit.middleware import _resolve_user

        request = RequestFactory().post(
            '/api/auth/roles/create/', HTTP_AUTHORIZATION=f'Bearer {self.token_for(self.priya)}')

        self.assertEqual(_resolve_user(request), self.priya)


# The admin pages render `{% static %}`; the manifest storage settings.py uses
# needs a `collectstatic` run that tests don't do.
@override_settings(STORAGES={
    **settings.STORAGES,
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class AdminSignInTests(TestCase):

    def setUp(self):
        self.boss = _user('boss', 'admin', email='boss@jivo.in', auth_id=uuid.uuid4(),
                          is_staff=True, is_superuser=True)

    def test_only_jivo_auth_checks_passwords(self):
        """ModelBackend would accept every migrated row's old OMS hash."""
        self.assertEqual(settings.AUTHENTICATION_BACKENDS,
                         ['jivo_auth.backends.JivoAuthBackend'])

    def test_an_old_oms_password_does_not_open_the_admin(self):
        with mock.patch('jivo_auth.backends.AuthClient') as client:
            client.return_value.login.side_effect = AuthServiceError(
                401, {'detail': 'No active account found.'})
            res = self.client.post(reverse('admin:login'), {
                'username': 'boss@jivo.in', 'password': 'CorrectHorse9!',
                'next': '/admin/'})

        self.assertEqual(res.status_code, 200)  # the form again, with an error
        self.assertNotIn('_auth_user_id', self.client.session)
        client.return_value.login.assert_called_once()

    def test_the_admin_sign_in_asks_for_the_email(self):
        res = self.client.get(reverse('admin:login'))
        self.assertContains(res, 'Email')

    def test_the_admin_cannot_set_a_password(self):
        self.client.force_login(self.boss, backend='jivo_auth.backends.JivoAuthBackend')
        url = f'/admin/users/user/{self.boss.pk}/password/'

        res = self.client.post(url, {'password1': 'Attacker9Pass!', 'password2': 'Attacker9Pass!'})

        self.assertNotEqual(res.status_code, 200)
        self.assertNotContains(self.client.get(url, follow=True), 'password1')
        self.boss.refresh_from_db()
        self.assertFalse(self.boss.check_password('Attacker9Pass!'))

    def test_users_are_not_added_in_the_admin(self):
        self.client.force_login(self.boss, backend='jivo_auth.backends.JivoAuthBackend')
        self.assertEqual(self.client.get('/admin/users/user/add/').status_code, 403)


class _JivoDirectoryTestCase(TestCase):
    """An admin, and Jivo Auth's user list stood in for."""

    def setUp(self):
        self.admin = _user('t-admin', 'admin')
        self.staff = _user('t-staff', 'salesman')
        self.as_admin = APIClient()
        self.as_admin.force_authenticate(self.admin)
        self.as_staff = APIClient()
        self.as_staff.force_authenticate(self.staff)
        self.new_hire = _jivo('new.hire@jivo.in', first='New', last='Hire')
        self.jivo_users = [self.new_hire]
        patcher = mock.patch('users.jivo.AuthClient')
        self.auth_client = patcher.start()
        self.addCleanup(patcher.stop)

        def get_users(ids=None):
            if ids is None:
                return self.jivo_users
            return [u for u in self.jivo_users if u['id'] in {str(i) for i in ids}]

        self.auth_client.return_value.get_users.side_effect = get_users


class AddJivoUserTests(_JivoDirectoryTestCase):

    def create(self, **body):
        return self.as_admin.post(reverse('create-user'), {
            'auth_id': self.new_hire['id'], **body}, format='json')

    def test_creates_the_row_from_jivo_with_the_oms_fields_given(self):
        state = State.objects.create(name='Punjab', code='PB')

        res = self.create(role=_role('salesman').pk, phone='9876543210', states=[state.pk])

        self.assertEqual(res.status_code, 201, res.content)
        user = User.objects.get(auth_id=self.new_hire['id'])
        self.assertEqual(res.json()['data']['id'], user.pk)
        self.assertEqual(user.email, 'new.hire@jivo.in')
        self.assertEqual(user.username, 'new.hire@jivo.in')
        self.assertEqual(user.name, 'New Hire')
        self.assertEqual(user.role.name, 'salesman')
        self.assertEqual(user.phone, '9876543210')
        self.assertEqual(list(user.states.all()), [state])
        self.assertFalse(user.has_usable_password())

    def test_takes_no_password_name_or_email(self):
        res = self.create(password='CorrectHorse9!', name='Someone Else', email='x@jivo.in')

        self.assertEqual(res.status_code, 201, res.content)
        user = User.objects.get(auth_id=self.new_hire['id'])
        self.assertFalse(user.has_usable_password())
        self.assertEqual((user.name, user.email), ('New Hire', 'new.hire@jivo.in'))

    def test_refuses_a_jivo_user_without_oms_access(self):
        self.jivo_users = []

        res = self.create()

        self.assertEqual(res.status_code, 400)
        self.assertIn('auth_id', res.json()['errors'])
        self.assertFalse(User.objects.filter(email='new.hire@jivo.in').exists())

    def test_refuses_someone_already_in_oms(self):
        existing = _user('hire', email='new.hire@jivo.in', auth_id=self.new_hire['id'])

        res = self.create()

        self.assertEqual(res.status_code, 400)
        self.assertIn(str(existing.pk), str(res.json()['errors']['auth_id']))

    def test_refuses_when_an_unlinked_row_already_has_that_email(self):
        """Edit that row instead: it is linked at the person's first sign-in."""
        old = _user('old-hire', email='New.Hire@jivo.in', is_active=False)

        res = self.create()

        self.assertEqual(res.status_code, 400)
        self.assertIn('Edit that user', str(res.json()['errors']['auth_id']))
        old.refresh_from_db()
        self.assertIsNone(old.auth_id)

    def test_jivo_auth_unreachable_is_a_503(self):
        self.auth_client.return_value.get_users.side_effect = AuthServiceUnavailable('down')

        res = self.create()

        self.assertEqual(res.status_code, 503)
        self.assertFalse(res.json()['success'])

    def test_a_privileged_role_is_refused_before_asking_jivo(self):
        """A non-admin never reaches the serializer (IsAdminRole), but the
        serializer's own guard still runs first: no Jivo call to refuse it."""
        from users.serializers import AddJivoUserSerializer

        request = mock.Mock(user=self.staff)
        serializer = AddJivoUserSerializer(
            data={'auth_id': self.new_hire['id'], 'role': _role('admin').pk},
            context={'request': request})

        self.assertFalse(serializer.is_valid())
        self.assertIn('role', serializer.errors)
        self.auth_client.return_value.get_users.assert_not_called()


class JivoDirectoryTests(_JivoDirectoryTestCase):

    def test_lists_jivo_users_with_the_oms_row_each_has(self):
        linked = _jivo('priya@jivo.in', first='Priya')
        self.jivo_users = [self.new_hire, linked]
        priya = _user('priya', email='priya@jivo.in', auth_id=linked['id'])

        res = self.as_admin.get(reverse('jivo-users'))

        self.assertEqual(res.status_code, 200, res.content)
        rows = {row['email']: row for row in res.json()['data']}
        self.assertEqual(rows['priya@jivo.in']['oms_user_id'], priya.pk)
        self.assertIsNone(rows['new.hire@jivo.in']['oms_user_id'])
        self.assertEqual(rows['new.hire@jivo.in']['name'], 'New Hire')

    def test_is_for_administrators_only(self):
        self.assertEqual(self.as_staff.get(reverse('jivo-users')).status_code, 403)


class TrackerJivoUserTests(_JivoDirectoryTestCase):

    def setUp(self):
        super().setUp()
        self.tracker_admin = _user('t-tracker-admin', 'tracker_admin')
        _role('tracker_entry')
        self.as_tracker_admin = APIClient()
        self.as_tracker_admin.force_authenticate(self.tracker_admin)

    def test_adds_a_jivo_user_under_a_tracker_role(self):
        res = self.as_tracker_admin.post(reverse('tracker-admin-tracker-users'), {
            'auth_id': self.new_hire['id'], 'role': 'tracker_entry', 'phone': '9876543210',
        }, format='json')

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['auth_id'], self.new_hire['id'])
        user = User.objects.get(auth_id=self.new_hire['id'])
        self.assertEqual(user.role.name, 'tracker_entry')
        self.assertEqual(user.created_by, self.tracker_admin)
        self.assertFalse(user.has_usable_password())

    def test_refuses_someone_already_in_oms(self):
        _user('hire', 'manager', email='new.hire@jivo.in', auth_id=self.new_hire['id'])

        res = self.as_tracker_admin.post(reverse('tracker-admin-tracker-users'), {
            'auth_id': self.new_hire['id'], 'role': 'tracker_entry'}, format='json')

        self.assertEqual(res.status_code, 400)
        self.assertIn('already has an OMS user', res.json()['detail'])

    def test_an_edit_ignores_name_email_and_password(self):
        user = _user('entry', 'tracker_entry', email='entry@jivo.in', auth_id=uuid.uuid4())

        res = self.as_tracker_admin.patch(
            reverse('tracker-admin-tracker-user', args=[user.pk]),
            {'name': 'Renamed', 'email': 'x@jivo.in', 'password': 'Attacker9Pass!',
             'phone': '9876543210'}, format='json')

        self.assertEqual(res.status_code, 200, res.content)
        user.refresh_from_db()
        self.assertEqual((user.name, user.email), ('entry', 'entry@jivo.in'))
        self.assertTrue(user.check_password('CorrectHorse9!'))
        self.assertEqual(user.phone, '9876543210')

    def test_lists_the_jivo_directory_for_tracker_admins(self):
        res = self.as_tracker_admin.get(reverse('tracker-admin-jivo-users'))

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([row['email'] for row in res.json()], ['new.hire@jivo.in'])
