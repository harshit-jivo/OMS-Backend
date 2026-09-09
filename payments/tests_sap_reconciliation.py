"""Tests for SAP-side cancellation detection.

NO DATABASE. Every test drives `sap_reconciliation` against fakes, because this
project has no test database: the DB user cannot CREATE DATABASE and savepoint
wrappers have been proved to leak fixtures into live data. `SimpleTestCase`
plus fakes verifies the decision logic — which is where the risk lives — with
no possibility of contamination.

What is pinned here:

  A. POSTED + SAP Canceled='N'  -> stays POSTED
  B. POSTED + SAP Canceled='Y'  -> CANCELLED_IN_SAP
  C. the original sap_response is never rewritten
  D. DocEntry / DocNum / TransId are never cleared
  E. a SAP_CANCELLED history row is written, carrying the SAP identifiers
  F. an unreadable SAP row (down / missing) changes nothing
  G. re-running is idempotent — no duplicate history rows
"""
from datetime import datetime
from unittest.mock import patch

from django.test import SimpleTestCase

from payments import sap_reconciliation


POSTED_RESPONSE = 'Payment posted to SAP as document 826246657.'


class _Status:
    POSTED = 'POSTED'
    CANCELLED_IN_SAP = 'CANCELLED_IN_SAP'
    PENDING_ERROR = 'PENDING_ERROR'


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class _Manager:
    """Stands in for `Model.objects` — only what the service calls."""

    def __init__(self, document):
        self._document = document

    def select_for_update(self):
        return self

    def get(self, pk=None):
        return self._document


class _FakeDocument:
    """A posted receipt, with just the attributes the service reads."""

    Status = _Status

    def __init__(self, *, status=_Status.POSTED, doc_entry=21947):
        self.pk = 1
        self.status = status
        self.company = 'OIL'
        self.receipt_no = 'RCP-OIL-20260821-000002'
        self.sap_doc_entry = doc_entry
        self.sap_doc_num = 826246657
        self.sap_trans_id = 227476
        self.sap_response = POSTED_RESPONSE
        self.sap_cancelled_at = None
        self.sap_cancellation_response = ''
        self.sap_reconciled_at = None
        self.saved_fields = []

    def save(self, update_fields=None):
        self.saved_fields.append(list(update_fields or []))


def _wire(document):
    """Give the instance a class exposing Status and objects, as the service expects."""
    document.__class__ = type(
        'FakeReceipt', (_FakeDocument,),
        {'Status': _Status, 'objects': _Manager(document)},
    )
    return document


