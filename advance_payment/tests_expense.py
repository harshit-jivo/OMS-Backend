"""Expense requests: paid straight to expense G/L accounts.

No database and no SAP: rows are stand-ins, managers and SAP calls are patched.
What is pinned:

* the form asks only the company, budget head, electricity, an optional
  vendor, remarks (optional) and the lines — each an amount and a G/L, or,
  not knowing the G/L, remarks saying what it is for. Not asked: Payment
  Against (direct or indirect follows from the G/Ls; one kind per request),
  who is paid (the vendor, else whoever raises it), ownership, the payment
  date (the day it is raised) and the month (that day's), the sub budget;
* the Payment desk may correct all but the company, budget head and the
  amounts: vendor / payee, sub budget, month, electricity, each line's G/L,
  remarks and month, and records each line's GST (the taxable amount backed
  out of the amount) and deducts TDS on the taxable amount — one code for the
  request, any line its own or none — logged as "Edited at Payment";
* the Payment stage cannot approve while a line has no G/L or month, or the
  request no sub budget;
* Audit's approval posts an Expense (its route has no Final);
* SAP: a TDS journal (Dr each line's G/L with its four dimensions, Cr the
  TDS account), and an account payment paying each line's amount less its
  TDS, with Variety / Month / Budget / Sub Budget in ProfitCenter..4.
"""
import contextlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.models import RequestType, StageRole
from advance_payment.services import flow as flow_service
from advance_payment.services import payout as payout_service
from advance_payment.services import requests as request_service
from advance_payment.services import voucher as voucher_service
from advance_payment.tests_requests import BUDGETS, DAY, line, payout

ACCOUNTS = [
    {'code': '5680011', 'name': 'ELECTRICITY EXPENSES', 'group': 'POWER & FUEL', 'kind': 'INDIRECT'},
    {'code': '5670001', 'name': 'RENT', 'group': 'ADMIN', 'kind': 'INDIRECT'},
    {'code': '5100008', 'name': 'CASUAL LABOUR', 'group': 'DIRECT EXPENSE', 'kind': 'DIRECT'},
]
MONTHS = ['11-2026', '10-2026', '09-2026']
TDS_CODES = [{'code': 'C194-2', 'name': '194C Contractor 2%', 'rate': '2', 'account': '2133011',
              'account_name': 'TDS PAYABLE', 'assigned': False},
             {'code': 'J194-10', 'name': '194J Professional 10%', 'rate': '10', 'account': '2133012',
              'account_name': 'TDS PAYABLE 194J', 'assigned': False}]
RAISER = SimpleNamespace(name='Kamal Kumar', get_username=lambda: 'kamal')


def expense_request(**overrides):
    """What the form sends: who is paid (Pay To, 2026-10-20), but no ownership,
    payment date, month or sub budget."""
    data = {
        'company': 'OIL', 'request_type': 'EXPENSE', 'budget_code': 'Factory', 'is_electricity': True,
        'expense_payee': 'VENDOR', 'partner_code': 'VENDA000101',
        # The vendor's bill it pays (2026-10-20): category, number, date.
        'expense_category': 'SERVICE', 'expense_bill_number': 'INV-2026-118', 'expense_bill_date': '2026-10-05',
        'remarks': '',
        'expense_lines': [
            {'amount': '11800', 'gl_account': '5680011'},
            {'amount': '3000.50', 'gl_account': '', 'remarks': 'Penalty, G/L to confirm'},
        ],
    }
    data.update(overrides)
    return data


VENDOR = {'card_code': 'VENDA000101', 'card_name': 'ABC Technologies', 'party_type': 'vendor'}


@contextlib.contextmanager
def sap_lookups(partner=VENDOR):
    with mock.patch('advance_payment.services.sap.budgets', return_value=BUDGETS), \
            mock.patch('advance_payment.services.sap.expense_accounts', return_value=ACCOUNTS), \
            mock.patch('advance_payment.services.sap.expense_months', return_value=MONTHS), \
            mock.patch('advance_payment.services.sap.tds_codes', return_value=TDS_CODES), \
            mock.patch('advance_payment.services.sap.partner', return_value=partner) as found, \
            mock.patch('advance_payment.services.clock.today', return_value=date(2026, 10, 8)):
        yield found


def _clean(data, partner=VENDOR, desk=False, user=RAISER):
    with sap_lookups(partner):
        return request_service.clean(data, desk=desk, user=user)


