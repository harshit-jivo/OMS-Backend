"""Authentication and authorization tests for `users`.

This app had ZERO tests while owning login, the user table and the party
assignments that decide which customers a salesperson can see. It is also the
app the security phase changes first, which is the worst possible combination:
no characterisation of what login does today, and an imminent rewrite of who
may reach every endpoint around it.

Two jobs here.

1. **Characterisation** — pin the behaviour that must NOT change: the login
   response shape both clients parse, the inactive-account refusal, the
   profile payload.
2. **Authorization** — assert 401/403 on every endpoint that used to be
   `AllowAny`. These are the regression tests for the privilege-escalation
   hole: `POST /auth/users/create/` accepted an anonymous request whose body
   named `role: <admin>`, so anyone reachable by the API could mint themselves
   an administrator.

Run with::

    python manage.py test users --settings=OMS.test_settings
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import identify_hasher
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from OMS.settings import _parse_bool
from users.models import User, UserPartyAssignment, UserRole


def _role(name):
    role, _ = UserRole.objects.get_or_create(
        name=name, defaults={'display_name': name.title(), 'is_active': True})
    return role


def _user(username, role_name=None, **kwargs):
    return User.objects.create_user(
        username=username, password='CorrectHorse9!', name=username,
        role=_role(role_name) if role_name else None, **kwargs)


class _ApiTestCase(TestCase):
    """Shared fixtures: one admin, one ordinary user, one anonymous client."""

    def setUp(self):
        self.admin = _user('t-admin', 'admin')
        self.staff = _user('t-staff', 'salesman')
        self.anon = APIClient()
        self.as_admin = APIClient()
        self.as_admin.force_authenticate(self.admin)
        self.as_staff = APIClient()
        self.as_staff.force_authenticate(self.staff)


# ---------------------------------------------------------------------------
# Characterisation — the login contract two clients depend on
# ---------------------------------------------------------------------------

class LoginContractTests(_ApiTestCase):
    """The response shape here is parsed by the React web client AND by
    `OMS-app` (React Native), which is not in this workspace and cannot be
    updated in lockstep. Changing these keys breaks a client nobody here can
    inspect."""

    def test_login_returns_both_tokens_and_the_user(self):
        """Pins the exact envelope, nesting included: the tokens live under
        `data.tokens`, NOT at `data` level, and carry `token_type` /
        `expires_in` alongside. Flattening this would break both clients."""
        res = self.anon.post(reverse('login'),
                             {'username': 't-staff', 'password': 'CorrectHorse9!'},
                             format='json')
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body['success'])
        data = body['data']
        self.assertEqual(data['user']['username'], 't-staff')
        tokens = data['tokens']
        self.assertIn('access', tokens)
        self.assertIn('refresh', tokens)
        self.assertEqual(tokens['token_type'], 'Bearer')
        self.assertEqual(tokens['expires_in'], 86400)

    def test_login_rejects_a_wrong_password(self):
        """401, not the 400 a DRF ValidationError would normally produce —
        `LoginView` translates it deliberately. Clients branch on this code."""
        res = self.anon.post(reverse('login'),
                             {'username': 't-staff', 'password': 'wrong'},
                             format='json')
        self.assertEqual(res.status_code, 401)

    def test_login_rejects_a_deactivated_account(self):
        """`DeleteUserView` is a soft delete — it only clears `is_active`. If
        that stopped blocking login, "removing" a user would remove nothing."""
        self.staff.is_active = False
        self.staff.save(update_fields=['is_active'])
        res = self.anon.post(reverse('login'),
                             {'username': 't-staff', 'password': 'CorrectHorse9!'},
                             format='json')
        self.assertEqual(res.status_code, 401)

    def test_login_stays_reachable_without_a_token(self):
        """The endpoint that MUST remain AllowAny. Asserted by a successful
        anonymous login rather than by a status code on an empty body — a bad
        body also answers 401 here, so that would not distinguish "rejected the
        credentials" from "rejected the anonymous caller"."""
        res = self.anon.post(reverse('login'),
                             {'username': 't-admin', 'password': 'CorrectHorse9!'},
                             format='json')
        self.assertEqual(res.status_code, 200)

    def test_refresh_stays_reachable_without_a_token(self):
        """A refresh carries its credential in the BODY, so requiring a bearer
        token here would make the endpoint unusable exactly when it is needed —
        after the access token expired."""
        login = self.anon.post(reverse('login'),
                               {'username': 't-staff', 'password': 'CorrectHorse9!'},
                               format='json').json()
        res = self.anon.post(reverse('token-refresh'),
                             {'refresh': login['data']['tokens']['refresh']},
                             format='json')
        self.assertEqual(res.status_code, 200)
        self.assertIn('access', res.json())

    def test_profile_requires_a_login_and_returns_the_caller(self):
        self.assertEqual(self.anon.get(reverse('profile')).status_code, 401)
        res = self.as_staff.get(reverse('profile'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['data']['username'], 't-staff')


class SerializerLeakTests(_ApiTestCase):
    """`UserSerializer` used to list `password` in `Meta.fields`. Being a
    ModelSerializer, that serialised the stored PBKDF2 hash into every response
    built from it — including the roster endpoint, which was AllowAny."""

    def test_no_endpoint_ever_returns_a_password_hash(self):
        for res in (self.as_staff.get(reverse('users-list')),
                    self.as_staff.get(reverse('profile')),
                    self.as_admin.get(reverse('user-detail', args=[self.staff.pk]))):
            self.assertEqual(res.status_code, 200)
            self.assertNotIn('password', res.content.decode())

    def test_passwords_are_still_stored_hashed(self):
        """Guards the other direction: the field is gone from the API, not
        from the model."""
        identify_hasher(self.staff.password)  # raises if stored in the clear
        self.assertTrue(self.staff.check_password('CorrectHorse9!'))


# ---------------------------------------------------------------------------
# Authorization — the endpoints that used to be AllowAny
# ---------------------------------------------------------------------------

class AnonymousAccessTests(_ApiTestCase):
    """Every one of these answered an unauthenticated request before Phase 1."""

    def test_user_management_refuses_anonymous_callers(self):
        cases = [
            ('post', reverse('create-user'), {'name': 'x', 'username': 'x',
                                              'password': 'CorrectHorse9!'}),
            ('get', reverse('user-detail', args=[self.staff.pk]), None),
            ('put', reverse('user-detail', args=[self.staff.pk]), {'name': 'y'}),
            ('post', reverse('delete-user', args=[self.staff.pk]), {}),
            ('get', reverse('users-list'), None),
        ]
        for method, url, payload in cases:
            call = getattr(self.anon, method)
            res = call(url, payload, format='json') if payload is not None else call(url)
            self.assertIn(res.status_code, (401, 403), f'{method} {url}')

    def test_party_assignment_refuses_anonymous_callers(self):
        """Party assignment IS the data-visibility boundary for salespeople."""
        cases = [
            ('post', reverse('assign-parties'), {'user_id': self.staff.pk,
                                                 'card_codes': ['C1']}),
            ('post', reverse('remove-party'), {'user_id': self.staff.pk,
                                               'card_code': 'C1'}),
            ('post', reverse('bulk-assign-users-parties'), {'rows': [{}]}),
            ('get', reverse('user-parties', args=[self.staff.pk]), None),
            ('get', reverse('party-users', args=['C1']), None),
        ]
        for method, url, payload in cases:
            call = getattr(self.anon, method)
            res = call(url, payload, format='json') if payload is not None else call(url)
            self.assertIn(res.status_code, (401, 403), f'{method} {url}')

    def test_master_data_refuses_anonymous_callers(self):
        for name in ('states', 'companies', 'mainGroup', 'categories', 'roles-list'):
            self.assertIn(self.anon.get(reverse(name)).status_code, (401, 403), name)


class PrivilegeEscalationTests(_ApiTestCase):
    """The §7.1 hole, in both of its halves.

    Half one is the endpoint: `CreateUserView` was AllowAny. Half two is the
    serializer: `role` was bound to `UserRole.objects.all()`, so the body could
    name ANY role including `admin`. Closing only the first leaves the second
    armed the moment account creation is ever delegated.
    """

    def test_anonymous_cannot_create_an_admin(self):
        res = self.anon.post(reverse('create-user'), {
            'name': 'Attacker', 'username': 'attacker',
            'password': 'CorrectHorse9!', 'role': _role('admin').pk,
        }, format='json')
        self.assertIn(res.status_code, (401, 403))
        self.assertFalse(User.objects.filter(username='attacker').exists())

    def test_a_non_admin_cannot_create_any_user(self):
        res = self.as_staff.post(reverse('create-user'), {
            'name': 'Sneak', 'username': 'sneak', 'password': 'CorrectHorse9!',
        }, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertFalse(User.objects.filter(username='sneak').exists())

    def test_an_admin_can_still_create_a_user(self):
        """The lockdown must not break the legitimate admin flow."""
        res = self.as_admin.post(reverse('create-user'), {
            'name': 'New Hire', 'username': 'newhire',
            'password': 'CorrectHorse9!', 'role': _role('salesman').pk,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(User.objects.filter(username='newhire').exists())

    def test_a_non_admin_cannot_reset_another_users_password(self):
        """`PUT /auth/users/<id>/` accepts `password`, so while it was AllowAny
        it was an account-takeover of any account, including an admin's."""
        res = self.as_staff.put(reverse('user-detail', args=[self.admin.pk]),
                                {'password': 'Attacker9Pass!'}, format='json')
        self.assertEqual(res.status_code, 403)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.check_password('Attacker9Pass!'))

    def test_a_non_admin_cannot_deactivate_anyone(self):
        res = self.as_staff.post(reverse('delete-user', args=[self.admin.pk]),
                                 {}, format='json')
        self.assertEqual(res.status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_an_admin_cannot_deactivate_themselves(self):
        """Not a security rule — a lockout guard. Disabling the last admin
        account leaves no way back in through the API."""
        res = self.as_admin.post(reverse('delete-user', args=[self.admin.pk]),
                                 {}, format='json')
        self.assertEqual(res.status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)


class PasswordPolicyTests(_ApiTestCase):
    """`CreateUserSerializer` declared `min_length=6` and called `set_password()`
    directly, which does not validate — so AUTH_PASSWORD_VALIDATORS, configured
    in settings, applied to nothing created through the API."""

    def test_a_common_password_is_rejected(self):
        res = self.as_admin.post(reverse('create-user'), {
            'name': 'Weak', 'username': 'weak', 'password': 'password123',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('password', res.json()['errors'])

    def test_a_short_password_is_rejected(self):
        res = self.as_admin.post(reverse('create-user'), {
            'name': 'Short', 'username': 'short', 'password': 'Ab3!x',
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_an_update_that_omits_the_password_still_works(self):
        """The validators must not fire on the "leave it alone" case — the edit
        form posts `password: ""` on every save."""
        res = self.as_admin.put(reverse('user-detail', args=[self.staff.pk]),
                                {'name': 'Renamed', 'password': ''}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.name, 'Renamed')
        self.assertTrue(self.staff.check_password('CorrectHorse9!'))


class ScopedAccessTests(_ApiTestCase):
    """Endpoints kept open to all authenticated users, but scoped to the caller."""

    def test_a_user_reads_their_own_party_assignments(self):
        UserPartyAssignment.objects.create(
            user=self.staff, card_code='C1', is_active=True)
        res = self.as_staff.get(reverse('user-parties', args=[self.staff.pk]))
        self.assertEqual(res.status_code, 200)

    def test_a_user_cannot_read_someone_elses_party_assignments(self):
        res = self.as_staff.get(reverse('user-parties', args=[self.admin.pk]))
        self.assertEqual(res.status_code, 403)

    def test_an_admin_reads_anyones_party_assignments(self):
        res = self.as_admin.get(reverse('user-parties', args=[self.staff.pk]))
        self.assertEqual(res.status_code, 200)

    def test_a_user_cannot_read_someone_elses_page_permissions(self):
        res = self.as_staff.get(
            reverse('user-page-permissions', args=[self.admin.pk]))
        self.assertEqual(res.status_code, 403)

    def test_a_non_admin_cannot_grant_page_permissions(self):
        res = self.as_staff.put(
            reverse('user-page-permissions', args=[self.staff.pk]),
            {'extra_pages': ['Payments_Dashboard']}, format='json')
        self.assertEqual(res.status_code, 403)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.extra_pages)


class LoginThrottleTests(_ApiTestCase):
    """Login had no rate limit of any kind, so passwords could be guessed as
    fast as the network allowed — on an AllowAny endpoint reachable from the
    internet.

    Rates are disabled in `OMS/test_settings.py` (a process-global throttle
    cache makes every other test order-dependent), so this class turns them
    back on for itself and clears the cache, which persists across tests.
    """

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(REST_FRAMEWORK={
        **settings.REST_FRAMEWORK,
        'DEFAULT_THROTTLE_RATES': {'anon': None, 'user': None, 'login': '3/min'},
    })
    def test_repeated_failed_logins_are_throttled(self):
        from rest_framework.throttling import ScopedRateThrottle
        ScopedRateThrottle.THROTTLE_RATES = settings.REST_FRAMEWORK[
            'DEFAULT_THROTTLE_RATES']

        seen = [
            self.anon.post(reverse('login'),
                           {'username': 't-staff', 'password': f'guess-{i}'},
                           format='json').status_code
            for i in range(5)
        ]
        self.assertIn(429, seen, f'no 429 in {seen} — guessing is unlimited')
        # The throttle must not swallow the earlier attempts' real answer.
        self.assertEqual(seen[0], 401)


class SecuritySettingsTests(TestCase):
    """`settings.py` carried three problems on five lines: a committed
    SECRET_KEY, `DEBUG = True` overriding the env-driven line above it, and
    `'*'` in ALLOWED_HOSTS defeating Host-header validation.

    These assertions describe the DEBUG=false (deployed) shape, so they are
    written against the settings module's logic rather than the values this
    test run happens to load.
    """

    def test_the_committed_secret_key_is_no_longer_a_production_fallback(self):
        """It stays in the file as the DEBUG-only fallback, so grepping for it
        proves nothing. What matters is that a non-DEBUG start REFUSES to
        proceed without SECRET_KEY rather than quietly reusing it."""
        source = (Path(settings.BASE_DIR) / 'OMS' / 'settings.py').read_text(
            encoding='utf-8')
        self.assertIn('raise ImproperlyConfigured', source)
        self.assertIn("SECRET_KEY = config('SECRET_KEY', default='')", source)

    def test_allowed_hosts_wildcard_is_debug_only(self):
        """`'*'` used to be unconditional, which disabled Host-header
        validation on the deployed server.

        Asserted against `OMS.settings.DEBUG` — the module-level value that
        actually built the list — not `django.conf.settings.DEBUG`, which the
        test runner forces to False after import. Reading the latter would make
        this pass on a DEBUG=true machine no matter what the code did.
        """
        from OMS import settings as settings_module

        if settings_module.DEBUG:
            self.assertIn('*', settings.ALLOWED_HOSTS)
        else:
            self.assertNotIn('*', settings.ALLOWED_HOSTS)

    def test_cors_is_not_wide_open_by_default(self):
        """`CORS_ALLOW_ALL_ORIGINS` is now an explicit .env escape hatch
        defaulting to false, not the baked-in True it used to be."""
        self.assertFalse(_parse_bool('false'))
        self.assertTrue(settings.CORS_ALLOWED_ORIGINS)

    def test_throttling_is_configured(self):
        rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
        self.assertEqual(set(rates), {'anon', 'user', 'login'})

    def test_clickjacking_and_sniffing_headers_apply_everywhere(self):
        """Not DEBUG-gated: neither needs HTTPS, so there is no reason for
        development to run without them."""
        self.assertEqual(settings.X_FRAME_OPTIONS, 'DENY')
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)


class RoleResolutionTests(TestCase):
    """`core.permissions` is now the project's only definition of "admin".

    Five modules each had their own and they disagreed — two ignored
    `is_superuser`, all five ignored `extra_roles` despite `users/models.py`
    stating that every role check must consult both.
    """

    def test_extra_roles_count_as_roles(self):
        from core.permissions import has_role, is_admin, role_names

        user = _user('t-dual', 'manager')
        user.extra_roles.add(_role('admin'))
        self.assertEqual(role_names(user), frozenset({'manager', 'admin'}))
        self.assertTrue(has_role(user, 'manager'))
        self.assertTrue(is_admin(user), 'admin via extra_roles must count')

    def test_a_superuser_is_an_admin_even_with_no_role(self):
        from core.permissions import is_admin

        user = _user('t-super')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])
        self.assertTrue(is_admin(user))

    def test_an_anonymous_user_holds_no_roles(self):
        from django.contrib.auth.models import AnonymousUser

        from core.permissions import is_admin, role_names

        self.assertEqual(role_names(AnonymousUser()), frozenset())
        self.assertFalse(is_admin(AnonymousUser()))
        self.assertFalse(is_admin(None))

    def test_an_ordinary_role_is_not_privileged(self):
        from core.permissions import holds_privileged_role

        self.assertFalse(holds_privileged_role(_user('t-plain', 'salesman')))
        self.assertTrue(holds_privileged_role(_user('t-tadmin', 'tracker_admin')))
