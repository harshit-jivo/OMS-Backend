"""Cash G/L accounts come from SAP's account tree, never from a constant.

A cash drawer has no house-bank row, so it is identified by where it sits in
the chart of accounts: the postable, unfrozen children of the configured node
(SAP_CASH_PARENT_ACCOUNT — 1105000 "CASH IN HAND" in every company database).

These tests pin the three properties that make that rule safe to rely on: the
SQL asks SAP the right question, the node itself can never be selected, and no
account code is written into the application.

Every SAP read is mocked. Nothing here opens a HANA connection.
"""
import ast
import pathlib
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from . import bank_master, hana_queries

CASH_PARENT = '1105000'

# What SAP returns for the postable children — the shape, not a fixture the
# application is allowed to know: the codes live only in this test.
OIL_ROWS = [
    {'gl_account': '1105001', 'account_name': 'CASH SALE'},
    {'gl_account': '1105002', 'account_name': 'CASH IN HAND'},
    {'gl_account': '1105003', 'account_name': 'CASH SALE MAYAPURI'},
]


def _user(username):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(username=username, name=username.title(),
                               role=role, extra_pages=[])


class FakeConnection:
    """Stands in for HANAConnection, recording what it was asked."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self.rows


@override_settings(SAP_CASH_PARENT_ACCOUNT=CASH_PARENT)
class CashAccountQueryTests(SimpleTestCase):
    """The SQL asks SAP the approved question."""

    def setUp(self):
        cache.clear()

    def _run(self, rows=OIL_ROWS):
        conn = FakeConnection(rows)
        with patch.object(hana_queries, 'HANAConnection', lambda: conn), \
                patch.object(hana_queries, '_schema_for',
                             lambda company: 'SCHEMA_OIL'):
            accounts = hana_queries.fetch_company_cash_accounts(company='OIL')
        return conn, accounts

    def test_the_parent_is_a_bind_parameter(self):
        """Never interpolated: the value is configuration, not a literal."""
        conn, _ = self._run()
        self.assertEqual(conn.params, [CASH_PARENT])
        self.assertNotIn(CASH_PARENT, conn.sql)

    def test_only_postable_children_are_asked_for(self):
        """The parent node is a summary account, so this excludes it."""
        conn, _ = self._run()
        self.assertIn('"Postable"', conn.sql)
        self.assertIn("'Y'", conn.sql)
        self.assertIn('"FatherNum"', conn.sql)

    def test_frozen_accounts_are_excluded(self):
        """Freezing a drawer in SAP retires it with no OMS change."""
        conn, _ = self._run()
        self.assertIn('"Frozen"', conn.sql)
        self.assertIn("'N'", conn.sql)

    def test_the_rejected_rules_are_not_used(self):
        """CashBox is unset in this deployment; the rest are far too broad."""
        conn, _ = self._run()
        for rejected in ('CashBox', 'Finanse', 'GroupMask', 'LIKE'):
            self.assertNotIn(rejected, conn.sql)

    def test_rows_become_selectable_accounts(self):
        _, accounts = self._run()
        self.assertEqual([a['gl_account'] for a in accounts],
                         ['1105001', '1105002', '1105003'])
        first = accounts[0]
        # `key` is what a client sends back as account_key.
        self.assertEqual(first['key'], '1105001')
        self.assertEqual(first['label'], '1105001 - CASH SALE')

    def test_an_unnamed_row_still_labels_something(self):
        _, accounts = self._run([{'gl_account': '1105009', 'account_name': ''}])
        self.assertEqual(accounts[0]['account_name'], '1105009')

    def test_no_cash_node_configured_is_refused(self):
        with override_settings(SAP_CASH_PARENT_ACCOUNT=''):
            with self.assertRaises(Exception) as caught:
                hana_queries.fetch_company_cash_accounts(company='OIL')
        self.assertIn('SAP_CASH_PARENT_ACCOUNT', str(caught.exception))


@override_settings(SAP_CASH_PARENT_ACCOUNT=CASH_PARENT)
class CashAccountEndpointTests(TestCase):
    """GET /payments/cash-accounts/?company="""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(_user('cash_reader'))
        self.url = reverse('payment-cash-accounts')

    def _get(self, company, rows=OIL_ROWS):
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=[
                              {'gl_account': r['gl_account'],
                               'account_name': r['account_name'],
                               'key': r['gl_account'],
                               'label': f"{r['gl_account']} - {r['account_name']}"}
                              for r in rows]):
            return self.client.get(self.url, {'company': company})

    def test_oil_accounts_load(self):
        resp = self._get('OIL')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data['data']), 3)

    def test_beverages_accounts_load(self):
        self.assertEqual(self._get('BEVERAGES').status_code, 200)

    def test_mart_accounts_load(self):
        self.assertEqual(self._get('MART').status_code, 200)

    def test_each_company_is_asked_for_its_own_accounts(self):
        """Company isolation starts here: the schema is resolved server-side."""
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=[]) as fetch:
            self.client.get(self.url, {'company': 'BEVERAGES'})
        fetch.assert_called_once_with(company='BEVERAGES')

    def test_a_company_is_required(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_an_unknown_company_is_rejected(self):
        """resolve_company_db refuses it; the endpoint must not 500."""
        def explode(**kwargs):
            raise ValueError('"FOO" is not a known company.')

        with patch.object(hana_queries, 'fetch_company_cash_accounts', explode):
            resp = self.client.get(self.url, {'company': 'FOO'})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(resp.data['success'])

    def test_the_response_never_names_the_sap_schema(self):
        body = str(self._get('OIL').data)
        self.assertNotIn('SCHEMA', body.upper().replace('CASH SALE', ''))

    def test_sap_unreachable_is_a_failure_not_an_empty_list(self):
        """An empty dropdown would read as 'this company has no drawers'."""
        def explode(**kwargs):
            raise RuntimeError('HANA down')

        with patch.object(hana_queries, 'fetch_company_cash_accounts', explode):
            resp = self.client.get(self.url, {'company': 'OIL'})
        self.assertEqual(resp.status_code, 400)
        self.assertIs(resp.data['errors']['available'], False)

    def test_the_list_is_cached_and_refresh_bypasses_it(self):
        """Ten-minute cache, same as banks — not one SAP call per render."""
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=[]) as fetch:
            self.client.get(self.url, {'company': 'OIL'})
            self.client.get(self.url, {'company': 'OIL'})
            self.assertEqual(fetch.call_count, 1)
            self.client.get(self.url, {'company': 'OIL', 'refresh': 'true'})
            self.assertEqual(fetch.call_count, 2)

    def test_a_sap_outage_serves_the_last_known_list(self):
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=[{'gl_account': '1105001',
                                         'account_name': 'CASH SALE',
                                         'key': '1105001',
                                         'label': '1105001 - CASH SALE'}]):
            self.client.get(self.url, {'company': 'OIL'})

        def explode(**kwargs):
            raise RuntimeError('HANA down')

        with patch.object(hana_queries, 'fetch_company_cash_accounts', explode):
            accounts, meta = bank_master.get_company_cash_accounts(
                'OIL', force_refresh=True)
        self.assertTrue(meta['stale'])
        self.assertEqual(len(accounts), 1)


class NoHardcodedCashAccountTests(SimpleTestCase):
    """No cash account code may appear in application code.

    The rule discovers the accounts from SAP; a literal would quietly become a
    second source of truth and outlive the chart of accounts that produced it.
    Docstrings and comments are exempt — they are evidence, not behaviour.
    """

    FORBIDDEN = ('1105001', '1105002', '1105003')
    MODULES = ('hana_queries.py', 'bank_master.py', 'serializers.py',
               'views.py', 'services.py', 'sap_payloads.py', 'models.py')

    def test_no_cash_gl_literal_in_executable_code(self):
        base = pathlib.Path(__file__).parent
        offenders = []
        for name in self.MODULES:
            source = (base / name).read_text(encoding='utf-8')
            tree = ast.parse(source)

            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, 'body', None)
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or id(node) in docstrings:
                    continue
                text = str(node.value)
                for code in self.FORBIDDEN:
                    if code in text:
                        offenders.append(f'{name}:{node.lineno} -> {code}')
        self.assertEqual(offenders, [], f'Hardcoded cash G/L: {offenders}')


@override_settings(SAP_CASH_PARENT_ACCOUNT=CASH_PARENT)
class CashAccountCacheTests(SimpleTestCase):
    """The cache contract, exercised without a database or a user."""

    ROWS = [{'gl_account': '1105001', 'account_name': 'CASH SALE',
             'key': '1105001', 'label': '1105001 - CASH SALE'}]

    def setUp(self):
        cache.clear()

    def test_a_second_read_is_served_from_cache(self):
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=self.ROWS) as fetch:
            bank_master.get_company_cash_accounts('OIL')
            _, meta = bank_master.get_company_cash_accounts('OIL')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(meta['source'], 'cache')

    def test_refresh_bypasses_the_cache(self):
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=self.ROWS) as fetch:
            bank_master.get_company_cash_accounts('OIL')
            bank_master.get_company_cash_accounts('OIL', force_refresh=True)
        self.assertEqual(fetch.call_count, 2)

    def test_companies_are_cached_separately(self):
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=self.ROWS) as fetch:
            bank_master.get_company_cash_accounts('OIL')
            bank_master.get_company_cash_accounts('BEVERAGES')
        self.assertEqual(fetch.call_count, 2)

    def test_an_outage_serves_the_last_known_list(self):
        with patch.object(hana_queries, 'fetch_company_cash_accounts',
                          return_value=self.ROWS):
            bank_master.get_company_cash_accounts('OIL')

        def explode(**kwargs):
            raise RuntimeError('HANA down')

        with patch.object(hana_queries, 'fetch_company_cash_accounts', explode):
            accounts, meta = bank_master.get_company_cash_accounts(
                'OIL', force_refresh=True)
        self.assertTrue(meta['stale'])
        self.assertEqual(accounts, self.ROWS)

    def test_an_outage_with_nothing_cached_is_unavailable(self):
        def explode(**kwargs):
            raise RuntimeError('HANA down')

        with patch.object(hana_queries, 'fetch_company_cash_accounts', explode):
            accounts, meta = bank_master.get_company_cash_accounts('OIL')
        self.assertIs(meta['available'], False)
        self.assertEqual(accounts, [])
