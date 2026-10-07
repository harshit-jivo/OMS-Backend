"""A bill's SAP attachments, all of them, and one PO in full.

No SAP: `sap._run` is stood in for, answering by the SQL it is given.
"""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.services import sap


def _line(entry, num, line, name, ext='pdf'):
    return {'doc_entry': entry, 'doc_num': num, 'line': line, 'file_name': name, 'file_ext': ext,
            'date': datetime(2026, 9, 24), 'note': ''}


#: Bill 52169 (726094109) was made from GRPO 27276, which was made from PO 12556.
LINES = {
    'OPCH': [_line(52169, 726094109, 1, '21645644'), _line(52169, 726094109, 2, 'approval mail', 'msg')],
    'OPDN': [_line(27276, 2026096833, 1, 'delivery challan')],
    'OPOR': [_line(12556, 220726044, 1, 'quotation')],
}


def _fake_run(company, sql, params, what):
    if 'PCH1' in sql and 'UNION' in sql:  # the bill's bases
        return [{'kind': 'grpo', 'doc_entry': 27276, 'doc_num': 2026096833},
                {'kind': 'po', 'doc_entry': 12556, 'doc_num': 220726044}]
    for table, rows in LINES.items():
        if f'."{table}" D' in sql:
            return [r for r in rows if r['doc_entry'] in params]
    raise AssertionError(f'unexpected SQL for {what}')


@mock.patch.object(sap, '_schema', return_value='JIVO_OIL_HANADB')
@mock.patch.object(sap, '_run', side_effect=_fake_run)
class EveryAttachmentOfABill(SimpleTestCase):
    def test_lists_the_bills_own_then_its_grpos_then_its_pos(self, _run, _schema):
        found = sap.related_attachments('OIL', 'bill', '52169')
        self.assertEqual(
            [(a['kind'], a['doc_num'], a['line'], a['file_name']) for a in found],
            [('bill', 726094109, 1, '21645644.pdf'), ('bill', 726094109, 2, 'approval mail.msg'),
             ('grpo', 2026096833, 1, 'delivery challan.pdf'), ('po', 220726044, 1, 'quotation.pdf')])
        self.assertEqual(found[2]['kind_label'], 'Goods receipt PO')

    def test_a_po_lists_only_its_own(self, _run, _schema):
        found = sap.related_attachments('OIL', 'po', 12556)
        self.assertEqual([(a['kind'], a['line']) for a in found], [('po', 1)])

    def test_one_line_is_found_by_document_and_line(self, _run, _schema):
        self.assertEqual(sap.attachment_line('OIL', 'grpo', 27276, '1')['file_name'], 'delivery challan.pdf')
        self.assertIsNone(sap.attachment_line('OIL', 'bill', 52169, 9))

    def test_refuses_a_kind_or_line_it_does_not_know(self, _run, _schema):
        with self.assertRaisesRegex(ValueError, 'kind must be one of'):
            sap.related_attachments('OIL', 'grpo', 27276)
        with self.assertRaisesRegex(ValueError, 'kind must be one of'):
            sap.attachment_line('OIL', 'OPCH', 52169, 1)
        with self.assertRaisesRegex(ValueError, 'line must be a number'):
            sap.attachment_line('OIL', 'bill', 52169, 'x')


PO_HEADER = {
    'doc_entry': 12556, 'doc_num': 220726044, 'status': 'C', 'cancelled': 'N',
    'doc_date': datetime(2026, 7, 1), 'delivery_date': datetime(2026, 7, 1), 'document_date': datetime(2026, 7, 1),
    'created_on': datetime(2026, 7, 11), 'card_code': 'VENDA000657', 'card_name': 'INFINITE SOLUTIONS',
    'vendor_ref': None, 'currency': 'INR', 'rate': 1, 'doc_total': 15340, 'tax': 2340, 'discount_percent': 0,
    'discount': 0, 'freight': 0, 'rounding': 0, 'tds': 0, 'paid_to_date': 15340, 'down_payment': 0,
    'branch': 'DELHI', 'pay_to': 'NEW DELHI-110058\rIN', 'ship_to': 'A-35/1\rNew Delhi\rIN',
    'remarks': '', 'journal_memo': 'Purchase Orders - VENDA000657', 'payment_terms': 'ADVANCE/CASH/0 DAYS',
    'buyer': '-No Sales Employee / Buyer-', 'owner': '', 'created_by': 'HARSH',
}
PO_LINE = {
    'line': 0, 'item_code': 'CG0000008', 'description': 'COMPUTER AND HARDWARE', 'quantity': 1,
    'open_quantity': 0, 'unit': 'PCS', 'price_before_discount': 13000, 'discount_percent': 0, 'price': 13000,
    'line_total': 13000, 'tax_code': 'CG+SG@18', 'tax_percent': 18, 'tax': 2340, 'gross_total': 15340,
    'warehouse': 'DL-FA', 'delivery_date': datetime(2026, 7, 1), 'status': 'C', 'account': '5680022',
    'budget': 'BackOff', 'sub_budget': 'IT', 'note': None,
}


def _po_run(company, sql, params, what):
    if what == 'purchase order':
        return [PO_HEADER] if params == [12556] else []
    if what == 'purchase order lines':
        return [PO_LINE]
    if what == 'purchase order follow-on':
        return [{'kind': 'grpo', 'doc_entry': 24675, 'doc_num': 2026076587, 'doc_date': datetime(2026, 7, 2),
                 'doc_total': 15340, 'status': 'C', 'cancelled': 'N', 'vendor_ref': 'IS-GST-2627-0175'},
                {'kind': 'bill', 'doc_entry': 48410, 'doc_num': 726074106, 'doc_date': datetime(2026, 7, 2),
                 'doc_total': 15340, 'status': 'O', 'cancelled': 'N', 'vendor_ref': 'IS-GST-2627-0175'}]
    if what == 'document attachments':
        return [_line(12556, 220726044, 1, 'quotation')]
    raise AssertionError(what)


