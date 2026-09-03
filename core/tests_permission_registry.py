"""The permission registry and key-based authorization (Phase 2).

What is worth testing here and why:

1. **Registry integrity.** Every other guarantee rests on `ALL_KEYS` being a
   faithful, collision-free index of the registry. If two modules register the
   same key, the matrix UI becomes ambiguous and the label a lottery.

2. **`HasKey` fails at construction, not at request time.** The entire point
   of validating against the registry in `__init__` is that a typo in a view
   crashes the process on import — during deploy, in front of whoever made
   it — instead of shipping an endpoint that quietly denies everyone (or a
   grant that never matches). A test that only exercised `has_permission`
   would pass with that property broken.

3. **`effective_keys` fails closed and degrades gracefully.** Anonymous and
   None users hold nothing. A database without the RolePermissions table
   (migration not applied) must yield exactly the extra_pages-only behaviour
   the system had before Phase 2 — that degradation is what makes the model
   change deployable without a lockstep migrate.

4. **The seed migration's keys are registered.** 0032 grants string literals;
   if one drifts from the registry, the intersection in `effective_keys`
   silently discards it and a desk loses access on the Phase-3 flip. Cheap to
   pin here, expensive to discover in production.
"""

from django.test import SimpleTestCase, TestCase

from core.permission_registry import ALL_KEYS, REGISTRY
from core.permissions import HasKey, effective_keys


class RegistryIntegrityTests(SimpleTestCase):
    def test_all_keys_matches_registry(self):
        listed = [key for keys in REGISTRY.values() for key in keys]
        self.assertEqual(len(listed), len(ALL_KEYS), 'duplicate key across modules')
        self.assertEqual(set(listed), set(ALL_KEYS))

    def test_keys_are_nonempty_strings(self):
        for key in ALL_KEYS:
            self.assertIsInstance(key, str)
            self.assertTrue(key.strip(), 'blank key registered')

    def test_legacy_contract_keys_still_registered(self):
        # The mobile app and the web Permissions page exchange these strings.
        # Removing one from the registry silently revokes it everywhere —
        # which must be a decision someone makes in this file, on purpose.
        for key in [
            'Payments_Create', 'Payments_Approve', 'Deposit_Create',
            'Deposit_Approve', 'Payments_Dashboard', 'App_User', 'Reports',
        ]:
            self.assertIn(key, ALL_KEYS)

    def test_seed_migration_keys_are_registered(self):
        # The seed migrations grant string literals; if one drifts from the
        # registry, the intersection in `effective_keys` silently discards it
        # and a desk loses access on the role->key flip.
        import importlib
        for mod_name in [
            'users.migrations.0032_seed_role_permissions',
            'users.migrations.0033_seed_tracker_invoice_permissions',
        ]:
            seed = importlib.import_module(mod_name)
            for role_name, keys in seed.SEED.items():
                for key in keys:
                    self.assertIn(
                        key, ALL_KEYS,
                        f'{mod_name} seeds {key!r} for {role_name!r} but the '
                        f'registry does not list it',
                    )

    def test_tracker_keys_match_tracker_module(self):
        # The tracker fold registers ROLE_PAGE_MAP's page keys verbatim; if
        # either side renames a page the union in `tracker_pages_for` quietly
        # stops granting it.
        from tracker.permissions import ALL_TRACKER_PAGES
        self.assertEqual(set(REGISTRY['tracker']), set(ALL_TRACKER_PAGES))


class HasKeyConstructionTests(SimpleTestCase):
    def test_registered_key_constructs(self):
        self.assertEqual(HasKey('orders.sales.create').key, 'orders.sales.create')

    def test_unregistered_key_raises_at_construction(self):
        with self.assertRaises(ValueError):
            HasKey('orders.sales.creat')          # the typo case

    def test_empty_key_raises(self):
        with self.assertRaises(ValueError):
            HasKey('')


