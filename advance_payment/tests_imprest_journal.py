"""Employee Imprest against journal entries: the monthly TA/DA and incentive credits on a staff account.

No database and no SAP: querysets and lookups are stood in for.
"""
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from advance_payment.models import DocumentKind
from advance_payment.services import requests as request_service
from advance_payment.services import voucher as voucher_service
from advance_payment.tests_refunds import item
from advance_payment.tests_requests import DAY, _clean, advance_of, line, payout, vendor_bill_request


def ta_da(**overrides):
    """Two lines of the August TA/DA journal crediting Raminder Singh's staff account."""
    data = vendor_bill_request(
        request_type='EMPLOYEE_IMPREST', payment_against='AGAINST_JOURNAL', partner_code='ORGV000015',
        partner_name='RAMINDER SINGH ASM GT PB IMPREST JWPL0197', purpose_code='TA_DA',
        documents=[item(236357, '26290', 30, 'CREDIT', line_no=12), item(234717, '15000', 30, 'CREDIT', line_no=2)])
    data.update(overrides)
    return data


class AnImprestAgainstJournalEntries(SimpleTestCase):
    def test_is_worth_the_credits_it_pays(self):
        cleaned = _clean(ta_da())
        self.assertEqual(cleaned.fields['amount'], Decimal('41290.00'))
        self.assertEqual([(d['kind'], d['sap_doc_entry'], d['sap_line']) for d in cleaned.documents],
                         [(DocumentKind.LEDGER, 236357, 12), (DocumentKind.LEDGER, 234717, 2)])

    def test_takes_only_journal_entries(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not a journal entry owed to the employee'):
            _clean(ta_da(documents=[item(210827, '100', 24, 'CREDIT', line_no=1)]))

    def test_takes_only_what_is_owed_to_the_employee(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not a journal entry owed to the employee'):
            _clean(ta_da(documents=[item(236357, '500', 30, 'DEBIT', line_no=3),
                                    item(234717, '15000', 30, 'CREDIT', line_no=2)]))

    def test_is_offered_to_employee_imprest_only(self):
        with self.assertRaises(request_service.RequestInvalid):
            _clean(ta_da(request_type='VENDOR'))

    def test_posts_each_line_through_the_journal_as_sap_pays_staff(self):
        docs = [SimpleNamespace(kind=DocumentKind.LEDGER, sap_doc_entry=236357, sap_line=12, sap_object=30,
                                sap_doc_num='236357', amount=Decimal('26290'))]
        body = voucher_service.build_payload(
            advance_of('EMPLOYEE_IMPREST', documents=docs, amount='26290', partner='ORGV000015'),
            payout(line(1, 'NEFT', '26290')), posting_date=DAY, series=2601, bpl_id=1, memo='OMS AP-2026-0110/1')
        self.assertEqual((body['DocType'], body['CardCode']), ('rSupplier', 'ORGV000015'))
        self.assertEqual(body['PaymentInvoices'], [
            {'LineNum': 0, 'DocEntry': 236357, 'DocLine': 12, 'InvoiceType': 'it_JournalEntry', 'SumApplied': 26290.0}])
        # Applied to the journal, not on account: no settlement date is asked.
        self.assertNotIn('U_Adv_Settl_Dt', body)
        self.assertIn('JE 236357', body['Remarks'])