@mock.patch.object(sap, '_schema', return_value='JIVO_OIL_HANADB')
@mock.patch.object(sap, '_run', side_effect=_po_run)
class OnePurchaseOrder(SimpleTestCase):
    def test_reads_the_header_lines_follow_on_and_attachments(self, _run, _schema):
        po = sap.purchase_order('OIL', '12556')
        h = po['header']
        self.assertEqual((h['doc_num'], h['status'], h['doc_total'], h['tax']), (220726044, 'Closed', '15340', '2340'))
        self.assertEqual(h['ship_to'], 'A-35/1\nNew Delhi\nIN')  # SAP's bare CRs, as lines
        self.assertEqual(h['buyer'], '')  # SAP's "no buyer" placeholder is not a name
        self.assertEqual(h['vendor_ref'], '')
        self.assertEqual(po['lines'][0]['quantity'], '1')
        self.assertEqual(po['lines'][0]['status'], 'Closed')
        self.assertEqual([(f['kind'], f['status']) for f in po['follow_on']], [('grpo', 'Closed'), ('bill', 'Open')])
        self.assertEqual(po['attachments'][0]['file_name'], 'quotation.pdf')

    def test_none_when_sap_has_no_such_po(self, _run, _schema):
        self.assertIsNone(sap.purchase_order('OIL', 1))


@mock.patch.object(views.ap_perms.CanViewLookups, 'has_permission', return_value=True)
class TheEndpoints(SimpleTestCase):
    def _get(self, view, path):
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True))
        return view.as_view()(request)

    def test_lists_every_related_attachment(self, _perm):
        rows = [{'kind': 'bill', 'doc_entry': 52169, 'line': 1, 'file_name': 'a.pdf'}]
        with mock.patch.object(views.sap_service, 'related_attachments', return_value=rows) as related:
            response = self._get(views.DocumentAttachmentsView,
                                 '/api/advance-payments/document-attachments/?company=OIL&kind=bill&doc_entry=52169')
        related.assert_called_once_with('OIL', 'bill', '52169')
        self.assertEqual(response.data['data']['results'], rows)

    def test_opens_one_line_by_document_and_line(self, _perm):
        meta = {'file_name': 'delivery challan.pdf'}
        with mock.patch.object(views.sap_service, 'attachment_line', return_value=meta) as line, \
                mock.patch.object(views.attachment_files, 'fetch', return_value=(b'%PDF-', 'application/pdf')) as fetch:
            response = self._get(
                views.DocumentAttachmentView,
                '/api/advance-payments/document-attachment/?company=OIL&kind=grpo&doc_entry=27276&line=1')
        line.assert_called_once_with('OIL', 'grpo', '27276', '1')
        fetch.assert_called_once_with('OIL', 'delivery challan.pdf')  # the name SAP gave, never the caller's
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('X-Attachment-Count', response)

    def test_a_line_that_does_not_exist_is_404(self, _perm):
        with mock.patch.object(views.sap_service, 'attachment_line', return_value=None):
            response = self._get(
                views.DocumentAttachmentView,
                '/api/advance-payments/document-attachment/?company=OIL&kind=bill&doc_entry=52169&line=9')
        self.assertEqual(response.status_code, 404)

    def test_the_purchase_order(self, _perm):
        po = {'header': {'doc_num': 220726044}, 'lines': [], 'follow_on': [], 'attachments': []}
        with mock.patch.object(views.sap_service, 'purchase_order', return_value=po):
            response = self._get(views.PurchaseOrderView,
                                 '/api/advance-payments/purchase-order/?company=OIL&doc_entry=12556')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['header'], {'doc_num': 220726044})

    def test_a_purchase_order_sap_does_not_have_is_404(self, _perm):
        with mock.patch.object(views.sap_service, 'purchase_order', return_value=None):
            response = self._get(views.PurchaseOrderView,
                                 '/api/advance-payments/purchase-order/?company=OIL&doc_entry=1')
        self.assertEqual(response.status_code, 404)


class VendorOnAccount(SimpleTestCase):
    """The vendor's ledger: money paid but not yet adjusted, for a person to judge against the POs."""

    def test_reads_open_debits_not_credit_memos_newest_first(self):
        rows = [{'trans_id': 238072, 'line_id': 1, 'trans_type': '46', 'base_ref': '1026466574',
                 'posting_date': datetime(2026, 10, 6), 'debit': Decimal('802400'),
                 'open': Decimal('802400'), 'memo': 'Outgoing Payments - VENDA001429', 'ref2': ''}]
        with mock.patch.object(sap, '_run', return_value=rows) as run, \
                mock.patch.object(sap, '_schema', return_value='S'):
            got = sap.vendor_on_account('OIL', 'VENDA001429')
        sql = run.call_args.args[1]
        self.assertIn('"BalDueDeb" > 0', sql)
        self.assertIn('"TransType" <> \'19\'', sql)
        self.assertEqual(got, [{'trans_id': 238072, 'line_id': 1, 'doc_type': 'Outgoing Payment',
                                'doc_type_code': 46, 'doc_num': '1026466574',
                                'posting_date': '2026-10-06', 'paid': '802400', 'open': '802400',
                                'memo': 'Outgoing Payments - VENDA001429', 'reference': ''}])

    def test_no_vendor_no_query(self):
        with mock.patch.object(sap, '_run') as run:
            self.assertEqual(sap.vendor_on_account('OIL', ' '), [])
        run.assert_not_called()
