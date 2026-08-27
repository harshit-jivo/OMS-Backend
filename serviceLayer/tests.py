"""SAP Service Layer session manager.

`serviceLayer` had no tests, while holding the session manager that
`serviceLayer.views`, `serviceLayer.ap_views` and `einvoice.sap` all use to
reach SAP. Nothing here opens a socket: every request is stubbed.

The four things asserted are the four that were wrong.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from serviceLayer import service
from serviceLayer.service import SAPServiceLayerManager


class SessionTtlTests(TestCase):
    """SAP reports SessionTimeout in MINUTES."""

    def test_a_thirty_minute_session_is_cached_for_under_thirty_minutes(self):
        """The bug: 30 was read as seconds and 60 subtracted, giving -30.
        Django treats a non-positive TTL as already expired, so the session was
        never reused and every SAP call logged in again."""
        ttl = service._session_ttl({'SessionTimeout': 30})
        self.assertEqual(ttl, 30 * 60 - 60)
        self.assertGreater(ttl, 0)

    def test_a_missing_value_does_not_outlive_a_real_session(self):
        """The other half: with the field absent the old default of 3600 was
        used as seconds, caching for 59 minutes against a session SAP drops
        after 30 — a cached session that no longer exists."""
        self.assertLessEqual(service._session_ttl({}), 30 * 60)

    def test_a_junk_value_cannot_produce_a_negative_ttl(self):
        for value in (None, '', 'soon', 0, -5):
            with self.subTest(value=value):
                self.assertGreaterEqual(
                    service._session_ttl({'SessionTimeout': value}), 60)


class TlsVerificationTests(TestCase):
    """`verify=False` was hardcoded in four places, so SAP credentials went
    over an unverified connection whatever the configuration said."""

    @override_settings(HANA_SSL_VERIFY=True, HANA_SSL_CA_BUNDLE='')
    def test_verification_can_be_turned_on(self):
        self.assertIs(service._verify(), True)

    @override_settings(HANA_SSL_VERIFY=False, HANA_SSL_CA_BUNDLE='')
    def test_verification_can_be_turned_off(self):
        self.assertIs(service._verify(), False)

    @override_settings(HANA_SSL_VERIFY=True, HANA_SSL_CA_BUNDLE='/etc/ca.pem')
    def test_a_ca_bundle_wins(self):
        """requests takes a path here, which is how a self-signed SAP cert is
        trusted without turning verification off wholesale."""
        self.assertEqual(service._verify(), '/etc/ca.pem')

    @override_settings(HANA_SSL_VERIFY=True, HANA_SSL_CA_BUNDLE='')
    def test_the_configured_value_reaches_the_login_call(self):
        """Asserting the setting in isolation would not catch a call site that
        still passes a literal."""
        cache.clear()
        with patch.object(service.requests, 'post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'SessionTimeout': 30,
                                                   'SessionId': 'abc'}
            post.return_value.cookies = {'ROUTEID': 'r1'}
            SAPServiceLayerManager.get_session('OIL')
        self.assertIs(post.call_args.kwargs['verify'], True)


class TimeoutTests(TestCase):
    def test_every_login_call_carries_a_timeout(self):
        """A Service Layer call with no timeout holds the worker until SAP
        answers, which on a hung connection is never."""
        cache.clear()
        with patch.object(service.requests, 'post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'SessionTimeout': 30,
                                                   'SessionId': 'abc'}
            post.return_value.cookies = {'ROUTEID': 'r1'}
            SAPServiceLayerManager.get_session('OIL')
            SAPServiceLayerManager.get_session_for('u', 'p', 'OIL')
        for call in post.call_args_list:
            self.assertIsInstance(call.kwargs.get('timeout'), tuple)


class CredentialLoggingTests(TestCase):
    """`print(login_payload)` stood in `get_session_for`, so every approver
    login wrote a live SAP password to stdout — that is, to the server log."""

    def test_the_approver_password_is_never_printed(self):
        with patch.object(service.requests, 'post') as post, \
                patch('builtins.print') as printed:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'SessionId': 'abc'}
            post.return_value.cookies = {'ROUTEID': 'r1'}
            SAPServiceLayerManager.get_session_for('approver', 's3cret', 'OIL')
        printed.assert_not_called()

    def test_no_call_in_this_module_prints_a_login_payload(self):
        """Belt and braces: the test above covers one call path only.

        Checked with the AST rather than a substring — the comment that now
        stands where the `print` was quotes the old line, and a substring
        search cannot tell an explanation from the thing it explains.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path(service.__file__).read_text(encoding='utf-8'))
        printed = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == 'print'
        ]
        self.assertEqual(printed, [], 'print() in a module that handles SAP '
                                      'credentials')

    def test_no_logging_call_passes_the_payload_itself(self):
        """A `logger.debug(login_payload)` would leak exactly as a print did,
        while looking like an improvement."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(service.__file__).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in {'login_payload',
                                                            'password'}:
                    self.fail(f'{arg.id} passed to a call at line {node.lineno}')


class CompanyDbIsolationTests(TestCase):
    """A session is valid for ONE company database. Sharing one cache key
    across all three would hand an OIL session to a BEVERAGE caller and post
    to the wrong company's books."""

    def test_each_company_db_gets_its_own_cache_key(self):
        oil = SAPServiceLayerManager._cache_keys(
            SAPServiceLayerManager.schema_for('OIL'))
        bev = SAPServiceLayerManager._cache_keys(
            SAPServiceLayerManager.schema_for('BEVERAGE'))
        self.assertNotEqual(oil, bev)

    def test_both_beverage_spellings_resolve_alike(self):
        self.assertEqual(SAPServiceLayerManager.schema_for('BEVERAGE'),
                         SAPServiceLayerManager.schema_for('BEVERAGES'))

    def test_branch_and_schema_round_trip(self):
        for branch in ('OIL', 'BEVERAGE', 'MART'):
            with self.subTest(branch=branch):
                schema = SAPServiceLayerManager.schema_for(branch)
                self.assertEqual(SAPServiceLayerManager.branch_for(schema),
                                 branch)