class TheExpenseForm(SimpleTestCase):
    def test_what_is_not_asked_is_filled_in(self):
        cleaned = _clean(expense_request())
        f = cleaned.fields
        self.assertEqual(f['amount'], Decimal('14800.50'))
        self.assertEqual((f['request_type'], f['payment_against']), ('EXPENSE', 'INDIRECT_EXPENSE'))
        # Paid to the vendor named; dated the day it is raised, in that month.
        self.assertEqual((f['partner_code'], f['partner_name']), ('VENDA000101', 'ABC Technologies'))
        self.assertEqual((f['payment_date'], f['effect_month']), (date(2026, 10, 8), '10-2026'))
        self.assertEqual((f['owner_label'], f['remarks'], f['sub_budget_code']), ('', '', ''))
        self.assertEqual((f['budget_code'], f['is_electricity'], f['purpose_code']), ('Factory', True, ''))
        self.assertEqual([(ln['amount'], ln['taxable_amount'], ln['gst_code'], ln['gl_account'], ln['gl_name'],
                           ln['effect_month']) for ln in cleaned.expense_lines],
                         [(Decimal('11800'), Decimal('11800.00'), '', '5680011', 'ELECTRICITY EXPENSES', ''),
                          (Decimal('3000.50'), Decimal('3000.50'), '', '', '', '')])
        self.assertEqual(cleaned.documents, [])

    def test_a_vendor_names_who_is_paid(self):
        f = _clean(expense_request(partner_code='VENDA000101')).fields
        self.assertEqual((f['partner_code'], f['partner_name']), ('VENDA000101', 'ABC Technologies'))
        with self.assertRaisesRegex(request_service.RequestInvalid, "CUSTA1 is not a vendor in OIL's SAP"):
            _clean(expense_request(partner_code='CUSTA1'), partner={**VENDOR, 'party_type': 'customer'})

    def test_direct_or_indirect_follows_from_the_gl_accounts_and_one_request_is_one_kind(self):
        direct = _clean(expense_request(expense_lines=[{'amount': '10', 'gl_account': '5100008'},
                                                       {'amount': '5', 'remarks': 'not sure'}]))
        self.assertEqual(direct.fields['payment_against'], 'DIRECT_EXPENSE')
        unknown = _clean(expense_request(expense_lines=[{'amount': '5', 'remarks': 'not sure'}]))
        self.assertEqual(unknown.fields['payment_against'], 'INDIRECT_EXPENSE')
        with self.assertRaisesRegex(request_service.RequestInvalid, 'lines 1 are direct, lines 2 indirect'):
            _clean(expense_request(expense_lines=[{'amount': '10', 'gl_account': '5100008'},
                                                  {'amount': '10', 'gl_account': '5670001'}]))

    def test_each_line_needs_an_amount_and_a_gl_or_remarks(self):
        data = expense_request(expense_lines=[{'amount': '10', 'gl_account': ''},
                                              {'amount': '10', 'gl_account': '1104107', 'remarks': 'x'},
                                              {'amount': '0', 'gl_account': '5670001'},
                                              {'amount': '5', 'gl_account': '5670001', 'effect_month': '13-2026'}])
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(data)
        problems = caught.exception.problems
        self.assertIn('Line 1: choose the G/L account, or say in the remarks what it is for.', problems)
        self.assertIn('Line 2: 1104107 is not an expense account in SAP.', problems)
        self.assertIn('Line 3: enter an amount above zero.', problems)
        self.assertIn('Line 4: month "13-2026" is not one of SAP\'s Effective Months.', problems)

    def test_what_an_expense_must_have(self):
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(expense_request(budget_code='', expense_lines=[], partner_code=''), user=None)
        problems = ' '.join(caught.exception.problems)
        for expected in ('Choose the Department', 'Add at least one expense line', 'Choose the vendor'):
            self.assertIn(expected, problems)
        for not_asked in ('Payment Purpose', 'Sub Budget', 'Month', 'Ownership', 'Payment Date', 'Remarks'):
            self.assertNotIn(not_asked, problems)

    def test_the_requester_sets_no_gst_and_no_tds(self):
        data = expense_request(expense_tds_code='C194-2', expense_lines=[
            {'amount': '118', 'gl_account': '5670001', 'gst_code': 'CGST_SGST_18', 'tds_override': 'J194-10'}])
        cleaned = _clean(data)
        ln = cleaned.expense_lines[0]
        self.assertEqual((cleaned.fields['expense_tds_code'], ln['gst_code'], ln['tds_override'], ln['tds_amount']),
                         ('', '', '', Decimal('0')))

    def test_sap_ties_some_budget_heads_to_their_sub_budgets(self):
        self.assertEqual(_clean(expense_request(budget_code='BackOff', sub_budget_code='IT')).fields['sub_budget_code'],
                         'IT')
        with mock.patch.dict('advance_payment.services.sap.SUB_BUDGET_RULES', {'OIL': {'BackOff': {'Accounts'}}}):
            with self.assertRaisesRegex(request_service.RequestInvalid, 'Under BackOff, SAP allows the Sub Budget '
                                                                        'Accounts — not "IT"'):
                _clean(expense_request(budget_code='BackOff', sub_budget_code='IT'))

    def test_an_expense_takes_lines_not_documents(self):
        data = expense_request(documents=[{'kind': 'BILL', 'sap_doc_entry': 1, 'amount': '1'}])
        with self.assertRaisesRegex(request_service.RequestInvalid, 'pays its lines, not documents'):
            _clean(data)