class CancellationDetectionTests(SimpleTestCase):

    def setUp(self):
        self.history = []

        def _capture(document, **kwargs):
            self.history.append(kwargs)
            return None

        patcher = patch.object(sap_reconciliation, 'log_status', _capture)
        patcher.start()
        self.addCleanup(patcher.stop)

        # transaction.atomic would need a real DB connection; the service's use
        # of it is a no-op for these fakes.
        atomic = patch.object(sap_reconciliation.transaction, 'atomic',
                              lambda *a, **k: _NullContext())
        atomic.start()
        self.addCleanup(atomic.stop)

    def _run(self, document, sap_row):
        with patch.object(sap_reconciliation.hana_queries,
                          'fetch_payment_cancellation',
                          return_value=sap_row) as fetch:
            changed = sap_reconciliation.reconcile_document(document)
        return changed, fetch

    # -- A ---------------------------------------------------------------
    def test_not_cancelled_stays_posted(self):
        doc = _wire(_FakeDocument())
        changed, _ = self._run(doc, {'canceled': 'N', 'cancel_date': None})

        self.assertFalse(changed)
        self.assertEqual(doc.status, _Status.POSTED)
        self.assertEqual(doc.sap_cancellation_response, '')
        self.assertIsNone(doc.sap_cancelled_at)
        self.assertEqual(self.history, [])

    # -- B ---------------------------------------------------------------
    def test_cancelled_in_sap_sets_status(self):
        doc = _wire(_FakeDocument())
        cancel_date = datetime(2026, 8, 8, 0, 0)
        changed, _ = self._run(doc, {'canceled': 'Y',
                                     'cancel_date': cancel_date})

        self.assertTrue(changed)
        self.assertEqual(doc.status, _Status.CANCELLED_IN_SAP)
        self.assertIsNotNone(doc.sap_cancelled_at)
        self.assertIn('posted successfully but was later cancelled',
                      doc.sap_cancellation_response)

    def test_cancellation_is_not_a_posting_failure(self):
        doc = _wire(_FakeDocument())
        self._run(doc, {'canceled': 'Y', 'cancel_date': None})
        self.assertNotEqual(doc.status, _Status.PENDING_ERROR)

    # -- C ---------------------------------------------------------------
    def test_original_sap_response_is_preserved(self):
        doc = _wire(_FakeDocument())
        self._run(doc, {'canceled': 'Y', 'cancel_date': None})

        self.assertEqual(doc.sap_response, POSTED_RESPONSE)
        for fields in doc.saved_fields:
            self.assertNotIn('sap_response', fields)

    # -- D ---------------------------------------------------------------
    def test_sap_identifiers_are_preserved(self):
        doc = _wire(_FakeDocument())
        self._run(doc, {'canceled': 'Y', 'cancel_date': None})

        self.assertEqual(doc.sap_doc_entry, 21947)
        self.assertEqual(doc.sap_doc_num, 826246657)
        self.assertEqual(doc.sap_trans_id, 227476)
        for fields in doc.saved_fields:
            for key in ('sap_doc_entry', 'sap_doc_num', 'sap_trans_id'):
                self.assertNotIn(key, fields)

    # -- E ---------------------------------------------------------------
    def test_history_row_records_the_cancellation(self):
        doc = _wire(_FakeDocument())
        self._run(doc, {'canceled': 'Y', 'cancel_date': None})

        self.assertEqual(len(self.history), 1)
        row = self.history[0]
        self.assertEqual(row['from_status'], _Status.POSTED)
        self.assertEqual(row['to_status'], _Status.CANCELLED_IN_SAP)
        self.assertEqual(row['actor_kind'], 'SYSTEM')
        self.assertEqual(row['sap_doc_entry'], 21947)
        self.assertEqual(row['sap_doc_num'], 826246657)
        self.assertIn('cancelled in SAP', row['reason'])

    # -- F ---------------------------------------------------------------
    def test_unreadable_sap_row_changes_nothing(self):
        doc = _wire(_FakeDocument())
        changed, _ = self._run(doc, None)

        self.assertFalse(changed)
        self.assertEqual(doc.status, _Status.POSTED)
        self.assertEqual(self.history, [])

    def test_never_cancels_on_a_sap_error(self):
        """An exception reading SAP must not downgrade the document."""
        doc = _wire(_FakeDocument())
        with patch.object(sap_reconciliation.hana_queries,
                          'fetch_payment_cancellation',
                          side_effect=RuntimeError('SAP down')):
            with self.assertRaises(RuntimeError):
                sap_reconciliation.reconcile_document(doc)
        self.assertEqual(doc.status, _Status.POSTED)

    # -- G ---------------------------------------------------------------
    def test_idempotent_when_already_cancelled(self):
        doc = _wire(_FakeDocument(status=_Status.CANCELLED_IN_SAP))
        changed, fetch = self._run(doc, {'canceled': 'Y',
                                         'cancel_date': None})

        self.assertFalse(changed)
        self.assertEqual(self.history, [])
        fetch.assert_not_called()

    def test_second_run_writes_no_duplicate_history(self):
        doc = _wire(_FakeDocument())
        self._run(doc, {'canceled': 'Y', 'cancel_date': None})
        self._run(doc, {'canceled': 'Y', 'cancel_date': None})

        self.assertEqual(len(self.history), 1)

    # -- guards ----------------------------------------------------------
    def test_document_without_doc_entry_is_skipped(self):
        doc = _wire(_FakeDocument(doc_entry=None))
        changed, fetch = self._run(doc, {'canceled': 'Y'})

        self.assertFalse(changed)
        fetch.assert_not_called()

    def test_uses_server_side_doc_entry_and_company(self):
        """Section 4 — never a client-supplied DocEntry or database."""
        doc = _wire(_FakeDocument())
        _, fetch = self._run(doc, {'canceled': 'N'})

        fetch.assert_called_once_with(company='OIL', doc_entry=21947)
