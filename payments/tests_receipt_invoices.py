"""Tests for the Paid Invoices rows on the receipt PDF.

NO DATABASE. This project has no test database (the DB user cannot CREATE
DATABASE) and savepoint wrappers have been proved to leak fixtures into live
data, so these drive `receipt_invoices` against fakes. That is where the whole
risk lives: which source a value comes from, and what happens when SAP cannot
answer.

Pinned here:

  A. invoice payment      -> number, date, total and applied all present
  B. partial payment      -> Invoice Total != Amount Applied, both kept
  C. full payment         -> equal values still reported separately
  D. multiple invoices    -> one row each, never collapsed
  E. advance              -> no invoice rows at all
  F. missing metadata     -> nothing invented; number + applied still shown
  G. SAP wins over a stale OMS snapshot
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase

from payments import receipt_invoices


class _Allocation:
    """Stand-in for PaymentAllocation — only the attributes read."""

    def __init__(self, *, sap_doc_entry, sap_doc_num=None, invoice_date=None,
                 invoice_total=Decimal('0'), amount_applied=Decimal('0')):
        self.sap_doc_entry = sap_doc_entry
        self.sap_doc_num = sap_doc_num
        self.invoice_date = invoice_date
        self.invoice_total = invoice_total
        self.amount_applied = amount_applied


class _Receipt:
    def __init__(self, *, company='BEVERAGES', currency='INR', is_advance=False):
        self.company = company
        self.currency = currency
        self.is_advance = is_advance


def _rows(receipt, allocations, sap_details):
    with patch.object(receipt_invoices.hana_queries, 'fetch_invoice_details',
                      return_value=sap_details) as fetch:
        rows = receipt_invoices.invoice_rows_for(
            receipt, allocations=allocations)
    return rows, fetch


class InvoiceRowTests(SimpleTestCase):

    # -- A ---------------------------------------------------------------
    def test_invoice_payment_has_all_four_values(self):
        """The real receipt: RCP-BEVERAGES-20260821-000001, invoice 625078180."""
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=7077, sap_doc_num=625078180,
                         amount_applied=Decimal('42000.00'))],
            {7077: {'doc_num': 625078180, 'doc_date': date(2025, 7, 31),
                    'doc_total': Decimal('42000.00'), 'currency': 'INR'}},
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['invoice_no'], '625078180')
        self.assertEqual(row['invoice_date'], date(2025, 7, 31))
        self.assertEqual(row['invoice_total'], Decimal('42000.00'))
        self.assertEqual(row['amount_applied'], Decimal('42000.00'))

    def test_invoice_number_is_never_the_payment_doc_entry(self):
        """The bug this fixes: the PDF printed the PAYMENT's DocEntry."""
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=7077, sap_doc_num=625078180,
                         amount_applied=Decimal('1.00'))],
            {7077: {'doc_num': 625078180, 'doc_date': None,
                    'doc_total': None, 'currency': 'INR'}},
        )
        self.assertEqual(rows[0]['invoice_no'], '625078180')
        self.assertNotEqual(rows[0]['invoice_no'], '6400')

    # -- B ---------------------------------------------------------------
    def test_partial_payment_keeps_total_and_applied_distinct(self):
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=900, sap_doc_num=6400,
                         amount_applied=Decimal('30000.00'))],
            {900: {'doc_num': 6400, 'doc_date': date(2026, 8, 21),
                   'doc_total': Decimal('50000.00'), 'currency': 'INR'}},
        )

        row = rows[0]
        self.assertEqual(row['invoice_total'], Decimal('50000.00'))
        self.assertEqual(row['amount_applied'], Decimal('30000.00'))
        self.assertNotEqual(row['invoice_total'], row['amount_applied'])

    def test_total_is_never_derived_from_amount_applied(self):
        """With no SAP total and no snapshot, the total stays unknown."""
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=901, sap_doc_num=6401,
                         invoice_total=Decimal('0'),
                         amount_applied=Decimal('7500.00'))],
            {},
        )
        self.assertIsNone(rows[0]['invoice_total'])
        self.assertEqual(rows[0]['amount_applied'], Decimal('7500.00'))

    # -- C ---------------------------------------------------------------
    def test_full_payment_reports_both_values_separately(self):
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=902, sap_doc_num=6402,
                         amount_applied=Decimal('20000.00'))],
            {902: {'doc_num': 6402, 'doc_date': date(2026, 8, 22),
                   'doc_total': Decimal('20000.00'), 'currency': 'INR'}},
        )
        row = rows[0]
        self.assertEqual(row['invoice_total'], Decimal('20000.00'))
        self.assertEqual(row['amount_applied'], Decimal('20000.00'))

    # -- D ---------------------------------------------------------------
    def test_multiple_invoices_each_get_a_row(self):
        rows, fetch = _rows(
            _Receipt(),
            [
                _Allocation(sap_doc_entry=900, sap_doc_num=6400,
                            amount_applied=Decimal('30000.00')),
                _Allocation(sap_doc_entry=912, sap_doc_num=6412,
                            amount_applied=Decimal('20000.00')),
            ],
            {
                900: {'doc_num': 6400, 'doc_date': date(2026, 8, 21),
                      'doc_total': Decimal('50000.00'), 'currency': 'INR'},
                912: {'doc_num': 6412, 'doc_date': date(2026, 8, 22),
                      'doc_total': Decimal('20000.00'), 'currency': 'INR'},
            },
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['invoice_no'], '6400')
        self.assertEqual(rows[0]['amount_applied'], Decimal('30000.00'))
        self.assertEqual(rows[1]['invoice_no'], '6412')
        self.assertEqual(rows[1]['amount_applied'], Decimal('20000.00'))

    def test_all_invoices_resolved_in_one_query(self):
        """No N+1: one batched read for the whole receipt."""
        _, fetch = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=n, sap_doc_num=n,
                         amount_applied=Decimal('1.00'))
             for n in (900, 901, 902, 903)],
            {},
        )
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['doc_entries'],
                         [900, 901, 902, 903])

    # -- E ---------------------------------------------------------------
    def test_advance_has_no_invoice_rows(self):
        rows, fetch = _rows(_Receipt(is_advance=True), [], {})
        self.assertEqual(rows, [])
        fetch.assert_not_called()

    # -- F ---------------------------------------------------------------
    def test_missing_metadata_invents_nothing(self):
        """SAP unreachable and nothing snapshotted: omit only what is unknown."""
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=7077, sap_doc_num=625078180,
                         invoice_date=None, invoice_total=Decimal('0'),
                         amount_applied=Decimal('42000.00'))],
            {},
        )

        row = rows[0]
        self.assertIsNone(row['invoice_date'])
        self.assertIsNone(row['invoice_total'])
        # Still identifiable and still shows what was applied.
        self.assertEqual(row['invoice_no'], '625078180')
        self.assertEqual(row['amount_applied'], Decimal('42000.00'))

    def test_falls_back_to_oms_snapshot_when_sap_is_silent(self):
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=7077, sap_doc_num=625078180,
                         invoice_date=date(2025, 7, 31),
                         invoice_total=Decimal('42000.00'),
                         amount_applied=Decimal('42000.00'))],
            {},
        )
        self.assertEqual(rows[0]['invoice_date'], date(2025, 7, 31))
        self.assertEqual(rows[0]['invoice_total'], Decimal('42000.00'))

    # -- G ---------------------------------------------------------------
    def test_sap_wins_over_a_stale_snapshot(self):
        rows, _ = _rows(
            _Receipt(),
            [_Allocation(sap_doc_entry=7077, sap_doc_num=625078180,
                         invoice_date=date(2020, 1, 1),
                         invoice_total=Decimal('11111.00'),
                         amount_applied=Decimal('42000.00'))],
            {7077: {'doc_num': 625078180, 'doc_date': date(2025, 7, 31),
                    'doc_total': Decimal('42000.00'), 'currency': 'INR'}},
        )
        self.assertEqual(rows[0]['invoice_date'], date(2025, 7, 31))
        self.assertEqual(rows[0]['invoice_total'], Decimal('42000.00'))

    def test_currency_follows_the_receipt_when_sap_gives_none(self):
        rows, _ = _rows(
            _Receipt(currency='USD'),
            [_Allocation(sap_doc_entry=903, sap_doc_num=6403,
                         amount_applied=Decimal('10.00'))],
            {},
        )
        self.assertEqual(rows[0]['currency'], 'USD')
