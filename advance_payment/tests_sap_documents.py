"""A bill's SAP attachments, all of them, and one PO in full.

No SAP: `sap._run` is stood in for, answering by the SQL it is given.
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.services import sap
from advance_payment.services import sap_relations


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

    def test_a_journal_entrys_files_are_read_by_its_trans_id(self, _run, _schema):
        # 2026-10-21: an imprest's journal entry (TA/DA, incentives) shows its attachments too.
        rows = [{'doc_entry': 90910, 'doc_num': 926890238, 'line': 1, 'file_name': 'Recharge Approval',
                 'file_ext': 'pdf', 'date': datetime(2026, 10, 6), 'note': ''}]
        _run.side_effect = lambda company, sql, params, what: rows
        found = sap.related_attachments('MART', 'journal', 90910)
        sql = _run.call_args.args[1]
        self.assertIn('FROM "JIVO_OIL_HANADB"."OJDT" D', sql)
        self.assertIn('D."TransId" AS "doc_entry", D."Number" AS "doc_num"', sql)
        self.assertIn('WHERE D."TransId" IN (?)', sql)
        self.assertEqual([(a['kind'], a['kind_label'], a['doc_num'], a['file_name']) for a in found],
                         [('journal', 'Journal entry', 926890238, 'Recharge Approval.pdf')])
        self.assertEqual(sap.attachment_line('MART', 'journal', 90910, 1)['file_name'], 'Recharge Approval.pdf')

    def test_one_line_is_found_by_document_and_line(self, _run, _schema):
        self.assertEqual(sap.attachment_line('OIL', 'grpo', 27276, '1')['file_name'], 'delivery challan.pdf')
        self.assertIsNone(sap.attachment_line('OIL', 'bill', 52169, 9))

    def test_refuses_a_kind_or_line_it_does_not_know(self, _run, _schema):
        with self.assertRaisesRegex(ValueError, 'kind must be one of'):
            sap.related_attachments('OIL', 'order', 27276)  # a GRPO and a payment now list theirs
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


@mock.patch.object(sap, '_schema', return_value='JIVO_OIL_HANADB')
class TdsInSap(SimpleTestCase):
    """Whether SAP withheld TDS: on a bill itself, and on the bills raised from a PO."""

    def test_a_bill_carries_the_tds_sap_withheld(self, _schema):
        row = {'doc_entry': 7, 'doc_num': 624104114, 'doc_total': '100000', 'paid_to_date': '0',
               'balance_due': '100000', 'tds': Decimal('37500')}
        with mock.patch.object(sap, '_run', return_value=[row]), \
                mock.patch.object(sap, '_with_attachments', side_effect=lambda _c, _t, rows: rows), \
                mock.patch.object(sap, '_with_debits', side_effect=lambda _c, _k, rows: rows):
            (bill,) = sap.open_invoices('OIL', 'vendor', card_code='V1')
        self.assertEqual(bill['tds'], '37500')

    def test_a_po_carries_the_tds_withheld_on_its_bills(self, _schema):
        rows = [{'doc_entry': 1}, {'doc_entry': 2}]
        found = [{'po': 1, 'tds': Decimal('12589'), 'bills': 2}]
        with mock.patch.object(sap, '_run', return_value=found) as run:
            sap._with_bill_tds('OIL', rows)
        self.assertEqual((rows[0]['tds_on_bills'], rows[0]['billed']), ('12589', 2))
        self.assertEqual((rows[1]['tds_on_bills'], rows[1]['billed']), ('0', 0))
        # One query for the whole page, the entries bound once per branch.
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[2], [1, 2, 1, 2])

    def test_an_unreadable_lookup_says_nothing_rather_than_none_deducted(self, _schema):
        rows = [{'doc_entry': 1}]
        with mock.patch.object(sap, '_run', side_effect=sap.SapUnavailable('down')):
            sap._with_bill_tds('OIL', rows)
        self.assertEqual((rows[0]['tds_on_bills'], rows[0]['billed']), (None, None))


@mock.patch.object(sap, '_schema', return_value='JIVO_OIL_HANADB')
class OneBillsBreakdown(SimpleTestCase):
    """Bill 624104114 as SAP booked it: rent, CGST + SGST at 9%, TDS 194I at 10%."""

    HEADER = [{'doc_entry': 188, 'doc_num': 624104114, 'vendor_ref': '7/2024-25', 'doc_date': None,
               'document_date': None, 'status': 'O', 'cancelled': 'N', 'card_code': 'VENDA000869',
               'card_name': 'GEETA GUPTA', 'freight': Decimal('0'), 'discount': Decimal('0'),
               'gst': Decimal('67500'), 'tds': Decimal('37500'), 'rounding': Decimal('0'),
               'net': Decimal('405000'), 'paid': Decimal('305000'),
               'payable_account': '2110004', 'payable_account_name': 'SUNDRY CREDITOR SERVICE'}]
    LINES = [{'line': 0, 'item_code': None, 'description': None, 'quantity': Decimal('0'),
              'taxable': Decimal('375000'), 'gst': Decimal('67500'), 'tax_code': 'CG+SG@18',
              'account': '5660002', 'account_name': 'RENT'}]
    GST = [{'code': 'CGST@9', 'rate': Decimal('9'), 'base': Decimal('375000'), 'amount': Decimal('33750'),
            'account': '2131012', 'account_name': 'INPUT CGST @ 9 %'}]
    TDS = [{'code': '94IB', 'name': '194I TDS ON RENT', 'rate': Decimal('10'), 'taxable': Decimal('375000'),
            'amount': Decimal('37500'), 'account': '2133001', 'account_name': 'TDS ON RENT 194I'}]

    LINKS = [{'kind': 'grpo', 'doc_entry': 27357, 'doc_num': 2026096869, 'doc_date': None, 'amount': Decimal('885000')}]

    def test_taxable_gst_tds_and_net_add_up(self, _schema):
        with mock.patch.object(sap, '_run', side_effect=[self.HEADER, self.LINES, self.GST, self.TDS, self.LINKS]):
            bill = sap.bill_breakdown('OIL', 188)
        h = bill['header']
        self.assertEqual((h['taxable'], h['gst'], h['gross'], h['tds'], h['net'], h['balance']),
                         ('375000', '67500', '442500', '37500', '405000', '100000'))
        self.assertEqual((h['payable_account'], h['status']), ('2110004', 'Open'))
        self.assertEqual((bill['lines'][0]['account'], bill['lines'][0]['account_name']), ('5660002', 'RENT'))
        self.assertEqual(bill['tds'][0]['account'], '2133001')
        self.assertEqual((bill['links'][0]['kind_label'], bill['links'][0]['doc_num']), ('Goods Receipt PO', 2026096869))

    def test_no_such_bill_is_none(self, _schema):
        with mock.patch.object(sap, '_run', return_value=[]):
            self.assertIsNone(sap.bill_breakdown('OIL', 1))



@mock.patch.object(sap, '_schema', return_value='JIVO_OIL_HANADB')
class GoodsReceiptAndPayment(SimpleTestCase):
    """The two documents a bill links to that had no window: a GRPO, a payment."""

    GRPO = [{'doc_entry': 27357, 'doc_num': 2026096869, 'vendor_ref': '', 'doc_date': None, 'due_date': None,
             'document_date': None, 'status': 'C', 'cancelled': 'N', 'card_code': 'VENDA001', 'card_name': 'PIONEER',
             'branch': 'FACTORY', 'remarks': '', 'discount': Decimal('0'), 'freight': Decimal('0'),
             'tax': Decimal('135000'), 'rounding': Decimal('0'), 'total': Decimal('885000')}]
    GRPO_LINES = [{'line': 0, 'item_code': 'PM01', 'description': 'PET', 'quantity': Decimal('10'),
                   'price': Decimal('75000'), 'line_total': Decimal('750000'), 'tax_code': 'IGST@18',
                   'tax': Decimal('135000'), 'warehouse': 'BH-FG', 'account': '5120001', 'account_name': 'PM'}]
    GRPO_LINKS = [{'kind': 'bill', 'doc_entry': 52574, 'doc_num': 626094379, 'doc_date': None, 'amount': Decimal('884250')}]

    def test_a_goods_receipt(self, _schema):
        with mock.patch.object(sap, '_run', side_effect=[self.GRPO, self.GRPO_LINES, self.GRPO_LINKS]):
            g = sap.goods_receipt('OIL', 27357)
        self.assertEqual((g['header']['status'], g['header']['before_discount'], g['header']['total']),
                         ('Closed', '750000', '885000'))
        self.assertEqual(g['links'][0]['kind_label'], 'A/P Invoice')

    def test_an_outgoing_payment(self, _schema):
        header = [{'doc_entry': 29816, 'doc_num': 1026466646, 'doc_type': 'S', 'doc_date': None, 'due_date': None,
                   'document_date': None, 'cancelled': 'N', 'card_code': 'VENDA001792', 'card_name': 'RAICON',
                   'branch': 'FACTORY', 'remarks': '', 'journal_memo': '', 'reference': '', 'trans_id': 239003,
                   'transfer_account': '1104107', 'transfer_account_name': 'ICICI', 'transfer_sum': Decimal('25000'),
                   'transfer_date': None, 'transfer_ref': '', 'cash_account': '', 'cash_account_name': '',
                   'cash_sum': Decimal('0'), 'check_sum': Decimal('0'), 'on_account': Decimal('0'),
                   'total': Decimal('25000'), 'payment_mode': 'NEFT', 'urgency': 'H',
                   'type_of_advance': 'One Time Settlement', 'settle_by': None}]
        docs = [{'inv_type': 18, 'doc_entry': 52574, 'doc_num': 626094379, 'vendor_ref': 'INV-1',
                 'applied': Decimal('25000'), 'tds': Decimal('0')}]
        with mock.patch.object(sap, '_run', side_effect=[header, docs, []]), \
                mock.patch('advance_payment.services.sap_rules.field', side_effect=lambda db, name: name):
            p = sap.outgoing_payment('OIL', 29816)
        self.assertEqual((p['header']['paid_to'], p['header']['status'], p['header']['payment_mode']),
                         ('Vendor', 'Posted', 'NEFT'))
        self.assertEqual((p['documents'][0]['kind_label'], p['documents'][0]['doc_num']), ('A/P Invoice', 626094379))

    def test_neither_is_none(self, _schema):
        with mock.patch.object(sap, '_run', return_value=[]):
            self.assertIsNone(sap.goods_receipt('OIL', 1))
            self.assertIsNone(sap.outgoing_payment('OIL', 1))



class RelationsInSap(SimpleTestCase):
    """Relationship map, journal entry, and one partner's balance across companies."""

    def setUp(self):
        # The same-code tests: no partner under another code.
        for name, value in (('_gstins', []), ('_accounts', []), ('_found_by', []), ('standing', {'since': None, 'days': None})):
            patcher = mock.patch.object(sap_relations, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_same_code_that_is_another_business_is_not_shown(self):
        # VENDA001056: S.N. INDUSTRIES in Beverages, RAUNAK NURSERY in Oil.
        partners = {('BEVERAGES', 'VENDA001056'): {'code': 'VENDA001056', 'name': 'S.N. INDUSTRIES', 'type': 'S',
                                                   'balance': Decimal('-1703088')},
                    ('OIL', 'VENDA001056'): {'code': 'VENDA001056', 'name': 'RAUNAK NURSERY', 'type': 'S',
                                             'balance': Decimal('0')}}
        pans = {('BEVERAGES', 'VENDA001056'): 'AANPG8531F', ('OIL', 'VENDA001056'): ''}
        with mock.patch.object(sap_relations, '_partner', side_effect=lambda c, code: partners.get((c, code))), \
                mock.patch.object(sap_relations, '_pan', side_effect=lambda c, code: pans.get((c, code), '')):
            rows = sap_relations.partner_balances('BEVERAGES', 'VENDA001056')
        self.assertEqual([r['company'] for r in rows], ['BEVERAGES'])

    def test_the_same_code_and_pan_shows_both_companies(self):
        partner = {'code': 'VENDA000279', 'name': 'DIL EXIM', 'type': 'S', 'balance': Decimal('25699905')}
        with mock.patch.object(sap_relations, '_partner', return_value=partner), \
                mock.patch.object(sap_relations, '_pan', return_value='AAHCD7279A'):
            rows = sap_relations.partner_balances('OIL', 'VENDA000279')
        self.assertEqual([(r['company'], r['matched_by']) for r in rows], [('OIL', 'self'), ('BEVERAGES', 'PAN')])

    def test_a_different_pan_on_the_same_code_is_not_shown(self):
        partner = {'code': 'V1', 'name': 'ACME', 'type': 'S', 'balance': Decimal('5')}
        with mock.patch.object(sap_relations, '_partner', return_value=partner), \
                mock.patch.object(sap_relations, '_pan', side_effect=lambda c, code: 'AAAAA1111A' if c == 'OIL' else 'BBBBB2222B'):
            rows = sap_relations.partner_balances('OIL', 'V1')
        self.assertEqual([r['company'] for r in rows], ['OIL'])

    def test_the_same_business_under_another_code_is_shown_with_its_code(self):
        own = {'code': 'VENDA001056', 'name': 'S.N. INDUSTRIES', 'type': 'S', 'balance': Decimal('-1703088')}
        theirs = {'code': 'VENDA001044', 'name': 'S.N. INDUSTRIES', 'type': 'S', 'balance': Decimal('0')}
        partners = {('BEVERAGES', 'VENDA001056'): own}
        with mock.patch.object(sap_relations, '_partner', side_effect=lambda c, code: partners.get((c, code))), \
                mock.patch.object(sap_relations, '_pan', return_value='AANPG8531F'), \
                mock.patch.object(sap_relations, '_found_by', return_value=[(theirs, 'GSTIN'), (theirs, 'name')]):
            rows = sap_relations.partner_balances('BEVERAGES', 'VENDA001056')
        self.assertEqual([(r['company'], r['card_code'], r['matched_by']) for r in rows],
                         [('BEVERAGES', 'VENDA001056', 'self'), ('OIL', 'VENDA001044', 'GSTIN')])

    def test_a_journal_entry_balances(self):
        header = [{'trans_id': 234693, 'number': 926302189, 'posting_date': None, 'document_date': None,
                   'memo': 'A/P Invoices - VENDA000942', 'trans_type': '18', 'base_ref': '626094172',
                   'ref1': '', 'ref2': ''}]
        lines = [{'line': 0, 'account': '2110005', 'account_name': 'SUNDRY CREDITOR', 'short_name': 'VENDA000942',
                  'partner_name': 'X', 'debit': Decimal('0'), 'credit': Decimal('1080253'), 'memo': ''},
                 {'line': 1, 'account': '2140001', 'account_name': 'GRNI', 'short_name': '2140001',
                  'partner_name': None, 'debit': Decimal('1080253'), 'credit': Decimal('0'), 'memo': ''}]
        with mock.patch.object(sap_relations, '_schema', return_value='S'), \
                mock.patch.object(sap_relations, '_run', side_effect=[header, lines]):
            je = sap_relations.journal_entry('OIL', 234693)
        self.assertEqual((je['header']['debit'], je['header']['credit']), ('1080253', '1080253'))
        self.assertEqual((je['lines'][0]['partner'], je['lines'][1]['partner']), ('VENDA000942', ''))

    def test_a_map_is_asked_of_a_po_grpo_or_bill_only(self):
        with self.assertRaisesRegex(ValueError, 'kind must be one of'):
            sap_relations.relationship_map('OIL', 'payment', 1)



class Ledgers(SimpleTestCase):
    """The partner's whole ledger, and one bill's."""

    def test_the_full_ledger_runs_from_the_opening_balance(self):
        opening = [{'b': Decimal('-1899095')}]
        lines = [{'trans_id': 1, 'line_id': 0, 'trans_type': '30', 'doc_entry': 1, 'doc_num': '826880024',
                  'ref1': '', 'ref2': '', 'posting_date': None, 'due_date': None, 'debit': Decimal('1724277'),
                  'credit': Decimal('0'), 'memo': ''},
                 {'trans_id': 2, 'line_id': 0, 'trans_type': '18', 'doc_entry': 9, 'doc_num': '626089124',
                  'ref1': '', 'ref2': '', 'posting_date': None, 'due_date': None, 'debit': Decimal('0'),
                  'credit': Decimal('21616'), 'memo': ''}]
        with mock.patch.object(sap_relations, '_schema', return_value='S'), \
                mock.patch.object(sap_relations, '_run', side_effect=[opening, lines]):
            led = sap_relations.full_ledger('BEVERAGES', 'VENDA000948', datetime(2026, 8, 1).date())
        self.assertEqual([r['balance'] for r in led['rows']], ['-174818', '-196434'])
        self.assertEqual((led['opening'], led['closing'], led['rows'][1]['doc_type']),
                         ('-1899095', '-196434', 'A/P Invoice'))

    def test_a_bill_is_settled_by_its_own_reconciled_amount(self):
        # Bill 624104114: Rs 4,05,000, of which a payment on account cleared Rs 3,05,000.
        bill = [{'e': 188, 'num': 624104114, 'date': None, 'due': None, 'ref': '7/2024-25',
                 'total': Decimal('405000'), 'paid': Decimal('305000'), 'card': 'VENDA000869', 'trans_id': 50}]
        recons = [{'recon': 7, 'recon_date': None, 'amount': Decimal('305000'), 'side': 'C'}]
        other = [{'recon': 7, 'trans_type': '46', 'doc_entry': 197, 'doc_num': '1024466623',
                  'posting_date': None, 'trans_id': 60}]
        with mock.patch.object(sap_relations, '_schema', return_value='S'), \
                mock.patch.object(sap_relations, '_run', side_effect=[bill, recons, other]):
            led = sap_relations.bill_ledger('OIL', 188)
        self.assertEqual((led['open'], led['sap_open']), ('100000', '100000'))
        self.assertEqual((led['rows'][1]['doc_type'], led['rows'][1]['doc_num']), ('Outgoing Payment', '1024466623'))


class FindingTheSameBusiness(SimpleTestCase):
    """`_found_by`: another code in the other company, by GSTIN, PAN, bank account or name."""

    def run_found(self, answers, pans=None, **kwargs):
        sqls = []

        def run(company, sql, params, what):
            sqls.append(what)
            return answers.get(what, [])

        with mock.patch.object(sap_relations, '_schema', return_value='S'), \
                mock.patch.object(sap_relations, '_run', side_effect=run), \
                mock.patch.object(sap_relations, '_pan', side_effect=lambda c, code: (pans or {}).get(code, '')):
            found = sap_relations._found_by('OIL', 'S', **kwargs)
        return [(r['code'], how) for r, how in found], sqls

    def test_looks_by_each_identifier_it_has(self):
        row = {'code': 'VENDA000956', 'name': 'ARNAV TRANSPORT SERVICE', 'type': 'S', 'balance': Decimal('-111404.82')}
        found, sqls = self.run_found({'partner by bank account': [row], 'partner by name': [row]},
                                     gstins=[], pan='', name='Arnav Transport Service.', accounts=['50200088470979'])
        self.assertEqual(found, [('VENDA000956', 'bank account'), ('VENDA000956', 'name')])
        self.assertEqual(sqls, ['partner by bank account', 'partner by name'])

    def test_a_same_name_with_another_pan_is_another_business(self):
        row = {'code': 'V9', 'name': 'SHARMA TRADERS', 'type': 'S', 'balance': Decimal('1')}
        found, _sqls = self.run_found({'partner by name': [row]}, pans={'V9': 'BBBBB2222B'},
                                      gstins=[], pan='AAAAA1111A', name='Sharma Traders', accounts=[])
        self.assertEqual(found, [])

    def test_a_name_is_compared_on_letters_and_digits(self):
        self.assertEqual(sap_relations._name_key('M/s. A.B. Traders (P) Ltd'), 'MSABTRADERSPLTD')


class HowLongItHasStood(SimpleTestCase):
    """`standing`: the days since the balance crossed onto its current side."""

    def stood(self, days):
        rows = [{'day': date(2026, m, d), 'net': Decimal(net)} for (m, d), net in days]
        with mock.patch.object(sap_relations, '_schema', return_value='S'), \
                mock.patch.object(sap_relations, '_run', return_value=rows):
            return sap_relations.standing('OIL', 'V1', today=date(2026, 10, 14))

    def test_counts_from_the_day_it_crossed_onto_its_side(self):
        # Dr 500 on 1 Aug, cleared and gone Cr on 24 Aug, more Cr since: Cr since 24 Aug.
        self.assertEqual(self.stood([((8, 1), '500'), ((8, 24), '-700'), ((9, 5), '-100')]),
                         {'since': '2026-08-24', 'days': 51})

    def test_a_balance_back_to_nil_starts_afresh(self):
        self.assertEqual(self.stood([((6, 1), '-100'), ((7, 1), '100'), ((10, 1), '-50')]),
                         {'since': '2026-10-01', 'days': 13})

    def test_nothing_outstanding_has_no_days(self):
        self.assertEqual(self.stood([((6, 1), '-100'), ((7, 1), '100.004')]), {'since': None, 'days': None})



class TheDebitsAgainstABillOrAPO(SimpleTestCase):
    """SAP's debit notes (A/P credit memos) made from a bill, or from a PO's bills (user, 2026-10-16)."""

    def run_with(self, rows, kind, found):
        with mock.patch.object(sap, '_schema', return_value='S'), \
                mock.patch.object(sap, '_run', return_value=found) as run:
            out = sap._with_debits('OIL', kind, rows)
        return out, run

    def test_a_bill_carries_its_debit_notes_and_their_total(self):
        out, run = self.run_with([{'doc_entry': 7}, {'doc_entry': 8}], 'bill', [
            {'doc': 7, 'note': 725025904, 'amount': Decimal('1000.004')},
            {'doc': 7, 'note': 725025903, 'amount': Decimal('21982.2436')},
        ])
        self.assertEqual(out[0]['debits'], {'amount': '22982.25', 'notes': [725025903, 725025904]})
        self.assertEqual(out[1]['debits']['notes'], [])
        self.assertIn('"RPC1"', run.call_args.args[1])

    def test_a_po_looks_through_its_bills_and_unreadable_says_nothing(self):
        _out, run = self.run_with([{'doc_entry': 5}], 'po', [])
        self.assertIn('"PDN1"', run.call_args.args[1])
        self.assertEqual(run.call_args.args[2], [5, 5])
        with mock.patch.object(sap, '_schema', return_value='S'), \
                mock.patch.object(sap, '_run', side_effect=sap.SapUnavailable('down')):
            rows = sap._with_debits('OIL', 'po', [{'doc_entry': 5}])
        self.assertIsNone(rows[0]['debits'])


class WhatSapTookOffTheRequestsDocuments(SimpleTestCase):
    """The desks' TDS and debit notes per bill or PO, from Payment on (user, 2026-10-16)."""

    def test_each_bill_says_its_tds_and_debit_notes(self):
        def run(_company, sql, _params, _what):
            if '"RPC1"' in sql:
                return [{'doc': 7, 'note': 725025903, 'amount': Decimal('8140')}]
            return [{'doc_entry': 7, 'tds': Decimal('23695.00')}, {'doc_entry': 8, 'tds': Decimal('0')}]
        with mock.patch.object(sap, '_schema', return_value='S'), mock.patch.object(sap, '_run', side_effect=run):
            out = sap.document_deductions('MART', 'bill', ['8', 7])
        self.assertEqual(out, [
            {'doc_entry': 7, 'tds': '23695.00', 'bills': None, 'debits': {'amount': '8140.00', 'notes': [725025903]}},
            {'doc_entry': 8, 'tds': '0', 'bills': None, 'debits': {'amount': '0.00', 'notes': []}},
        ])

    def test_a_po_says_the_tds_on_its_bills(self):
        def run(_company, sql, _params, _what):
            return [] if '"RPC1"' in sql else [{'po': 5, 'tds': Decimal('500'), 'bills': 2}]
        with mock.patch.object(sap, '_schema', return_value='S'), mock.patch.object(sap, '_run', side_effect=run):
            out = sap.document_deductions('OIL', 'po', [5])
        self.assertEqual(out[0]['tds'], '500')
        self.assertEqual(out[0]['bills'], 2)

    def test_nothing_asked_nothing_read(self):
        with mock.patch.object(sap, '_run') as run:
            self.assertEqual(sap.document_deductions('OIL', 'bill', []), [])
        run.assert_not_called()


@mock.patch.object(sap, '_schema', return_value='S')
class AJournalEntrysLinkedFile(SimpleTestCase):
    """A journal entry's file in its UDF, not ATC1 (AP-2026-0297's TA/DA, 2026-10-22)."""

    def test_the_linked_file_is_line_zero_by_its_name(self, _schema):
        def run(_company, sql, _params, _what):
            if '"ATC1"' in sql:
                return []
            self.assertIn('"U_ATTACH_LINK"', sql)
            return [{'doc_entry': 241383, 'doc_num': 1026900028, 'date': datetime(2026, 10, 1),
                     'link': '\\10.10.101.52\Attachments_Oil\JIVO_OIL\Attachments\TA DA-2708511.pdf'}]
        with mock.patch.object(sap, '_run', side_effect=run):
            line = sap.attachment_line('OIL', 'journal', 241383, 0)
        self.assertEqual(line['file_name'], 'TA DA-2708511.pdf')
        self.assertEqual(line['doc_num'], 1026900028)

    def test_beverages_reads_its_own_udf_and_a_name_without_a_path_is_not_a_file(self, _schema):
        with mock.patch.object(sap, '_run', return_value=[{'doc_entry': 1, 'doc_num': 2, 'date': None,
                                                          'link': 'abc123.png'}]) as run:
            self.assertEqual(sap._journal_link_lines('BEVERAGES', [1]), [])
        self.assertIn('"U_ATTACHEMENT"', run.call_args.args[1])

    def test_other_documents_do_not_look_for_a_link(self, _schema):
        with mock.patch.object(sap, '_run', return_value=[]) as run:
            sap._attachment_lines('OIL', 'bill', [5])
        self.assertEqual(run.call_count, 1)
