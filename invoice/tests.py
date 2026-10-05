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
import datetime
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from invoice.models import CreditLimitLogs, InvocieHistory, InvoiceLog
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

    def test_a_sap_outcome_cannot_be_recorded_through_update_status(self):
        # POSTED_TO_SAP and its document numbers come only from post-to-sap,
        # which got them from SAP. Here anyone could have typed them.
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self._patch({'status': 'POSTED_TO_SAP', 'sap_doc_num': '1234'}).status_code, 400)
        self.log.refresh_from_db()
        self.assertEqual((self.log.status, self.log.sap_doc_num), ('PENDING', None))

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


class ReservedBatchesExcludeLogTests(TestCase):
    """`?exclude_log=` drops one log's own holds.

    A failed invoice sits in ERROR, which is a holding status, so its batches
    are reserved — by itself. Asked to re-allocate that same invoice against
    current stock (the "Re-check batches & repost" action), it would find its
    own pieces taken and report a shortage that does not exist.
    """

    def _log(self, so_number, status, batch, quantity):
        return InvoiceLog.objects.create(
            so_number=so_number, party_name='ACME', total_amount=100,
            status=status, warehouse='DL-MP', branch='OIL',
            created_by=self.user,
            invoice_payload={
                'DocumentLines': [{
                    'ItemCode': 'FG001',
                    'WarehouseCode': 'DL-MP',
                    'BatchNumbers': [{'BatchNumber': batch, 'Quantity': quantity}],
                }],
            },
        )

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='batch-reader', password='x')
        # Created after the user: the log requires an author.
        self.failed = self._log('SO-FAILED', 'ERROR', 'B-1', 40)
        self.other = self._log('SO-OTHER', 'PENDING', 'B-1', 10)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _get(self, query=''):
        response = self.client.get(f'/api/invoice/reserved-batches/{query}')
        self.assertEqual(response.status_code, 200)
        return {entry['batch_number']: entry['quantity'] for entry in response.json()['data']}

    def test_without_the_parameter_both_logs_hold_their_share(self):
        self.assertEqual(self._get(), {'B-1': 50})

    def test_excluding_a_log_frees_exactly_its_own_pieces(self):
        # The other draft's 10 stay held; only the failed log's 40 come back.
        self.assertEqual(self._get(f'?exclude_log={self.failed.pk}'), {'B-1': 10})

    def test_a_non_numeric_exclude_log_is_ignored_rather_than_erroring(self):
        # A malformed parameter must not blank the holds and let two invoices
        # allocate the same pieces.
        self.assertEqual(self._get('?exclude_log=abc'), {'B-1': 50})


