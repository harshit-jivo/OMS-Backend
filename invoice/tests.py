"""JSAP connection configuration.

`invoice` had no tests. This file covers two things: that an unconfigured
JSAP connection says so instead of connecting somewhere, and that a status
change records WHO made it.

Until now `JSAPConnection.__init__` fell back to the production host, database
and user as literals, so clearing `.env` did not disable JSAP — it reconnected
to production with three of the four values hardcoded. The values now come from
settings, which default to blank, and `connect()` refuses rather than handing
pymssql an empty server name.

Nothing here opens a socket.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from invoice.models import InvocieHistory, InvoiceLog
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


class StatusChangeIsAttributedTests(TestCase):
    """Every status change names the user who made it.

    `InvocieHistory.created_by` used to be read from the request BODY
    (`request.data.get('user')`), a key no client sends. The review screen
    PATCHes `{status, rejection_reason}` and nothing else, so every approval,
    rejection, SAP post and credit-limit row was written with created_by NULL —
    369 of them on live — while the create/edit/delete/restore paths, which
    pass `request.user`, produced none.

    These are regression tests for that, not coverage: the failure was silent
    (the endpoint returned 200 and the timeline rendered fine, just with no
    name on it), so nothing but an assertion would have caught it coming back.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='approver1', password='x')
        self.log = InvoiceLog.objects.create(
            so_number='SO-1', party_name='ACME', total_amount=100,
            status='PENDING', invoice_payload={}, created_by=self.user,
            warehouse='DL-MP', branch='OIL',
        )
        self.client = APIClient()

    def _patch(self, payload):
        return self.client.patch(
            f'/api/invoice/{self.log.pk}/update-status/', payload, format='json')

    def _last_actor(self):
        return InvocieHistory.objects.order_by('-id').first().created_by

    def test_an_approval_records_the_authenticated_user(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self._patch({'status': 'APPROVED'}).status_code, 200)
        self.assertEqual(self._last_actor(), 'approver1')

    def test_a_rejection_records_the_authenticated_user(self):
        self.client.force_authenticate(self.user)
        response = self._patch({'status': 'REJECTED', 'rejection_reason': 'rate'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._last_actor(), 'approver1')

    def test_a_sap_post_records_the_authenticated_user(self):
        # The status with the most NULL rows behind it (192), and the one whose
        # actor matters most: it is the decision SAP acted on.
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self._patch({'status': 'POSTED_TO_SAP', 'sap_doc_num': '1234'}).status_code, 200)
        self.assertEqual(self._last_actor(), 'approver1')

    def test_the_body_cannot_override_the_authenticated_user(self):
        # The whole point of resolving server-side. The view is AllowAny, so
        # without this the actor on an audit row would be whatever the caller
        # typed.
        self.client.force_authenticate(self.user)
        self._patch({'status': 'APPROVED', 'user': 'someone_else'})
        self.assertEqual(self._last_actor(), 'approver1')

    def test_an_anonymous_caller_is_recorded_as_unknown_not_as_a_name(self):
        # These views are AllowAny, so this request really does go through. It
        # must not be filed under the literal 'AnonymousUser', which would read
        # in the timeline as an account that made a decision.
        self.assertEqual(self._patch({'status': 'APPROVED'}).status_code, 200)
        self.assertIsNone(self._last_actor())