class TheDesksGstAndTds(SimpleTestCase):
    def test_gst_backs_the_taxable_amount_out_and_tds_is_on_it(self):
        data = expense_request(effect_month='10-2026', expense_tds_code='C194-2', expense_lines=[
            {'amount': '11800', 'gst_code': 'CGST_SGST_18', 'gl_account': '5680011'},
            {'amount': '2500', 'gl_account': '5670001', 'tds_override': 'J194-10'},
            {'amount': '700', 'gl_account': '5670001', 'tds_override': 'NONE'},
            {'amount': '1049.99', 'gst_code': 'IGST_5', 'gl_account': '5670001'},
        ])
        cleaned = _clean(data, desk=True)
        self.assertEqual((cleaned.fields['expense_tds_code'], cleaned.fields['effect_month']), ('C194-2', '10-2026'))
        self.assertEqual([(ln['amount'], ln['taxable_amount'], ln['gst_amount'], ln['tds_code'], ln['tds_amount'])
                          for ln in cleaned.expense_lines], [
            (Decimal('11800'), Decimal('10000.00'), Decimal('1800.00'), 'C194-2', Decimal('200')),  # 2% of 10000
            (Decimal('2500'), Decimal('2500.00'), Decimal('0.00'), 'J194-10', Decimal('250')),
            (Decimal('700'), Decimal('700.00'), Decimal('0.00'), '', Decimal('0')),
            (Decimal('1049.99'), Decimal('999.99'), Decimal('50.00'), 'C194-2', Decimal('20')),  # 19.9998
        ])

    def test_a_gst_choice_and_a_tds_code_must_be_known(self):
        data = expense_request(expense_tds_code='X1', expense_lines=[
            {'amount': '10', 'gl_account': '5670001', 'tds_override': 'X2', 'gst_code': 'GST_12'}])
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(data, desk=True)
        self.assertIn('TDS: X1 is not an active TDS code at 1, 2, 5 or 10% in SAP.', caught.exception.problems)
        self.assertIn('Line 1: X2 is not an active TDS code at 1, 2, 5 or 10% in SAP.', caught.exception.problems)
        self.assertIn('Line 1: "GST_12" is not a GST choice.', caught.exception.problems)

    def test_the_payment_methods_pay_the_amount_less_tds(self):
        advance = SimpleNamespace(request_type=RequestType.EXPENSE, amount=Decimal('11800'))
        advance.expense_lines = mock.Mock()
        advance.expense_lines.all.return_value = [SimpleNamespace(tds_amount=Decimal('200'))]
        self.assertEqual(payout_service.net_amount(advance, payout()), Decimal('11600'))


def advance(lines=(), **extra):
    values = dict(pk=9, request_no='AP-2026-0021', request_type=RequestType.EXPENSE, company='OIL',
                  partner_code='', partner_name='PSPCL (electricity board)', amount=Decimal('14800.50'),
                  currency='INR', remarks='Factory power bill', budget_code='Factory', sub_budget_code='Accounts',
                  purpose_label='', effect_month='10-2026')
    values.update(extra)
    row = SimpleNamespace(**values)
    row.expense_lines = mock.Mock()
    row.expense_lines.all.return_value = list(lines)
    row.documents = mock.Mock()
    row.documents.all.return_value = []
    return row


def expense_line(no, amount, gl, month='', remarks='', tds='0', tds_code='', tds_account=''):
    return SimpleNamespace(pk=100 + no, line_no=no, amount=Decimal(amount), gl_account=gl, gl_name='',
                           effect_month=month, remarks=remarks, tds_amount=Decimal(tds), tds_code=tds_code,
                           tds_account=tds_account)


class TheSapPayment(SimpleTestCase):
    def test_each_line_pays_its_invoice_value_less_tds_with_all_four_dimensions(self):
        lines = [expense_line(1, '11800', '5680011', remarks='Unit 1', tds='200', tds_code='C194-2',
                              tds_account='2133011'),
                 expense_line(2, '3000.50', '5670001', month='09-2026')]
        body = voucher_service.build_payload(advance(lines), payout(line(1, 'NEFT', '14600.50')),
                                             posting_date=DAY, series=2601, bpl_id=1, memo='OMS AP-2026-0021/1')
        self.assertEqual(body['DocType'], 'rAccount')
        self.assertNotIn('CardCode', body)
        self.assertEqual(body['PaymentAccounts'], [
            {'AccountCode': '5680011', 'SumPaid': 11600.0, 'Decription': 'Unit 1', 'ProfitCenter': 'CANOLA',
             'ProfitCenter2': '10-2026', 'ProfitCenter3': 'Factory', 'ProfitCenter4': 'Accounts'},
            {'AccountCode': '5670001', 'SumPaid': 3000.5, 'Decription': 'PSPCL (electricity board)',
             'ProfitCenter': 'CANOLA', 'ProfitCenter2': '09-2026', 'ProfitCenter3': 'Factory',
             'ProfitCenter4': 'Accounts'},
        ])
        self.assertEqual(body['JournalRemarks'], 'OMS AP-2026-0021/1')
        self.assertIn('TDS 200', body['Remarks'])

    def test_the_tds_journal_debits_each_lines_gl_and_credits_each_tds_account(self):
        lines = [expense_line(1, '11800', '5680011', tds='200', tds_code='C194-2', tds_account='2133011'),
                 expense_line(2, '2500', '5670001', month='09-2026', tds='250', tds_code='J194-10',
                              tds_account='2133012'),
                 expense_line(3, '500', '5670001', tds='10', tds_code='C194-2', tds_account='2133011'),
                 expense_line(4, '700', '5670001')]
        body = voucher_service.build_expense_tds_journal(advance(lines), posting_date=DAY, bpl_id=1,
                                                         memo='OMS AP-2026-0021/1')
        self.assertEqual(body['Memo'], 'OMS AP-2026-0021/1 TDS')
        debits = [(ln['AccountCode'], ln['Debit'], ln['CostingCode'], ln['CostingCode2'], ln['CostingCode3'],
                   ln['CostingCode4']) for ln in body['JournalEntryLines'] if ln['Debit']]
        self.assertEqual(debits, [('5680011', 200.0, 'CANOLA', '10-2026', 'Factory', 'Accounts'),
                                  ('5670001', 250.0, 'CANOLA', '09-2026', 'Factory', 'Accounts'),
                                  ('5670001', 10.0, 'CANOLA', '10-2026', 'Factory', 'Accounts')])
        credits = sorted((ln['AccountCode'], ln['Credit']) for ln in body['JournalEntryLines'] if ln['Credit'])
        self.assertEqual(credits, [('2133011', 210.0), ('2133012', 250.0)])
        self.assertTrue(all(ln['BPLID'] == 1 for ln in body['JournalEntryLines']))

    def test_variety_is_the_companys(self):
        body = voucher_service.build_payload(advance([expense_line(1, '10', '5670001')], company='BEVERAGES'),
                                             payout(line(1, 'NEFT', '10')), posting_date=DAY, series=1,
                                             bpl_id=1, memo='m')
        self.assertEqual(body['PaymentAccounts'][0]['ProfitCenter'], 'WATER')


