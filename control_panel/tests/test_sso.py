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
        _, data = self._ticket(self._post('beverages-sale', {perms.SALES}))
        self.assertEqual((data['uid'], data['path']), (7, '/realise/beverages/'))

    def test_a_page_needs_its_own_key(self):
        self.assertEqual(self._post('salaries', {perms.SALES}).status_code, 403)
        self.assertEqual(self._post('sales', {'Reports'}).status_code, 403)
        # One page key opens every sub-page of that page, and only those.
        for page in ('sales-channel', 'beverages-sale', 'realise-dashboard', 'targets', 'sales'):
            self.assertEqual(self._post(page, {perms.SALES}).status_code, 200, page)
        self.assertEqual(self._post('oils-sale', {perms.SALES}).status_code, 403)
        self.assertEqual(self._post('expenses', {perms.FINANCE}).status_code, 200)
        self.assertEqual(self._post('inventory', {perms.FINANCE}).status_code, 403)
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

    def test_oils_sale_is_all_segments_and_edits_targets(self):
        groups, flags = self._flags({perms.OILS_SALE})
        self.assertEqual(groups, {'realise_admin'})
        self.assertTrue(flags['can_edit'] and flags['can_realise'])
        self.assertFalse(flags['can_sales'] or flags['can_salaries'])

    def test_each_page_brings_its_groups(self):
        self.assertEqual(self._flags({perms.SALES})[0], {'realise_admin', 'sales_viewer'})
        groups, flags = self._flags({perms.INVENTORY})
        self.assertEqual(groups, {'inventory_viewer', 'inventory_admin'})
        self.assertTrue(flags['inventory_can_edit'])  # rupee values shown
        self.assertEqual(self._flags({perms.FINANCE})[0], {'expenses_viewer', 'salaries_viewer'})

    def test_admin_gets_every_page_and_nothing_else(self):
        groups, flags = self._flags(ALL_KEYS, admin=True)
        self.assertIn('realise_admin', groups)
        closed = {k for k, v in flags.items() if not v}
        # Only COGS stays closed: OMS does not show that page.
        self.assertEqual(closed, {'can_cogs'})

    def test_a_report_key_opens_its_own_flag_only(self):
        _, flags = self._flags({perms.REPORT_KEY['claims']})
        self.assertEqual({k for k, v in flags.items() if v}, {'can_claims'})
        # Rate List rides on the Realise Calculator's flag, as in C_Panel.
        _, flags = self._flags({perms.REPORT_KEY['rate_list']})
        self.assertEqual({k for k, v in flags.items() if v}, {'can_realise_calculator'})

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

    def test_a_page_key_opens_its_own_pages_only(self):
        self.assertEqual(self._get('/realise/beverages/', {perms.SALES}), 'page')
        self.assertEqual(self._get('/realise/targets/', {perms.SALES}), 'page')
        self.assertEqual(self._get('/realise/', {perms.SALES}), 'refused')
        self.assertEqual(self._get('/realise/', {perms.OILS_SALE}), 'page')
        self.assertEqual(self._get('/realise/targets/', {perms.OILS_SALE}), 'refused')

    def test_shared_flag_pages_stay_apart(self):
        # Rate List and the Calculator share a flag; the guard keeps them separate.
        rate = perms.REPORT_KEY['rate_list']
        self.assertEqual(self._get('/realise/rate-list/', {rate}), 'page')
        self.assertEqual(self._get('/realise/realise-calculator/', {rate}), 'refused')
        self.assertEqual(self._get('/realise/customer-aging/', {perms.REPORT_KEY['beverages_gst']}), 'refused')

    def test_other_paths_pass_through(self):
        # The JSON endpoints stay with C_Panel's own group checks.
        self.assertEqual(self._get('/realise/api/channel-targets/', set()), 'page')


class SubTabTests(SimpleTestCase):
    def test_a_page_key_opens_every_tab(self):
        with ExitStack() as stack:
            _keys(stack, {perms.INVENTORY})
            self.assertEqual(perms.allowed_tabs(_user(), perms.INVENTORY, perms.INVENTORY_TABS),
                             list(perms.INVENTORY_TABS))
            self.assertEqual(perms.allowed_tabs(_user(), perms.OILS_SALE, perms.OILS_TABS), [])


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


class AccessImportTests(SimpleTestCase):
    def test_every_mapped_key_is_a_real_control_panel_key(self):
        from control_panel.management.commands.import_cpanel_access import GROUP_KEYS
        mapped = {k for keys in GROUP_KEYS.values() for k in keys}
        self.assertTrue(mapped <= set(perms.ALL_CP_KEYS), mapped - set(perms.ALL_CP_KEYS))
        # Every report is reachable from some C_Panel group.
        self.assertEqual({k for k in mapped if '.report.' in k}, set(perms.REPORT_KEY.values()))