class NoSilentTlsDowngradeTests(TestCase):
    """No SAP client may retry with verification off after a certificate error.

    Two did. `sap_sync.services.sync_service._post_with_ssl_fallback` caught
    SSLError, retried with `verify=False`, and left verification off for the
    rest of that object's life; `sap_sync.services.hana_service` did the same.

    That is worse than never verifying: it yields at exactly the moment
    verification is doing its job, then sends the SAP credential and a sales
    order over the connection it just failed to authenticate — silently, so a
    deployment could believe it had TLS on for months.

    Checked by source inspection rather than by exercising the client, because
    the failure is a `verify=False` on a RETRY path that only a real handshake
    failure reaches.
    """

    MODULES = (
        'sap_sync.services.sync_service',
        'sap_sync.services.hana_service',
        'serviceLayer.service',
        'payments.sap_client',
        'einvoice.sap',
    )

    def test_no_sap_client_catches_sslerror(self):
        import ast
        import importlib
        from pathlib import Path

        offenders = []
        for name in self.MODULES:
            module = importlib.import_module(name)
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or node.type is None:
                    continue
                caught = ast.unparse(node.type)
                if 'SSLError' in caught:
                    offenders.append(f'{name}:{node.lineno} catches {caught}')
        self.assertEqual(offenders, [], (
            'a caught SSLError is almost always a retry without verification:\n  '
            + '\n  '.join(offenders)))

    def test_no_sap_client_hardcodes_verify_false(self):
        """`verify=False` must come from configuration, never from a literal."""
        import ast
        import importlib
        from pathlib import Path

        offenders = []
        for name in self.MODULES:
            module = importlib.import_module(name)
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == 'verify' and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is False:
                        offenders.append(f'{name}:{node.lineno}')
        self.assertEqual(offenders, [],
                         'verify=False written as a literal: ' + ', '.join(offenders))

    def test_every_sap_request_carries_a_timeout(self):
        """A Service Layer call with no timeout holds the worker until SAP
        answers, which on a hung connection is never. `hana_service` had none
        on any request."""
        import ast
        import importlib
        from pathlib import Path

        offenders = []
        for name in self.MODULES:
            module = importlib.import_module(name)
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {'post', 'get', 'put', 'patch', 'delete'}:
                    continue
                base = ast.unparse(node.func.value)
                if 'session' not in base.lower() and base != 'requests':
                    continue
                if not any(kw.arg == 'timeout' for kw in node.keywords):
                    offenders.append(f'{name}:{node.lineno} {ast.unparse(node.func)}')
        self.assertEqual(offenders, [],
                         'HTTP call with no timeout: ' + ', '.join(offenders))
