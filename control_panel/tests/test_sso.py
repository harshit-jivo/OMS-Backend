"""Opening a Control Panel page: the ticket link, and the session it becomes."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core import signing
from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

import core.permissions as core_perms
from core.permission_registry import ALL_KEYS, REGISTRY
from control_panel import permissions as perms
from control_panel.views import SsoLinkView
from cpanel.core import oms_access, oms_session, page_guard


def _user(**extra):
    return SimpleNamespace(pk=7, id=7, is_authenticated=True, is_active=True,
                           name='Priya Sharma', email='p@example.com',
                           get_username=lambda: 'priya', **extra)


def _keys(stack, keys, admin=False):
    for module in (core_perms, perms, oms_access, page_guard):
        stack.enter_context(mock.patch.object(module, 'effective_keys', return_value=frozenset(keys)))
    stack.enter_context(mock.patch.object(core_perms, 'is_admin', return_value=admin))


class TicketLinkTests(SimpleTestCase):
    factory = APIRequestFactory()

    def setUp(self):
        cache.clear()

    def _post(self, page, keys):
        request = self.factory.post('/api/control-panel/sso/', {'page': page}, format='json')
        force_authenticate(request, user=_user())
        with ExitStack() as stack:
            _keys(stack, keys)
            return SsoLinkView.as_view()(request)

    def _ticket(self, response):
        path = response.data['data']['path']
        self.assertTrue(path.startswith('/cp/session/?ticket='))
        raw = parse_qs(urlparse(path).query)['ticket'][0]
        return raw, signing.loads(raw, salt=oms_session.SALT)

    def test_link_names_the_page_and_the_user(self):
        _, data = self._ticket(self._post('beverages-sale', {perms.BEVERAGES, perms.PREMIUM_ONLY}))
        self.assertEqual((data['uid'], data['path']), (7, '/realise/beverages/'))

    def test_a_page_needs_its_own_key(self):
        self.assertEqual(self._post('salaries', {perms.SALES}).status_code, 403)
        self.assertEqual(self._post('sales', {'Reports'}).status_code, 403)
        # A sub-tab opens its own page, not its siblings.
        self.assertEqual(self._post('targets', {perms.BEVERAGES}).status_code, 403)
        self.assertEqual(self._post('oils-sale', {perms.OILS_MAP}).status_code, 200)
        self.assertEqual(self._post('inventory', {perms.INVENTORY_TABS['aging']}).status_code, 200)
        # A scope or the AI assistant alone opens nothing.
        self.assertEqual(self._post('oils-sale', {perms.PREMIUM_ONLY}).status_code, 403)
        self.assertEqual(self._post('inventory', {perms.INVENTORY_CHAT}).status_code, 403)
        self.assertEqual(self._post('users', {perms.SALES}).status_code, 400)

    def test_a_ticket_opens_once(self):
        raw, _ = self._ticket(self._post('sales', {perms.SALES}))
        with mock.patch.object(oms_session, 'get_user_model') as gum:
            gum.return_value.objects.filter.return_value.first.return_value = _user()
            user, path = oms_session.redeem(raw)
            self.assertEqual(path, '/sales/')
            with self.assertRaises(oms_session.TicketRefused):
                oms_session.redeem(raw)

    def test_a_forged_ticket_is_refused(self):
        forged = signing.dumps({'uid': 1, 'path': '/sales/', 'nonce': 'x'}, key='wrong', salt=oms_session.SALT)
        with self.assertRaises(oms_session.TicketRefused):
            oms_session.redeem(forged)

    def test_paths_agree_with_the_pages(self):
        self.assertEqual({p for p, _ in perms.PAGES.values()}, set(oms_session.ALLOWED_PATHS))
        # The registry lists exactly the keys the tree and scopes define.
        self.assertEqual(set(perms.ALL_CP_KEYS), set(REGISTRY['control_panel']))
        self.assertTrue(set(perms.ALL_CP_KEYS) <= ALL_KEYS)


class AccessBridgeTests(SimpleTestCase):
    """OMS keys -> the groups and flags C_Panel's own code checks."""

    def _flags(self, keys, admin=False):
        with ExitStack() as stack:
            _keys(stack, keys, admin)
            return oms_access.cp_groups(_user()), oms_access.cp_flags(_user())

    def test_premium_only_is_a_premium_viewer(self):
        groups, flags = self._flags({perms.OILS_OVERVIEW, perms.PREMIUM_ONLY})
        self.assertEqual(groups, {'realise_premium'})
        self.assertTrue(flags['can_realise'])
        self.assertFalse(flags['can_edit'] or flags['can_sales'] or flags['can_salaries'])

    def test_without_a_scope_realise_is_all_segments_and_edits(self):
        groups, flags = self._flags({perms.SALES_CHANNEL})
        self.assertEqual(groups, {'realise_admin'})
        self.assertTrue(flags['can_edit'])
        groups, _ = self._flags({perms.TARGETS, perms.COMMODITY_ONLY})
        self.assertEqual(groups, {'realise_commodity'})

    def test_one_group_per_page(self):
        groups, _ = self._flags({perms.SALES, perms.INVENTORY_TABS['trace'], perms.SALARIES})
        self.assertEqual(groups, {'sales_viewer', 'inventory_viewer', 'salaries_viewer'})

    def test_admin_gets_every_page_and_nothing_else(self):
        # Admins hold the scope keys too; they must not narrow an admin.
        groups, flags = self._flags(ALL_KEYS, admin=True)
        self.assertIn('realise_admin', groups)
        opened = {k for k, v in flags.items() if v}
        self.assertEqual(opened, {'can_edit', 'can_realise', 'can_inventory', 'inventory_can_edit',
                                  'can_sales', 'can_expenses', 'can_salaries'})

    def test_no_key_opens_nothing(self):
        groups, flags = self._flags({'Reports'})
        self.assertEqual(groups, set())
        self.assertFalse(any(flags.values()))