class ThePaymentStage(SimpleTestCase):
    def test_cannot_approve_while_a_line_has_no_gl_or_no_month_or_without_a_sub_budget(self):
        self.assertEqual(flow_service.expense_problems(advance([expense_line(1, '10', '5670001')],
                                                               sub_budget_code='')), ['Choose the Sub Budget.'])
        lines = [expense_line(1, '10', '5670001'), expense_line(2, '5', '')]
        self.assertEqual(flow_service.expense_problems(advance(lines)), ['Line 2: choose its expense G/L account.'])
        self.assertEqual(flow_service.expense_problems(advance([expense_line(1, '10', '5670001')])), [])
        self.assertEqual(flow_service.expense_problems(advance([expense_line(1, '10', '5670001')], effect_month='')),
                         ['Choose the Month.'])
        self.assertEqual(flow_service.expense_problems(advance([expense_line(1, '10', '5670001', month='09-2026')],
                                                               effect_month='')), [])
        self.assertEqual(flow_service.expense_problems(advance(request_type='VENDOR')), [])

    @contextlib.contextmanager
    def acting(self, the_advance, role='PAYMENT'):
        flow = SimpleNamespace(current_role=role, request=the_advance, version=3, cycle=1, save=mock.Mock())
        with mock.patch('django.db.transaction.Atomic.__enter__'), \
                mock.patch('django.db.transaction.Atomic.__exit__', return_value=False), \
                mock.patch.object(flow_service, '_acting', return_value=flow), \
                mock.patch.object(flow_service, 'log') as log:
            yield log

    def test_payment_corrects_all_but_company_payee_and_amounts_and_it_is_logged(self):
        the_advance = advance([expense_line(1, '5', '')])
        changes = {'sub_budget': {'old': None, 'new': 'IT — IT'}}
        with self.acting(the_advance) as log, sap_lookups(), \
                mock.patch.object(request_service, 'form_of', return_value=expense_request(partner_name='Kamal Kumar', partner_code='', expense_payee='')), \
                mock.patch.object(request_service, 'edit_expense', return_value=changes) as write:
            flow_service.edit_expense(the_advance, {
                'budget_code': 'BackOff', 'company': 'MART', 'sub_budget_code': 'IT', 'partner_code': 'VENDA000101',
                'request_type': 'VENDOR', 'effect_month': '09-2026', 'expense_tds_code': 'C194-2',
                'expense_lines': [{'amount': '11800', 'gl_account': '5680011', 'gst_code': 'CGST_SGST_18'},
                                  {'amount': '3000.50', 'gl_account': '5670001', 'remarks': 'Penalty'}],
            }, user=SimpleNamespace(pk=1))
        cleaned = write.call_args.args[1]
        # Laid over the request: only what the desk may change.
        f = cleaned.fields
        # The budget head is the desk's to change since 2026-10-20; the company and type are not.
        self.assertEqual((f['company'], f['budget_code'], f['request_type']), ('OIL', 'BackOff', 'EXPENSE'))
        self.assertEqual((f['sub_budget_code'], f['effect_month']), ('IT', '09-2026'))
        self.assertEqual((f['partner_code'], f['partner_name']), ('', 'Kamal Kumar'))  # who is paid: not the desk's
        self.assertEqual((f['amount'], f['expense_tds_code']), (Decimal('14800.50'), 'C194-2'))
        self.assertEqual([(ln['gl_account'], ln['taxable_amount'], ln['tds_amount']) for ln in cleaned.expense_lines],
                         [('5680011', Decimal('10000.00'), Decimal('200')), ('5670001', Decimal('3000.50'), Decimal('60'))])
        self.assertEqual(log.call_args.args[1], 'PAYMENT_EDITED')
        self.assertEqual(log.call_args.kwargs['data'], changes)

    def test_a_correction_is_checked_as_the_form_is_and_keeps_the_amounts(self):
        the_advance = advance([expense_line(1, '5', '')])
        with self.acting(the_advance), sap_lookups(), \
                mock.patch.object(request_service, 'form_of', return_value=expense_request(partner_name='Kamal Kumar', partner_code='', expense_payee='')):
            with self.assertRaisesRegex(flow_service.FlowError, 'not an expense account'):
                flow_service.edit_expense(the_advance, {'expense_lines': [
                    {'amount': '11800', 'gl_account': '1104107'}, {'amount': '3000.50', 'gl_account': '5670001'}]},
                    user=SimpleNamespace(pk=1))
            for lines in ([{'amount': '11000', 'gl_account': '5680011'}, {'amount': '3000.50', 'gl_account': '5670001'}],
                          [{'amount': '11800', 'gl_account': '5680011'}]):
                with self.assertRaisesRegex(flow_service.FlowError, 'cannot change the lines or their amounts'):
                    flow_service.edit_expense(the_advance, {'expense_lines': lines}, user=SimpleNamespace(pk=1))

    def test_only_at_payment_and_only_an_expense(self):
        with self.acting(advance(), role='AUDIT'):
            with self.assertRaisesRegex(flow_service.FlowError, 'corrected at the Payment stage'):
                flow_service.edit_expense(advance(), {}, user=SimpleNamespace(pk=1))
        with self.acting(advance(request_type='VENDOR')):
            with self.assertRaisesRegex(flow_service.FlowError, 'Only an Expense'):
                flow_service.edit_expense(advance(request_type='VENDOR'), {}, user=SimpleNamespace(pk=1))

    def test_the_edit_writes_the_fields_and_lines_and_says_what_changed(self):
        row = SimpleNamespace(budget_code='Factory', save=mock.Mock())
        cleaned = request_service.CleanRequest(
            fields={'payment_against': 'INDIRECT_EXPENSE', 'partner_code': 'VENDA000101', 'partner_name': 'ABC',
                    'amount': Decimal('20'), 'budget_code': 'BackOff', 'budget_name': 'Back Office',
                    'sub_budget_code': 'IT', 'sub_budget_name': 'IT', 'effect_month': '10-2026',
                    'is_electricity': False, 'expense_tds_code': 'C194-2', 'company': 'MART',
                    'expense_category': 'SERVICE', 'expense_bill_number': 'INV-9', 'expense_bill_date': None,
                    'expense_bpl_id': 2, 'expense_bpl_name': 'FACTORY', 'expense_due_date': None,
                    'expense_grpo_entry': None, 'expense_grpo_num': None, 'expense_grpo_fields': None},
            expense_lines=[{'line_no': 1, 'amount': Decimal('20')}])
        with mock.patch.object(request_service, '_snapshot', side_effect=[{'budget': 'Factory'}, {'budget': 'BackOff'}]), \
                mock.patch.object(request_service, '_expense_lines',
                                  side_effect=[{'Line 1': 'no G/L · 5.00'}, {'Line 1': '5670001 · 20.00'}]), \
                mock.patch.object(request_service, '_write_expense_lines') as write:
            changes = request_service.edit_expense(row, cleaned)
        # The budget head and the bill are the desk's too (2026-10-20); not the company or the amount.
        self.assertEqual((row.budget_code, row.sub_budget_code, row.expense_tds_code), ('BackOff', 'IT', 'C194-2'))
        self.assertEqual((row.expense_category, row.expense_bill_number), ('SERVICE', 'INV-9'))
        self.assertFalse(hasattr(row, 'amount') or hasattr(row, 'company') or hasattr(row, 'partner_code'))
        write.assert_called_once_with(row, cleaned.expense_lines)
        self.assertEqual(changes, {'budget': {'old': 'Factory', 'new': 'BackOff'},
                                   'expense_lines': {'Line 1': {'old': 'no G/L · 5.00', 'new': '5670001 · 20.00'}}})