class EffectiveKeysClosedTests(SimpleTestCase):
    def test_none_user_holds_nothing(self):
        self.assertEqual(effective_keys(None), frozenset())

    def test_anonymous_holds_nothing(self):
        class Anon:
            is_authenticated = False
        self.assertEqual(effective_keys(Anon()), frozenset())


class EffectiveKeysDbTests(TestCase):
    """Union and intersection behaviour, on a migrated database."""

    def _user(self, **kwargs):
        from users.models import User
        return User.objects.create_user(username=kwargs.pop('username'), password='x', **kwargs)

    def test_union_of_role_bundle_and_extra_pages(self):
        from users.models import UserRole, RolePermissions
        role = UserRole.objects.create(name='billing2', display_name='Billing 2')
        RolePermissions.objects.create(role=role, keys=['orders.sales.create'])
        u = self._user(username='t1', role=role, extra_pages=['Reports'])
        self.assertEqual(effective_keys(u), frozenset({'orders.sales.create', 'Reports'}))

    def test_extra_roles_bundle_counts(self):
        from users.models import UserRole, RolePermissions
        primary = UserRole.objects.create(name='clerk', display_name='Clerk')
        extra = UserRole.objects.create(name='rate approver 2', display_name='RA')
        RolePermissions.objects.create(role=extra, keys=['orders.decision.simple'])
        u = self._user(username='t2', role=primary)
        u.extra_roles.add(extra)
        self.assertIn('orders.decision.simple', effective_keys(u))

    def test_unregistered_stored_key_is_inert(self):
        from users.models import UserRole, RolePermissions
        role = UserRole.objects.create(name='stale', display_name='Stale')
        RolePermissions.objects.create(role=role, keys=['Version_Management'])  # retired key
        u = self._user(username='t3', role=role, extra_pages=['Not_A_Key'])
        self.assertEqual(effective_keys(u), frozenset())

    def test_admin_holds_every_registered_key(self):
        from users.models import UserRole
        role = UserRole.objects.create(name='admin', display_name='Admin')
        u = self._user(username='t4', role=role)
        self.assertEqual(effective_keys(u), frozenset(ALL_KEYS))



class HasKeyOrRoleTests(SimpleTestCase):
    """The transitional gate (Phase 3). See its cleanup contract: these tests
    exist to be DELETED with the class once migration 0032 is verified live."""

    def test_unregistered_key_raises_at_construction(self):
        from core.permissions import HasKeyOrRole
        with self.assertRaises(ValueError):
            HasKeyOrRole('orders.sales.creat', 'billing')

    def test_roleless_construction_refused(self):
        # Without a fallback role this class is HasKey wearing a costume;
        # refusing it keeps call sites honest about which phase they are in.
        from core.permissions import HasKeyOrRole
        with self.assertRaises(ValueError):
            HasKeyOrRole('orders.sales.create')

    def _fake_user(self, pages=(), role_name=None):
        class _Empty:
            def values_list(self, *a, **k): return []

        class U:
            is_authenticated = True
            is_staff = False
            is_superuser = False
            extra_roles = _Empty()
        u = U()
        u.extra_pages = list(pages)
        u.role = type('R', (), {'name': role_name, 'id': None})() if role_name else None
        return u

    def _passes(self, perm, user):
        req = type('Req', (), {'user': user})()
        return perm.has_permission(req, None)

    def test_key_admits_without_role(self):
        from core.permissions import HasKeyOrRole
        perm = HasKeyOrRole('orders.sales.create', 'billing')
        self.assertTrue(self._passes(perm, self._fake_user(pages=['orders.sales.create'])))

    def test_role_admits_without_key(self):
        from core.permissions import HasKeyOrRole
        perm = HasKeyOrRole('orders.sales.create', 'billing')
        self.assertTrue(self._passes(perm, self._fake_user(role_name='Billing')))

    def test_neither_denies(self):
        from core.permissions import HasKeyOrRole
        perm = HasKeyOrRole('orders.sales.create', 'billing')
        self.assertFalse(self._passes(perm, self._fake_user(pages=['Reports'], role_name='legal')))
