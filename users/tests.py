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
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import identify_hasher
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from OMS.settings import _parse_bool
from sap_sync.models import Party, Product
from users.models import (
    PartyProductAssignment,
    User,
    UserPartyAssignment,
    UserRole,
)


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


class BulkPartyRateTests(_ApiTestCase):
    """Re-pricing a set of parties in one request.

    The two endpoints behind the bulk half of Party Product Assignment. What
    matters here is not that they write — it is the four ways a mass price
    change can go wrong quietly: reaching a party that was never meant to sell
    the item, reading a percentage as an absolute rate, counting a BEVERAGES
    party as "missing" an OIL product, and rounding a repeated revision adrift.
    """

    def setUp(self):
        super().setUp()
        for card_code, state in (('H1', 'HARYANA'), ('H2', 'HARYANA'), ('P1', 'PUNJAB')):
            Party.objects.create(
                card_code=card_code, card_name=f'{card_code} traders',
                state=state, main_group='DISTRIBUTOR', category='OIL')
        Party.objects.create(
            card_code='M1', card_name='M1 mart', state='HARYANA',
            main_group='DISTRIBUTOR', category='MART')

        Product.objects.create(
            item_code='FG001', item_name='MUSTARD 1 LTR', category='OIL',
            variety='MUSTARD', is_active='Y')
        Product.objects.create(
            item_code='FG002', item_name='CANOLA 1 LTR', category='OIL',
            variety='CANOLA', is_active='Y')
        Product.objects.create(
            item_code='MG001', item_name='ATTA 5 KG', category='MART', is_active='Y')

        # H1 and H2 both sell mustard, at rates that have drifted apart. Only
        # H1 sells canola.
        PartyProductAssignment.objects.create(
            card_code='H1', item_code='FG001', category='OIL', basic_rate=Decimal('100'))
        PartyProductAssignment.objects.create(
            card_code='H2', item_code='FG001', category='OIL', basic_rate=Decimal('110'))
        PartyProductAssignment.objects.create(
            card_code='H1', item_code='FG002', category='OIL', basic_rate=Decimal('200'))

        self.haryana_oil = [
            {'card_code': 'H1', 'category': 'OIL'},
            {'card_code': 'H2', 'category': 'OIL'},
        ]

    def _rate(self, card_code, item_code, category='OIL'):
        return PartyProductAssignment.objects.get(
            card_code=card_code, item_code=item_code, category=category).basic_rate

    # ── Reading what the selection holds ───────────────────────────────────

    def test_unique_items_across_parties_carry_the_rate_spread(self):
        res = self.as_admin.post(
            reverse('bulk-party-products'),
            {'party_selections': self.haryana_oil}, format='json')
        self.assertEqual(res.status_code, 200)

        rows = {row['item_code']: row for row in res.json()['data']['products']}
        self.assertEqual(set(rows), {'FG001', 'FG002'})

        mustard = rows['FG001']
        self.assertEqual(mustard['party_count'], 2)
        self.assertEqual(mustard['distinct_rates'], 2)
        self.assertEqual((mustard['min_rate'], mustard['max_rate']), (100.0, 110.0))
        self.assertEqual(mustard['missing_parties'], 0)

        # Only H1 has canola, so the row says one of the two parties is without
        # it — the figure the "assign to all" toggle exists for.
        self.assertEqual(rows['FG002']['party_count'], 1)
        self.assertEqual(rows['FG002']['missing_parties'], 1)

    def test_a_mart_party_is_not_counted_as_missing_an_oil_product(self):
        """A MART party cannot be sold an OIL item, so it is not a gap."""
        res = self.as_admin.post(
            reverse('bulk-party-products'),
            {'party_selections': self.haryana_oil + [{'card_code': 'M1', 'category': 'MART'}]},
            format='json')
        rows = {row['item_code']: row for row in res.json()['data']['products']}
        self.assertEqual(rows['FG001']['eligible_parties'], 2)
        self.assertEqual(rows['FG001']['missing_parties'], 0)

    def test_an_inactive_product_is_not_offered_for_re_pricing(self):
        Product.objects.filter(item_code='FG002').update(is_active='N')
        res = self.as_admin.post(
            reverse('bulk-party-products'),
            {'party_selections': self.haryana_oil}, format='json')
        codes = {row['item_code'] for row in res.json()['data']['products']}
        self.assertEqual(codes, {'FG001'})

    # ── Writing the revision ───────────────────────────────────────────────

    def test_setting_one_rate_writes_it_to_every_selected_party(self):
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 125}]},
            format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['data']['updated'], 2)
        self.assertEqual(self._rate('H1', 'FG001'), Decimal('125.0000'))
        self.assertEqual(self._rate('H2', 'FG001'), Decimal('125.0000'))

    def test_a_party_outside_the_selection_is_untouched(self):
        PartyProductAssignment.objects.create(
            card_code='P1', item_code='FG001', category='OIL', basic_rate=Decimal('100'))
        self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 125}]},
            format='json')
        self.assertEqual(self._rate('P1', 'FG001'), Decimal('100.0000'))

    def test_a_percentage_moves_each_partys_own_rate(self):
        """The whole reason percent mode exists: 100 and 110 are not one price."""
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'rate_mode': 'percent',
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 10}]},
            format='json')
        self.assertEqual(res.json()['data']['updated'], 2)
        self.assertEqual(self._rate('H1', 'FG001'), Decimal('110.0000'))
        self.assertEqual(self._rate('H2', 'FG001'), Decimal('121.0000'))

    def test_a_rupee_change_may_be_a_cut(self):
        self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'rate_mode': 'amount',
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': -15}]},
            format='json')
        self.assertEqual(self._rate('H1', 'FG001'), Decimal('85.0000'))

    def test_a_cut_below_zero_is_refused_and_leaves_the_rate_alone(self):
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'rate_mode': 'amount',
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': -500}]},
            format='json')
        data = res.json()['data']
        self.assertEqual(data['updated'], 0)
        self.assertEqual(len(data['errors']), 2)
        self.assertEqual(self._rate('H1', 'FG001'), Decimal('100.0000'))

    def test_by_default_a_party_without_the_product_is_left_without_it(self):
        """The dangerous mistake is widening: handing a party something to sell."""
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'FG002', 'category': 'OIL', 'basic_rate': 210}]},
            format='json')
        data = res.json()['data']
        self.assertEqual((data['updated'], data['created'], data['skipped']), (1, 0, 1))
        self.assertFalse(
            PartyProductAssignment.objects.filter(card_code='H2', item_code='FG002').exists())

    def test_apply_to_all_assigns_the_product_where_it_is_missing(self):
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'apply_to': 'all',
             'items': [{'item_code': 'FG002', 'category': 'OIL', 'basic_rate': 210}]},
            format='json')
        data = res.json()['data']
        self.assertEqual((data['updated'], data['created']), (1, 1))
        self.assertEqual(self._rate('H2', 'FG002'), Decimal('210.0000'))

    def test_a_percentage_never_assigns_even_when_asked_to(self):
        """`apply_to: all` has no meaning here — there is no rate to move."""
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'rate_mode': 'percent', 'apply_to': 'all',
             'items': [{'item_code': 'FG002', 'category': 'OIL', 'basic_rate': 5}]},
            format='json')
        self.assertEqual(res.json()['data']['created'], 0)
        self.assertFalse(
            PartyProductAssignment.objects.filter(card_code='H2', item_code='FG002').exists())

    def test_a_mixed_selection_prices_each_party_in_its_own_category(self):
        PartyProductAssignment.objects.create(
            card_code='M1', item_code='MG001', category='MART', basic_rate=Decimal('300'))
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil + [{'card_code': 'M1', 'category': 'MART'}],
             'apply_to': 'all',
             'items': [
                 {'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 125},
                 {'item_code': 'MG001', 'category': 'MART', 'basic_rate': 320},
             ]},
            format='json')
        self.assertEqual(res.json()['data']['errors'], [])
        self.assertEqual(self._rate('M1', 'MG001', 'MART'), Decimal('320.0000'))
        # The MART party was never given the oil, despite `apply_to: all`.
        self.assertFalse(
            PartyProductAssignment.objects.filter(card_code='M1', item_code='FG001').exists())

    def test_a_rate_already_correct_is_reported_rather_than_rewritten(self):
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 100}]},
            format='json')
        data = res.json()['data']
        self.assertEqual((data['updated'], data['unchanged']), (1, 1))

    def test_an_unknown_product_is_refused_before_anything_is_written(self):
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'NOPE', 'category': 'OIL', 'basic_rate': 10}]},
            format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._rate('H1', 'FG001'), Decimal('100.0000'))

    def test_a_negative_absolute_rate_is_refused(self):
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': -5}]},
            format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._rate('H1', 'FG001'), Decimal('100.0000'))

    def test_an_inactive_assignment_is_not_quietly_revived_by_a_rate_change(self):
        PartyProductAssignment.objects.filter(
            card_code='H2', item_code='FG001').update(is_active=False)
        res = self.as_admin.post(
            reverse('bulk-party-update-rates'),
            {'party_selections': self.haryana_oil,
             'items': [{'item_code': 'FG001', 'category': 'OIL', 'basic_rate': 125}]},
            format='json')
        self.assertEqual(res.json()['data']['skipped'], 1)
        self.assertFalse(
            PartyProductAssignment.objects.get(
                card_code='H2', item_code='FG001').is_active)

    # ── Who may do it ──────────────────────────────────────────────────────

    def test_an_ungranted_user_cannot_read_or_re_price(self):
        for name in ('bulk-party-products', 'bulk-party-update-rates'):
            self.assertEqual(self.as_staff.post(reverse(name), {}, format='json').status_code, 403)

    def test_an_anonymous_request_is_refused(self):
        for name in ('bulk-party-products', 'bulk-party-update-rates'):
            self.assertIn(
                self.anon.post(reverse(name), {}, format='json').status_code, (401, 403))

    def test_the_page_key_is_enough_without_the_admin_role(self):
        self.staff.extra_pages = ['Party_Product_Assignment']
        self.staff.save(update_fields=['extra_pages'])
        res = self.as_staff.post(
            reverse('bulk-party-products'),
            {'party_selections': self.haryana_oil}, format='json')
        self.assertEqual(res.status_code, 200)