class AuditPosts(SimpleTestCase):
    def test_audit_posts_an_expense_and_completes_it(self):
        stages = [(SimpleNamespace(id=1, name='Budget Owner Approval', sequence=1), StageRole.APPROVAL),
                  (SimpleNamespace(id=2, name='Payment Approval', sequence=2), StageRole.PAYMENT),
                  (SimpleNamespace(id=3, name='Audit Approval', sequence=3), StageRole.AUDIT)]
        the_advance = advance([expense_line(1, '10', '5670001')], status='IN_APPROVAL', save=mock.Mock(),
                              department_head_id=None)
        flow = SimpleNamespace(current_role=StageRole.AUDIT, current_stage=stages[2][0], current_stage_id=3,
                               request=the_advance, version=3, cycle=1, status='PENDING', save=mock.Mock())
        with mock.patch('django.db.transaction.Atomic.__enter__'), \
                mock.patch('django.db.transaction.Atomic.__exit__', return_value=False), \
                mock.patch.object(flow_service, '_acting', return_value=flow), \
                mock.patch.object(flow_service, '_stages', return_value=stages), \
                mock.patch.object(flow_service, '_post_voucher', return_value='') as post, \
                mock.patch.object(flow_service, 'log') as log:
            _advance, error = flow_service.approve(the_advance, user=SimpleNamespace(pk=1))
        self.assertEqual(error, '')
        post.assert_called_once()
        self.assertEqual(the_advance.status, 'COMPLETED')
        self.assertIn('COMPLETED', [c.args[1] for c in log.call_args_list])



