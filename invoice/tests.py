"""JSAP connection configuration.

`invoice` had no tests. This file covers one thing: that an unconfigured JSAP
connection says so, instead of connecting somewhere.

Until now `JSAPConnection.__init__` fell back to the production host, database
and user as literals, so clearing `.env` did not disable JSAP — it reconnected
to production with three of the four values hardcoded. The values now come from
settings, which default to blank, and `connect()` refuses rather than handing
pymssql an empty server name.

Nothing here opens a socket.
"""
from django.test import TestCase, override_settings

from invoice.services.jsap_db import JSAPConnection

BLANK = dict(JSAP_DB_HOST='', JSAP_DB_PORT=1433, JSAP_DB_NAME='',
             JSAP_DB_USER='', JSAP_DB_PASSWORD='')


class JSAPConnectionConfigTests(TestCase):

    @override_settings(**BLANK)
    def test_an_unconfigured_connection_refuses_instead_of_dialling(self):
        """The failure mode this replaces was a login timeout against an empty
        server name, which reads as "JSAP is down" rather than "JSAP is off"."""
        with self.assertRaises(RuntimeError) as caught:
            JSAPConnection().connect()
        self.assertIn('JSAP_DB_HOST', str(caught.exception))

    @override_settings(**BLANK)
    def test_no_value_survives_from_the_old_hardcoded_defaults(self):
        conn = JSAPConnection()
        self.assertEqual(
            (conn.host, conn.database, conn.username, conn.password),
            ('', '', '', ''),
            'a hardcoded fallback is still supplying a value')

    @override_settings(JSAP_DB_HOST='  jsap.example  ', JSAP_DB_PORT='1433',
                       JSAP_DB_NAME="'jsapdb'", JSAP_DB_USER='"svc"',
                       JSAP_DB_PASSWORD=" pw ")
    def test_values_are_stripped_of_whitespace_and_stray_quotes(self):
        """`.env` values routinely arrive wrapped in quotes or padded; pymssql
        would treat those characters as part of the name."""
        conn = JSAPConnection()
        self.assertEqual(conn.host, 'jsap.example')
        self.assertEqual(conn.database, 'jsapdb')
        self.assertEqual(conn.username, 'svc')
        self.assertEqual(conn.password, 'pw')
        self.assertEqual(conn.port, 1433)