class PageGuardTests(SimpleTestCase):
    """A page path needs one of ITS keys, not just the group behind it."""

    def _get(self, path, keys):
        request = SimpleNamespace(path=path, user=_user())
        with ExitStack() as stack:
            _keys(stack, keys)
            guard = page_guard.ControlPanelPageGuard(lambda r: 'page')
            with mock.patch.object(page_guard, 'render', return_value='refused'):
                return guard(request)

    def test_a_sub_tab_opens_its_own_page_only(self):
        self.assertEqual(self._get('/realise/beverages/', {perms.BEVERAGES}), 'page')
        self.assertEqual(self._get('/realise/targets/', {perms.BEVERAGES}), 'refused')
        self.assertEqual(self._get('/realise/', {perms.BEVERAGES}), 'refused')
        self.assertEqual(self._get('/realise/', {perms.OILS_REALISE}), 'page')

    def test_other_paths_pass_through(self):
        # The JSON endpoints stay with C_Panel's own group checks.
        self.assertEqual(self._get('/realise/api/channel-targets/', set()), 'page')


class SubTabTests(SimpleTestCase):
    def test_only_held_tabs_in_page_order(self):
        with ExitStack() as stack:
            _keys(stack, {perms.INVENTORY_TABS['trace'], perms.INVENTORY_TABS['stock']})
            self.assertEqual(perms.allowed_tabs(_user(), perms.INVENTORY_TABS), ['stock', 'trace'])
            self.assertEqual(perms.allowed_tabs(_user(), perms.OILS_TABS), [])


class CompanySettingsTests(SimpleTestCase):
    """The Control Panel's SAP companies come from OMS's .env, never C_Panel's names."""

    def test_companies_are_the_oms_settings(self):
        from django.conf import settings

        from cpanel.core import companies
        self.assertEqual((companies.OIL, companies.BEVERAGES, companies.MART),
                         (settings.HANA_OIL_COMPANY_DB, settings.HANA_BEVERAGE_COMPANY_DB,
                          settings.HANA_MART_COMPANY_DB))

    def test_a_blank_company_stops_the_server(self):
        from django.test import override_settings

        from cpanel.core.companies import check_companies
        self.assertEqual(check_companies(), [])
        with override_settings(HANA_MART_COMPANY_DB=''):
            self.assertEqual([e.id for e in check_companies()], ['cpanel.E001'])

    def test_no_company_name_is_hard_coded(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[2] / 'cpanel'
        quoted = re.compile(r'''["']JIVO_(OIL|BEVERAGES|MART)_HANADB["']''')
        hits = [str(p.relative_to(root)) for p in root.rglob('*.py') if quoted.search(p.read_text(encoding='utf-8'))]
        self.assertEqual(hits, [])