class WhoAnExpenseIsPaidTo(SimpleTestCase):
    """Pay To (user, 2026-10-20): a Vendor or an Imprest, and the partner it names — both required."""

    def test_pay_to_and_the_partner_are_required(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, r'Choose who is paid \(Pay To\)'):
            _clean(expense_request(expense_payee=''))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Choose the vendor'):
            _clean(expense_request(partner_code=''))
        with self.assertRaisesRegex(request_service.RequestInvalid, "Choose the employee's imprest account"):
            _clean(expense_request(expense_payee='IMPREST', partner_code=''))

    def test_the_partner_must_be_of_its_pay_to(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, "VENDA000101 is not an employee's imprest account"):
            _clean(expense_request(expense_payee='IMPREST'))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'ORGV000044 is not a vendor'):
            _clean(expense_request(partner_code='ORGV000044'))

    def test_an_imprest_account_is_paid(self):
        imprest = {'card_code': 'ORGV000044', 'card_name': 'ZIYAUL HAQUE IMPREST JWPL0042', 'party_type': 'vendor'}
        f = _clean(expense_request(expense_payee='IMPREST', partner_code='ORGV000044'), partner=imprest).fields
        self.assertEqual((f['partner_code'], f['partner_name']), ('ORGV000044', 'ZIYAUL HAQUE IMPREST JWPL0042'))
        self.assertEqual(request_service.expense_payee_of('ORGV000044'), 'IMPREST')
        self.assertEqual(request_service.expense_payee_of('VENDA000101'), 'VENDOR')
        self.assertEqual(request_service.expense_payee_of(''), '')



class TheVendorsBill(SimpleTestCase):
    """An Expense paid to a vendor names its bill: category, number, date (2026-10-20)."""

    def test_they_are_kept(self):
        f = _clean(expense_request()).fields
        self.assertEqual((f['expense_category'], f['expense_bill_number'], str(f['expense_bill_date'])),
                         ('SERVICE', 'INV-2026-118', '2026-10-05'))
        self.assertTrue(request_service.needs_due_date_proof(f))  # the bill must be attached

    def test_number_and_doc_date_are_required_the_category_is_not(self):
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(expense_request(expense_category='', expense_bill_number='', expense_bill_date=''))
        self.assertEqual(caught.exception.problems, ["Enter the vendor's Bill Number.", "Enter the bill's Doc Date."])
        # The category is the Payment desk's: the requester may leave it.
        self.assertEqual(_clean(expense_request(expense_category='')).fields['expense_category'], '')

    def test_a_future_doc_date_or_an_unknown_category_is_refused(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'The Doc Date cannot be in the future'):
            _clean(expense_request(expense_bill_date='2099-01-01'))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Item or Service'):
            _clean(expense_request(expense_category='GOODS'))

    def test_the_desk_may_correct_them_and_need_not_resend_them(self):
        f = _clean(expense_request(expense_bill_number='', expense_bill_date='', expense_category='ITEM'),
                   desk=True).fields
        self.assertEqual((f['expense_category'], f['expense_bill_number']), ('ITEM', ''))
        for name in ('budget_code', 'expense_category', 'expense_bill_number', 'expense_bill_date'):
            self.assertIn(name, request_service.EXPENSE_EDITABLE)

    def test_an_imprest_is_asked_none_of_it(self):
        imprest = {'card_code': 'ORGV000044', 'card_name': 'ZIYAUL HAQUE IMPREST JWPL0042', 'party_type': 'vendor'}
        f = _clean(expense_request(expense_payee='IMPREST', partner_code='ORGV000044', expense_category='',
                                   expense_bill_number='', expense_bill_date=''), partner=imprest).fields
        self.assertEqual((f['expense_category'], f['expense_bill_number'], f['expense_bill_date']), ('', '', None))
        self.assertFalse(request_service.needs_due_date_proof(f))


def _bill_row(entry, num, doc_type, code, parent, name='X', date='2026-10-01'):
    return {'doc_entry': entry, 'doc_num': num, 'doc_date': date, 'vendor_ref': f'REF{num}', 'doc_type': doc_type,
            'code': code, 'name': name, 'parent': parent, 'group': 'GENERAL EXPENSES'}


class TheVendorsBillHistory(SimpleTestCase):
    """Per category, the latest bill with an expense account, and its accounts."""

    def history(self, rows):
        from advance_payment.services import sap as sap_service
        with mock.patch.object(sap_service, '_run', return_value=rows),                 mock.patch.object(sap_service, '_schema', return_value='S'):
            return sap_service.vendor_expense_history('OIL', 'VENDA000222')

    def test_the_latest_expense_bill_of_each_category(self):
        rows = [
            _bill_row(9, 109, 'S', '1103006', '1103000', date='2026-10-09'),        # newest: no expense line
            _bill_row(8, 108, 'S', '5680011', '5680000', 'ELECTRICITY', '2026-10-08'),
            _bill_row(8, 108, 'S', '5680003', '5680000', 'TELEPHONE', '2026-10-08'),
            _bill_row(7, 107, 'S', '5680025', '5680000', 'LEGAL', '2026-10-07'),     # older: not read
            _bill_row(6, 106, 'I', '5100008', '5100000', 'CASUAL LABOUR', '2026-10-06'),
        ]
        out = self.history(rows)
        self.assertEqual([h['category'] for h in out], ['SERVICE', 'ITEM'])
        self.assertEqual(out[0]['bill']['doc_num'], 108)
        self.assertEqual([(a['code'], a['kind']) for a in out[0]['accounts']],
                         [('5680011', 'INDIRECT'), ('5680003', 'INDIRECT')])
        self.assertEqual([(a['code'], a['kind']) for a in out[1]['accounts']], [('5100008', 'DIRECT')])

    def test_a_category_with_no_expense_bill_has_its_newest_bill_and_no_accounts(self):
        out = self.history([_bill_row(5, 105, 'I', '1103006', '1103000')])
        self.assertEqual(out, [{'category': 'ITEM', 'bill': {'doc_num': 105, 'doc_date': '2026-10-01',
                                                             'vendor_ref': 'REF105'}, 'accounts': []}])
        self.assertEqual(self.history([]), [])