class PostToSapTests(TestCase):
    """`POST /api/invoice/<id>/post-to-sap/` posts a log to SAP once.

    Live history shows posted invoices being posted again minutes later, and
    posted logs knocked back to ERROR by the repeat's failure. SAP refused
    those repeats only because the stock was gone; a partial invoice gets
    through. Nothing here reaches SAP or HANA: the session and the lookups
    are stubbed.
    """

    PAYLOAD = {
        'CardCode': 'C1', 'DocDate': '2026-09-01', 'TaxDate': '2026-09-01',
        'DocDueDate': '2026-09-11',  # 10 days of credit
        'DocumentLines': [{'ItemCode': 'FG1', 'Quantity': 5, 'BaseEntry': 7, 'BaseLine': 0}],
    }

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='poster', password='x')
        self.log = InvoiceLog.objects.create(
            so_number='SO-1', party_name='ACME', total_amount=100, status='APPROVED',
            invoice_payload=self.PAYLOAD, created_by=self.user, warehouse='DL-MP',
            branch='OIL', error_message='old failure',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.sent = []
        self.answer = None  # a response, or an exception to raise
        self._stub('invoice.services.sap_post.company_has_oms_ref', return_value=True)
        self._stub('invoice.services.sap_post._maybe_auto_irn')
        self.lookup = self._stub('invoice.services.sap_post.find_posted_invoice', return_value=None)
        self._stub('invoice.services.sap_post.SAPServiceLayerManager.get_session_for',
                   side_effect=lambda *a: self._session())

    def _stub(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _session(self):
        test = self

        class Session:
            def post(self, url, json=None, timeout=None):
                if url.endswith('/Logout'):
                    return mock.Mock(status_code=204)
                test.sent.append(json)
                if isinstance(test.answer, Exception):
                    raise test.answer
                return test.answer
        return Session()

    def _reply(self, status_code, body):
        return mock.Mock(status_code=status_code, json=mock.Mock(return_value=body), text='')

    def _post(self):
        return self.client.post(f'/api/invoice/{self.log.pk}/post-to-sap/', format='json')

    def _age_posting_mark(self):
        InvocieHistory.objects.filter(invoice_log=self.log, status='POSTING').update(
            created_at=timezone.now() - datetime.timedelta(minutes=10))

    def test_a_post_records_sap_numbers_and_who_posted(self):
        self.answer = self._reply(201, {'DocNum': 626000001, 'DocEntry': 90001})
        self.assertEqual(self._post().status_code, 201)
        self.log.refresh_from_db()
        self.assertEqual(
            (self.log.status, self.log.sap_doc_num, self.log.sap_doc_entry, self.log.error_message),
            ('POSTED_TO_SAP', '626000001', '90001', None))
        history = list(self.log.history.order_by('id').values_list('status', 'created_by'))
        self.assertEqual(history, [('POSTING', 'poster'), ('POSTED_TO_SAP', 'poster')])

    def test_the_stored_payload_goes_out_dated_today_and_stamped(self):
        self.answer = self._reply(201, {'DocNum': 1, 'DocEntry': 1})
        with mock.patch('invoice.services.sap_post.timezone.now',
                        return_value=datetime.datetime(2026, 9, 5, 20, 0, tzinfo=datetime.timezone.utc)):
            self._post()
        body = self.sent[0]
        # 20:00 UTC is already the 6th in India; the 10-day term is kept.
        self.assertEqual((body['DocDate'], body['TaxDate'], body['DocDueDate']),
                         ('2026-09-06', '2026-09-06', '2026-09-16'))
        self.assertEqual(body['U_OMS_REF'], f'OMSINV{self.log.pk}')
        self.log.refresh_from_db()
        self.assertEqual(self.log.invoice_payload, self.PAYLOAD)

    def test_no_stamp_where_the_company_lacks_the_field(self):
        self.answer = self._reply(201, {'DocNum': 1, 'DocEntry': 1})
        with mock.patch('invoice.services.sap_post.company_has_oms_ref', return_value=False):
            self._post()
        self.assertNotIn('U_OMS_REF', self.sent[0])

    def test_a_posted_invoice_is_never_sent_again(self):
        self.answer = self._reply(201, {'DocNum': 1, 'DocEntry': 1})
        self._post()
        self.assertEqual(self._post().status_code, 409)
        self.assertEqual(len(self.sent), 1)

    def test_an_unapproved_invoice_is_not_sent(self):
        InvoiceLog.objects.filter(pk=self.log.pk).update(status='PENDING')
        self.assertEqual(self._post().status_code, 409)
        self.assertEqual(self.sent, [])

    def test_a_refusal_records_sap_message_as_error(self):
        self.answer = self._reply(400, {'error': {'code': -10, 'message': 'Quantity falls into negative inventory'}})
        self.assertEqual(self._post().status_code, 400)
        self.log.refresh_from_db()
        self.assertEqual((self.log.status, self.log.error_message),
                         ('ERROR', 'Quantity falls into negative inventory'))

    def test_a_refusal_keeps_an_invoice_with_a_credit_request_on_cl_raised(self):
        CreditLimitLogs.objects.create(invoice_log=self.log, jsap_doc_id=5, party_name='ACME', created_by=self.user)
        self.answer = self._reply(400, {'error': {'message': 'Credit Limit Exceeded!'}})
        self._post()
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'CL_RAISED')

    def test_a_timeout_holds_the_invoice_until_sap_is_checked(self):
        self.answer = requests.exceptions.ReadTimeout('read timed out')
        self.assertEqual(self._post().status_code, 504)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'POSTING')
        # Straight away: refused, nothing sent.
        self.assertEqual(self._post().status_code, 409)
        self.assertEqual(len(self.sent), 1)

    def test_a_stale_posting_that_sap_has_is_recorded_not_reposted(self):
        self.answer = requests.exceptions.ReadTimeout('read timed out')
        self._post()
        self._age_posting_mark()
        self.lookup.return_value = {'DocNum': 626000009, 'DocEntry': 90009}
        response = self._post()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['reconciled'])
        self.assertEqual(len(self.sent), 1)
        self.log.refresh_from_db()
        self.assertEqual((self.log.status, self.log.sap_doc_num), ('POSTED_TO_SAP', '626000009'))

    def test_a_stale_posting_sap_does_not_have_is_posted_again(self):
        self.answer = requests.exceptions.ReadTimeout('read timed out')
        self._post()
        self._age_posting_mark()
        self.answer = self._reply(201, {'DocNum': 2, 'DocEntry': 2})
        self.assertEqual(self._post().status_code, 201)
        self.assertEqual(len(self.sent), 2)

    def test_a_stale_posting_sap_cannot_confirm_is_not_reposted(self):
        self.answer = requests.exceptions.ReadTimeout('read timed out')
        self._post()
        self._age_posting_mark()
        self.lookup.side_effect = RuntimeError('HANA down')
        self.assertEqual(self._post().status_code, 503)
        self.assertEqual(len(self.sent), 1)

    def test_a_posted_invoice_cannot_be_moved_or_edited(self):
        InvoiceLog.objects.filter(pk=self.log.pk).update(status='POSTED_TO_SAP', sap_doc_num='1')
        moved = self.client.patch(f'/api/invoice/{self.log.pk}/update-status/',
                                  {'status': 'ERROR', 'error_message': 'x'}, format='json')
        edited = self.client.patch(f'/api/invoice/log/{self.log.pk}/',
                                   {'invoice_payload': {}}, format='json')
        self.assertEqual((moved.status_code, edited.status_code), (409, 409))
        self.log.refresh_from_db()
        self.assertEqual((self.log.status, self.log.invoice_payload), ('POSTED_TO_SAP', self.PAYLOAD))
