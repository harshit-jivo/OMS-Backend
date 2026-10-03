"""TDS deducted at the Payment stage on vendor payments, and how it reaches SAP.

No database and no SAP: lookups and the Service Layer are stood in for.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.models import DocumentKind
from advance_payment.services import payout as payout_service
from advance_payment.services import voucher as voucher_service
from advance_payment.tests_requests import DAY, advance_of, line, payout

CODES = [
    {'code': '194C', 'name': 'TDS ON CONTRACTOR HUF INDIVIDUALS @ 1 %', 'rate': '1', 'account': '2133003',
     'account_name': 'TDS 194C', 'assigned': True},
    {'code': 'C194', 'name': '194C TDS ON CONTRACTER- COMPANY(INVOICE)', 'rate': '2', 'account': '2133006',
     'account_name': 'TDS 194C company', 'assigned': False},
]


def vendor_advance(kind=DocumentKind.PO, amount='123456', request_type='VENDOR'):
    docs = [SimpleNamespace(kind=kind, sap_doc_entry=501, sap_doc_num='126226523', amount=Decimal(amount))]
    return SimpleNamespace(request_type=request_type, company='OIL', partner_code='VENDA000062',
                           amount=Decimal(amount), documents=mock.Mock(all=mock.Mock(return_value=docs)))


def clean(advance, code, taxed=None):
    with mock.patch('advance_payment.services.sap.tds_codes', return_value=CODES), \
            mock.patch('advance_payment.services.sap.bills_with_tds', return_value=taxed or {}):
        return payout_service._clean_tds(advance, {'code': code} if code is not None else None)


class TheTdsChoice(SimpleTestCase):
    def test_is_the_amount_at_the_codes_rate_rounded_to_the_rupee(self):
        fields = clean(vendor_advance(), 'C194')
        self.assertEqual(fields['tds_amount'], Decimal('2469'))  # 1,23,456 x 2% = 2,469.12
        self.assertEqual((fields['tds_rate'], fields['tds_account']), (Decimal('2'), '2133006'))

    def test_no_code_is_no_tds(self):
        self.assertEqual(clean(vendor_advance(), None)['tds_amount'], Decimal('0'))
        self.assertEqual(clean(vendor_advance(), '')['tds_code'], '')

    def test_only_on_a_vendor_payment(self):
        with self.assertRaisesRegex(payout_service.PayoutInvalid, 'vendor payments only'):
            clean(vendor_advance(request_type='EMPLOYEE_IMPREST'), '194C')

    def test_only_a_code_sap_has_at_an_offered_rate(self):
        with self.assertRaisesRegex(payout_service.PayoutInvalid, 'not an active TDS code'):
            clean(vendor_advance(), '94JZ')

    def test_never_on_a_bill_sap_already_deducted_tds_on(self):
        with self.assertRaisesRegex(payout_service.PayoutInvalid, 'already deducted in SAP on bill 126226523'):
            clean(vendor_advance(DocumentKind.BILL), '194C', taxed={501: {'doc_num': 126226523, 'tds': '2400'}})

    def test_the_methods_pay_the_amount_less_the_tds(self):
        advance = SimpleNamespace(amount=Decimal('100000'), request_type='VENDOR')
        with_tds = payout(line(1, 'NEFT', '98000'), tds_code='C194', tds_amount=Decimal('2000'))
        with mock.patch.object(payout_service.Payout.objects, 'filter') as found:
            found.return_value.first.return_value = with_tds
            self.assertEqual(payout_service.problems(advance), [])
            with_tds.lines.all.return_value = [line(1, 'NEFT', '100000')]
            self.assertIn('The payment methods add up to 100000, but the request pays 98000 after TDS of 2000.',
                          payout_service.problems(advance))


def tds_payout(amount='98000', tds='2000'):
    return payout(line(1, 'NEFT', amount), tds_code='C194', tds_rate=Decimal('2'), tds_account='2133006',
                  tds_amount=Decimal(tds))


class InSap(SimpleTestCase):
    def test_the_journal_debits_the_vendor_first_and_credits_the_sections_account(self):
        body = voucher_service.build_tds_journal(
            advance_of(documents=[]), tds_payout(), posting_date=DAY, bpl_id=1, memo='OMS AP-2026-0007/1',
            control_account='2110004')
        self.assertEqual(body['Memo'], 'OMS AP-2026-0007/1 TDS')
        vendor, tds = body['JournalEntryLines']
        self.assertEqual((vendor['ShortName'], vendor['AccountCode'], vendor['Debit'], vendor['BPLID']),
                         ('VENDA001465', '2110004', 2000.0, 1))
        self.assertEqual((tds['AccountCode'], tds['Credit']), ('2133006', 2000.0))

    def test_a_bill_payment_applies_the_journal_so_the_bill_closes_in_full(self):
        bill = SimpleNamespace(kind=DocumentKind.BILL, sap_doc_entry=501, sap_doc_num='126226523',
                               amount=Decimal('100000'))
        body = voucher_service.build_payload(
            advance_of(documents=[bill], amount='100000'), tds_payout(), posting_date=DAY, series=2601,
            bpl_id=1, memo='OMS AP-2026-0007/1', tds_trans_id=240001)
        self.assertEqual(body['PaymentInvoices'][-1], {'LineNum': 1, 'DocEntry': 240001, 'DocLine': 0,
                                                       'InvoiceType': 'it_JournalEntry', 'SumApplied': 2000.0})
        self.assertEqual(body['TransferSum'], 98000.0)  # 1,00,000 bill less 2,000 TDS
        self.assertIn('TDS C194 2% 2000', body['Remarks'])

    def test_a_po_advance_stays_on_account_beside_the_journal(self):
        po = SimpleNamespace(kind=DocumentKind.PO, sap_doc_entry=14008, sap_doc_num='14008', amount=Decimal('100000'))
        body = voucher_service.build_payload(
            advance_of(documents=[po], amount='100000'), tds_payout(), posting_date=DAY, series=2601, bpl_id=1,
            memo='OMS AP-2026-0007/1', tds_trans_id=240001)
        self.assertNotIn('PaymentInvoices', body)
        self.assertEqual(body['TransferSum'], 98000.0)


class Posting(SimpleTestCase):
    def _post(self, sl_side_effect, *, found=None):
        advance = mock.Mock(pk=7, request_no='AP-2026-0007', company='OIL', partner_code='VENDA000062')
        advance.vouchers.filter.return_value.count.return_value = 0
        advance.vouchers.aggregate.return_value = {'n': None}
        the_payout = SimpleNamespace(tds_amount=Decimal('2000'), tds_code='C194', tds_rate=Decimal('2'),
                                     tds_account='2133006', lines=mock.Mock(all=mock.Mock(return_value=[])))
        with mock.patch.object(voucher_service.AdvanceRequest.objects, 'get', return_value=advance), \
                mock.patch.object(voucher_service.Payout.objects, 'filter') as payouts, \
                mock.patch.object(voucher_service, '_branch', return_value=(1, '')), \
                mock.patch.object(voucher_service, 'build_payload', return_value={'x': 1}), \
                mock.patch.object(voucher_service, 'build_tds_journal', return_value={'j': 1}), \
                mock.patch.object(voucher_service, '_company_db', return_value='TEST_OIL'), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_series', return_value=2601), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_by_memo', return_value=None), \
                mock.patch.object(voucher_service.sap_service, 'journal_by_memo', return_value=found), \
                mock.patch.object(voucher_service.sap_service, 'vendor_control_account', return_value='2110004'), \
                mock.patch('advance_payment.services.reservations.live_check', return_value=[]), \
                mock.patch('payments.sap_client.request', side_effect=sl_side_effect) as sl, \
                mock.patch.object(voucher_service.SapVoucher.objects, 'create') as create:
            payouts.return_value.first.return_value = the_payout
            outcome = voucher_service.post(advance, user=SimpleNamespace(pk=1))
        return outcome, sl, create

    def test_books_the_journal_then_pays(self):
        answers = iter([(201, {'JdtNum': 240001}), (201, {'DocEntry': 29999, 'DocNum': 926467999})])
        outcome, sl, create = self._post(lambda *a, **k: next(answers))
        self.assertTrue(outcome.ok)
        self.assertEqual([c.args[:2] for c in sl.call_args_list],
                         [('POST', '/JournalEntries'), ('POST', '/VendorPayments')])
        self.assertEqual(create.call_args.kwargs['tds_trans_id'], 240001)

    def test_a_refused_payment_cancels_its_journal(self):
        from payments.sap_client import SapError

        def answer(method, path, **kwargs):
            if path == '/JournalEntries':
                return 201, {'JdtNum': 240001}
            if path == '/VendorPayments':
                raise SapError('Balance due exceeded', status_code=400)
            return 204, {}

        outcome, sl, _create = self._post(answer)
        self.assertFalse(outcome.ok)
        self.assertIn('Its TDS journal 240001 was cancelled.', outcome.message)
        self.assertEqual(sl.call_args_list[-1].args[:2], ('POST', '/JournalEntries(240001)/Cancel'))

    def test_a_journal_from_an_earlier_attempt_is_reused_only_if_it_still_fits(self):
        outcome, sl, _create = self._post(lambda *a, **k: (201, {'DocEntry': 1, 'DocNum': 2}),
                                          found={'trans_id': 240001, 'debit': Decimal('2000')})
        self.assertTrue(outcome.ok)
        self.assertEqual([c.args[1] for c in sl.call_args_list], ['/VendorPayments'])  # not booked twice

        outcome, sl, _create = self._post(lambda *a, **k: (201, {}),
                                          found={'trans_id': 240001, 'debit': Decimal('1500')})
        self.assertFalse(outcome.ok)
        self.assertIn('Reverse it in SAP', outcome.message)
        sl.assert_not_called()

    def test_no_answer_on_the_journal_does_not_pay(self):
        from payments.sap_client import SapError

        outcome, sl, _create = self._post(SapError('timed out', status_code=None))
        self.assertFalse(outcome.ok)
        self.assertIn('finds it by its memo and will not book it twice', outcome.message)
        self.assertEqual(len(sl.call_args_list), 1)