class PaymentNeedsTheCategory(SimpleTestCase):
    """Paid to a vendor, Payment may not approve until the bill's category is set (2026-10-20)."""

    def advance(self, partner_code, category):
        lines = [SimpleNamespace(line_no=1, gl_account='5680011', effect_month='10-2026', tax_code='IGST@18',
                                 variety='CANOLA', state='HR', tds_code='')]
        return SimpleNamespace(request_type=RequestType.EXPENSE, partner_code=partner_code, expense_category=category,
                               sub_budget_code='IT', effect_month='10-2026', expense_bpl_id=2,
                               expense_bill_number='INV-9', expense_bill_date=date(2026, 9, 1),
                               expense_grpo_entry=None, expense_lines=SimpleNamespace(all=lambda: lines))

    def test_a_vendor_expense_needs_it_an_imprest_does_not(self):
        self.assertEqual(flow_service.expense_problems(self.advance('VENDA000101', '')),
                         ["Choose the bill's Category: Item or Service."])
        self.assertEqual(flow_service.expense_problems(self.advance('VENDA000101', 'SERVICE')), [])
        self.assertEqual(flow_service.expense_problems(self.advance('ORGV000044', '')), [])


class TheBillsDocDateInSap(SimpleTestCase):
    """Posted to SAP (2026-10-20): Document Date = the bill's Doc Date; Posting Date = the day it is posted."""

    def test_a_vendor_expense_carries_its_bills_date(self):
        lines = [expense_line(1, '11800', '5680011')]
        body = voucher_service.build_payload(
            advance(lines, partner_code='VENDA000101', expense_bill_date=date(2026, 9, 1)),
            payout(line(1, 'NEFT', '11800')), posting_date=DAY, series=2601, bpl_id=1, memo='OMS AP-2026-0021/1')
        self.assertEqual((body['DocDate'], body['TaxDate']), (DAY.isoformat(), '2026-09-01'))

    def test_without_one_both_are_the_posting_day(self):
        body = voucher_service.build_payload(advance([expense_line(1, '10', '5680011')]),
                                             payout(line(1, 'NEFT', '10')), posting_date=DAY, series=2601,
                                             bpl_id=1, memo='OMS AP-2026-0021/1')
        self.assertEqual((body['DocDate'], body['TaxDate']), (DAY.isoformat(), DAY.isoformat()))


# ---------------------------------------------------------------------------
# An Expense to a vendor is booked as an A/P invoice (2026-10-20)
# ---------------------------------------------------------------------------
from advance_payment.services import ap_invoice  # noqa: E402


def ap_line(no, taxable, gl='5680011', **extra):
    values = dict(pk=100 + no, line_no=no, taxable_amount=Decimal(taxable), amount=Decimal(taxable),
                  gl_account=gl, gl_name='Rent', effect_month='', remarks='', tds_amount=Decimal('0'),
                  tds_code='', tds_account='', tax_code='IGST@18', variety='CANOLA', state='HR', location=None)
    values.update(extra)
    return SimpleNamespace(**values)


def vendor_bill(lines=(), **extra):
    values = dict(partner_code='VENDA000101', partner_name='ABC Technologies', expense_category='SERVICE',
                  expense_bill_number='inv/ 2026-118', expense_bill_date=date(2026, 9, 1), expense_due_date=None,
                  expense_bpl_id=2, expense_bpl_name='FACTORY', expense_grpo_entry=None, expense_grpo_num=None,
                  expense_tds_code='')
    values.update(extra)
    return advance(lines, **values)


class TheApInvoiceIsForAVendorsExpense(SimpleTestCase):
    def test_a_vendor_expense_books_one_an_imprest_or_a_payment_does_not(self):
        self.assertTrue(ap_invoice.applies(vendor_bill()))
        self.assertFalse(ap_invoice.applies(vendor_bill(partner_code='ORGV000044')))
        self.assertFalse(ap_invoice.applies(vendor_bill(request_type=RequestType.VENDOR)))

    def test_what_the_accounts_desk_must_set(self):
        bare = vendor_bill([ap_line(1, '100', tax_code='', variety='', state='')], expense_bpl_id=None)
        self.assertEqual(ap_invoice.problems(bare), [
            'Choose the Branch the A/P invoice is booked to.',
            'Line 1: choose its Tax Code, Variety, State.'])
        self.assertEqual(ap_invoice.problems(vendor_bill([ap_line(1, '100')])), [])

    def test_one_tds_code_per_invoice(self):
        bill = vendor_bill([ap_line(1, '100', tds_code='C194'), ap_line(2, '50', tds_code='1024')])
        self.assertEqual(ap_invoice.problems(bill), ['An A/P invoice takes one TDS code; the lines have 1024, C194.'])

    def test_an_item_bill_needs_its_grpo_not_line_fields(self):
        item = vendor_bill([ap_line(1, '100', tax_code='', variety='', state='')], expense_category='ITEM')
        self.assertEqual(ap_invoice.problems(item), ['Choose the GRPO this Item bill is copied from.'])
        self.assertEqual(ap_invoice.problems(vendor_bill([ap_line(1, '100')], expense_category='ITEM',
                                                         expense_grpo_entry=27962)), [])

    def test_the_accounts_desk_needs_no_bank_details_for_it(self):
        bill = vendor_bill([ap_line(1, '100')])
        self.assertEqual(flow_service.expense_problems(bill), [])


class TheApInvoicePayload(SimpleTestCase):
    def test_a_service_bill_line_by_line_with_its_five_dimensions(self):
        bill = vendor_bill([ap_line(1, '10000.00', remarks='October rent', tds_code='C194'),
                            ap_line(2, '500.00', gl='5690002', tax_code='RIGST@5', variety='OLIVE', state='DL',
                                    location=1, effect_month='09-2026')],
                           expense_due_date=date(2026, 10, 30))
        body = ap_invoice.build_payload(bill, posting_date=DAY, series=3686, bpl_id=2, memo='OMS AP-2026-0021/1',
                                        default_location=2)
        self.assertEqual({k: body[k] for k in ('CardCode', 'DocType', 'DocDate', 'TaxDate', 'DocDueDate', 'NumAtCard',
                                               'BPL_IDAssignedToInvoice', 'Series', 'JournalMemo')},
                         {'CardCode': 'VENDA000101', 'DocType': 'dDocument_Service', 'DocDate': DAY.isoformat(),
                          'TaxDate': '2026-09-01', 'DocDueDate': '2026-10-30', 'NumAtCard': 'INV/ 2026-118',
                          'BPL_IDAssignedToInvoice': 2, 'Series': 3686, 'JournalMemo': 'OMS AP-2026-0021/1'})
        first, second = body['DocumentLines']
        self.assertEqual(first, {'AccountCode': '5680011', 'ItemDescription': 'October rent', 'LineTotal': 10000.0,
                                 'TaxCode': 'IGST@18', 'LocationCode': 2, 'CostingCode': 'CANOLA',
                                 'CostingCode2': '10-2026', 'CostingCode3': 'Factory', 'CostingCode4': 'Accounts',
                                 'CostingCode5': 'HR', 'WTLiable': 'tYES'})
        self.assertEqual((second['LocationCode'], second['CostingCode2'], second['WTLiable']), (1, '09-2026', 'tNO'))
        self.assertEqual(body['WithholdingTaxDataCollection'], [{'WTCode': 'C194'}])

    def test_an_item_bill_is_copied_from_its_grpos_open_lines(self):
        grpo = {'doc_entry': 27962, 'category': 'ITEM', 'lines': [{'line_num': 1}, {'line_num': 3}]}
        bill = vendor_bill([ap_line(1, '100')], expense_category='ITEM', expense_grpo_entry=27962,
                           expense_tds_code='194Q')
        body = ap_invoice.build_payload(bill, posting_date=DAY, series=1, bpl_id=2, memo='m', grpo=grpo)
        self.assertEqual(body['DocType'], 'dDocument_Items')
        self.assertEqual(body['DocumentLines'], [
            {'BaseType': 20, 'BaseEntry': 27962, 'BaseLine': 1, 'WTLiable': 'tYES'},
            {'BaseType': 20, 'BaseEntry': 27962, 'BaseLine': 3, 'WTLiable': 'tYES'}])
        self.assertEqual(body['WithholdingTaxDataCollection'], [{'WTCode': '194Q'}])

    def test_the_series_kind_follows_gst_charged(self):
        taxes = [{'code': 'IGST@18', 'rate': '18', 'reverse_charge': False},
                 {'code': 'RIGST@5', 'rate': '5', 'reverse_charge': True},
                 {'code': 'Exampt', 'rate': '0', 'reverse_charge': False}]
        self.assertTrue(ap_invoice.charges_gst(taxes, ['Exampt', 'IGST@18']))
        self.assertFalse(ap_invoice.charges_gst(taxes, ['RIGST@5', 'Exampt']))


class TheBillMatchesItsGrpo(SimpleTestCase):
    def test_the_bill_number_is_matched_on_letters_and_digits(self):
        from advance_payment.services import sap as sap_service

        self.assertEqual(sap_service.bill_key(' inv/ 2026-118 '), 'INV2026118')
        rows = [
            {'doc_entry': 2, 'doc_num': 20, 'doc_date': None, 'vendor_ref': 'X-1', 'doc_total': 5, 'bpl_id': 2,
             'bpl_name': 'F', 'attachment_entry': None, 'doc_type': 'I', 'line_num': 0, 'item_code': 'A',
             'description': '', 'open_qty': 1, 'price': 5, 'open_amount': 5, 'tax_code': '', 'warehouse': ''},
            {'doc_entry': 1, 'doc_num': 10, 'doc_date': None, 'vendor_ref': 'INV/2026/118', 'doc_total': 7,
             'bpl_id': 2, 'bpl_name': 'F', 'attachment_entry': 9, 'doc_type': 'S', 'line_num': 0, 'item_code': '',
             'description': 'Oil', 'open_qty': 0, 'price': 7, 'open_amount': 7, 'tax_code': 'GST05R',
             'warehouse': ''}]
        with mock.patch.object(sap_service, '_run', return_value=rows), \
                mock.patch.object(sap_service, '_schema', return_value='S'):
            found = sap_service.open_grpos('OIL', 'VENDA000101', bill_number='inv 2026 118')
        self.assertEqual([(g['doc_num'], g['matches'], g['category']) for g in found],
                         [(10, True, 'SERVICE'), (20, False, 'ITEM')])
