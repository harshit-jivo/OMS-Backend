"""Expense requests: paid straight to expense G/L accounts.

No database and no SAP: rows are stand-ins, managers and SAP calls are patched.
What is pinned:

* the form: one kind per request (direct or indirect expense, each with its
  own G/L accounts); each line a taxable amount and its GST (for the record:
  the invoice value is taxable + GST); G/L optional (remarks then required);
  no month (the Payment desk sets it); budget head and sub budget required,
  with SAP's head -> sub budget rules; no Payment Purpose; a SAP vendor
  optional, naming who is paid;
* the Payment desk may correct the whole request and sets its TDS — one code
  for the request, any line its own or none — on each line's taxable amount,
  rounded to the rupee; logged as "Edited at Payment";
* the Payment stage cannot approve while a line has no G/L or no month;
* Audit's approval posts an Expense (its route has no Final);
* SAP: a TDS journal (Dr each line's G/L with its four dimensions, Cr the
  TDS account), and an account payment paying each line's invoice value less
  its TDS, with Variety / Month / Budget / Sub Budget in ProfitCenter..4.
"""
import contextlib
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

ACCOUNTS = {
    'INDIRECT': [{'code': '5680011', 'name': 'ELECTRICITY EXPENSES', 'group': 'POWER & FUEL', 'kind': 'INDIRECT'},
                 {'code': '5670001', 'name': 'RENT', 'group': 'ADMIN', 'kind': 'INDIRECT'}],
    'DIRECT': [{'code': '5100008', 'name': 'CASUAL LABOUR', 'group': 'DIRECT EXPENSE', 'kind': 'DIRECT'}],
}
MONTHS = ['11-2026', '10-2026', '09-2026']
TDS_CODES = [{'code': 'C194-2', 'name': '194C Contractor 2%', 'rate': '2', 'account': '2133011',
              'account_name': 'TDS PAYABLE', 'assigned': False},
             {'code': 'J194-10', 'name': '194J Professional 10%', 'rate': '10', 'account': '2133012',
              'account_name': 'TDS PAYABLE 194J', 'assigned': False}]


def expense_request(**overrides):
    data = {
        'company': 'OIL', 'request_type': 'EXPENSE', 'payment_against': 'INDIRECT_EXPENSE',
        'partner_name': 'PSPCL (electricity board)', 'payment_date': '2026-10-08',
        'remarks': 'Factory power bill', 'owner_label': 'Finance desk',
        'budget_code': 'Factory', 'sub_budget_code': 'Accounts', 'is_electricity': True,
        'expense_lines': [
            {'taxable_amount': '10000', 'gst_code': 'CGST_SGST_18', 'gl_account': '5680011', 'remarks': 'Unit 1'},
            {'taxable_amount': '3000.50', 'gst_code': '', 'gl_account': '', 'remarks': 'Penalty, G/L to confirm'},
        ],
    }
    data.update(overrides)
    return data


VENDOR = {'card_code': 'VENDA000101', 'card_name': 'ABC Technologies', 'party_type': 'vendor'}


@contextlib.contextmanager
def sap_lookups(partner=VENDOR):
    with mock.patch('advance_payment.services.sap.budgets', return_value=BUDGETS), \
            mock.patch('advance_payment.services.sap.expense_accounts',
                       side_effect=lambda company, kind=None: ACCOUNTS[kind]), \
            mock.patch('advance_payment.services.sap.expense_months', return_value=MONTHS), \
            mock.patch('advance_payment.services.sap.tds_codes', return_value=TDS_CODES), \
            mock.patch('advance_payment.services.sap.partner', return_value=partner) as found:
        yield found


def _clean(data, partner=VENDOR, desk=False):
    with sap_lookups(partner):
        return request_service.clean(data, desk=desk)


class TheExpenseForm(SimpleTestCase):
    def test_an_expense_is_worth_its_lines_invoice_values(self):
        cleaned = _clean(expense_request())
        f = cleaned.fields
        # 10000 + 18% GST = 11800, and 3000.50 with no GST.
        self.assertEqual(f['amount'], Decimal('14800.50'))
        self.assertEqual((f['request_type'], f['payment_against'], f['partner_code'], f['partner_name']),
                         ('EXPENSE', 'INDIRECT_EXPENSE', '', 'PSPCL (electricity board)'))
        self.assertEqual((f['budget_code'], f['sub_budget_code'], f['sub_budget_name']), ('Factory', 'Accounts', 'Accounts'))
        self.assertEqual((f['effect_month'], f['is_electricity'], f['purpose_code']), ('', True, ''))
        self.assertEqual([(ln['taxable_amount'], ln['gst_code'], ln['gst_amount'], ln['amount'], ln['gl_account'],
                           ln['gl_name']) for ln in cleaned.expense_lines],
                         [(Decimal('10000'), 'CGST_SGST_18', Decimal('1800.00'), Decimal('11800.00'), '5680011',
                           'ELECTRICITY EXPENSES'),
                          (Decimal('3000.50'), '', Decimal('0.00'), Decimal('3000.50'), '', '')])
        self.assertEqual(cleaned.documents, [])

    def test_gst_is_worked_to_the_paisa(self):
        data = expense_request(expense_lines=[{'taxable_amount': '999.99', 'gst_code': 'IGST_5', 'gl_account': '5670001'}])
        ln = _clean(data).expense_lines[0]
        self.assertEqual((ln['gst_amount'], ln['amount']), (Decimal('50.00'), Decimal('1049.99')))

    def test_the_requester_sets_no_month_and_no_tds(self):
        data = expense_request(expense_tds_code='C194-2',
                               expense_lines=[{'taxable_amount': '10', 'gl_account': '5670001', 'tds_override': 'J194-10'}])
        cleaned = _clean(data)
        self.assertEqual((cleaned.fields['effect_month'], cleaned.fields['expense_tds_code']), ('', ''))
        self.assertEqual((cleaned.expense_lines[0]['tds_override'], cleaned.expense_lines[0]['tds_amount']),
                         ('', Decimal('0')))

    def test_what_an_expense_must_have(self):
        data = expense_request(partner_name='', sub_budget_code='', expense_lines=[])
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(data)
        problems = ' '.join(caught.exception.problems)
        for expected in ('Say who is being paid', 'Choose the Sub Budget', 'Add at least one expense line'):
            self.assertIn(expected, problems)
        self.assertNotIn('Payment Purpose', problems)  # routed by budget head, not purpose
        self.assertNotIn('Month', problems)  # the Payment desk's to set

    def test_each_line_needs_a_taxable_amount_a_gst_choice_and_a_gl_or_remarks(self):
        data = expense_request(expense_lines=[{'taxable_amount': '10', 'gl_account': ''},
                                              {'taxable_amount': '10', 'gl_account': '1104107', 'remarks': 'x'},
                                              {'taxable_amount': '0', 'gl_account': '5670001'},
                                              {'taxable_amount': '5', 'gl_account': '5670001', 'gst_code': 'GST_12'},
                                              {'taxable_amount': '5', 'gl_account': '5670001', 'effect_month': '13-2026'}])
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(data)
        problems = caught.exception.problems
        self.assertIn('Line 1: choose the G/L account, or say in the remarks what it is for.', problems)
        self.assertIn('Line 2: 1104107 is not an indirect expense account in SAP.', problems)
        self.assertIn('Line 3: enter a taxable amount above zero.', problems)
        self.assertIn('Line 4: "GST_12" is not a GST choice.', problems)
        self.assertIn('Line 5: month "13-2026" is not one of SAP\'s Effective Months.', problems)

    def test_a_line_with_a_gl_needs_no_remarks(self):
        data = expense_request(expense_lines=[{'taxable_amount': '10', 'gl_account': '5670001', 'remarks': ''}])
        self.assertEqual(_clean(data).expense_lines[0]['remarks'], '')

    def test_direct_and_indirect_each_take_only_their_own_accounts(self):
        direct = expense_request(payment_against='DIRECT_EXPENSE',
                                 expense_lines=[{'taxable_amount': '10', 'gl_account': '5100008'}])
        self.assertEqual(_clean(direct).expense_lines[0]['gl_name'], 'CASUAL LABOUR')
        mixed = expense_request(payment_against='DIRECT_EXPENSE',
                                expense_lines=[{'taxable_amount': '10', 'gl_account': '5670001'}])
        with self.assertRaisesRegex(request_service.RequestInvalid, '5670001 is not a direct expense account'):
            _clean(mixed)

    def test_sap_ties_some_budget_heads_to_their_sub_budgets(self):
        self.assertEqual(_clean(expense_request(budget_code='BackOff', sub_budget_code='IT')).fields['sub_budget_code'],
                         'IT')
        with mock.patch.dict('advance_payment.services.sap.SUB_BUDGET_RULES', {'OIL': {'BackOff': {'Accounts'}}}):
            with self.assertRaisesRegex(request_service.RequestInvalid, 'Under BackOff, SAP allows the Sub Budget '
                                                                        'Accounts — not "IT"'):
                _clean(expense_request(budget_code='BackOff', sub_budget_code='IT'))

    def test_a_vendor_is_optional_and_names_who_is_paid(self):
        f = _clean(expense_request(partner_code='VENDA000101', partner_name='')).fields
        self.assertEqual((f['partner_code'], f['partner_name']), ('VENDA000101', 'ABC Technologies'))
        f = _clean(expense_request(partner_code='VENDA000101', partner_name='ABC (Ludhiana)')).fields
        self.assertEqual(f['partner_name'], 'ABC (Ludhiana)')
        with self.assertRaisesRegex(request_service.RequestInvalid, "CUSTA1 is not a vendor in OIL's SAP"):
            _clean(expense_request(partner_code='CUSTA1'), partner={**VENDOR, 'party_type': 'customer'})
        with self.assertRaisesRegex(request_service.RequestInvalid, 'NOPE is not a vendor'):
            _clean(expense_request(partner_code='NOPE'), partner=None)

    def test_an_expense_takes_lines_not_documents(self):
        data = expense_request(documents=[{'kind': 'BILL', 'sap_doc_entry': 1, 'amount': '1'}])
        with self.assertRaisesRegex(request_service.RequestInvalid, 'pays its lines, not documents'):
            _clean(data)


class TheDesksTds(SimpleTestCase):
    def test_one_code_for_the_request_on_each_lines_taxable_amount_any_line_its_own_or_none(self):
        data = expense_request(effect_month='10-2026', expense_tds_code='C194-2', expense_lines=[
            {'taxable_amount': '10000', 'gst_code': 'CGST_SGST_18', 'gl_account': '5680011'},
            {'taxable_amount': '2500', 'gl_account': '5670001', 'tds_override': 'J194-10'},
            {'taxable_amount': '700', 'gl_account': '5670001', 'tds_override': 'NONE'},
            {'taxable_amount': '1234.50', 'gl_account': '5670001'},
        ])
        cleaned = _clean(data, desk=True)
        self.assertEqual((cleaned.fields['expense_tds_code'], cleaned.fields['effect_month']), ('C194-2', '10-2026'))
        self.assertEqual([(ln['tds_override'], ln['tds_code'], ln['tds_rate'], ln['tds_account'], ln['tds_amount'])
                          for ln in cleaned.expense_lines], [
            ('', 'C194-2', Decimal('2'), '2133011', Decimal('200')),   # 2% of 10000, not of 11800
            ('J194-10', 'J194-10', Decimal('10'), '2133012', Decimal('250')),
            ('NONE', '', None, '', Decimal('0')),
            ('', 'C194-2', Decimal('2'), '2133011', Decimal('25')),     # 24.69 -> the rupee
        ])

    def test_a_tds_code_must_be_sap_s(self):
        data = expense_request(expense_tds_code='X1', expense_lines=[
            {'taxable_amount': '10', 'gl_account': '5670001', 'tds_override': 'X2'}])
        with self.assertRaises(request_service.RequestInvalid) as caught:
            _clean(data, desk=True)
        self.assertIn('TDS: X1 is not an active TDS code at 1, 2, 5 or 10% in SAP.', caught.exception.problems)
        self.assertIn('Line 1: X2 is not an active TDS code at 1, 2, 5 or 10% in SAP.', caught.exception.problems)

    def test_the_payment_methods_pay_the_invoice_value_less_tds(self):
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
    def test_cannot_approve_while_a_line_has_no_gl_or_no_month(self):
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

    def test_payment_corrects_the_whole_expense_sets_month_and_tds_and_it_is_logged(self):
        the_advance = advance([expense_line(1, '5', '')])
        changes = {'budget': {'old': 'Factory — Factory', 'new': 'BackOff — Back Office'}}
        with self.acting(the_advance) as log, sap_lookups(), \
                mock.patch.object(request_service, 'form_of', return_value=expense_request()), \
                mock.patch.object(request_service, 'edit_expense', return_value=changes) as write:
            flow_service.edit_expense(the_advance, {
                'budget_code': 'BackOff', 'sub_budget_code': 'IT', 'partner_code': 'VENDA000101',
                'request_type': 'VENDOR', 'effect_month': '10-2026', 'expense_tds_code': 'C194-2',
                'expense_lines': [{'taxable_amount': '20', 'gl_account': '5670001'}],
            }, user=SimpleNamespace(pk=1))
        cleaned = write.call_args.args[1]
        # Laid over the request: only what the desk may change.
        f = cleaned.fields
        self.assertEqual((f['budget_code'], f['request_type'], f['effect_month']), ('BackOff', 'EXPENSE', '10-2026'))
        self.assertEqual((f['partner_code'], f['amount'], f['expense_tds_code']), ('VENDA000101', Decimal('20'), 'C194-2'))
        self.assertEqual([(ln['gl_account'], ln['tds_amount']) for ln in cleaned.expense_lines],
                         [('5670001', Decimal('0'))])  # 2% of 20 is 40 paise: nothing to the rupee
        self.assertEqual(log.call_args.args[1], 'PAYMENT_EDITED')
        self.assertEqual(log.call_args.kwargs['data'], changes)

    def test_a_correction_is_checked_as_the_form_is(self):
        the_advance = advance([expense_line(1, '5', '')])
        with self.acting(the_advance), sap_lookups(), \
                mock.patch.object(request_service, 'form_of', return_value=expense_request()):
            with self.assertRaisesRegex(flow_service.FlowError, 'not an indirect expense account'):
                flow_service.edit_expense(the_advance,
                                          {'expense_lines': [{'taxable_amount': '5', 'gl_account': '1104107'}]},
                                          user=SimpleNamespace(pk=1))

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
                    'is_electricity': False, 'expense_tds_code': 'C194-2'},
            expense_lines=[{'line_no': 1, 'amount': Decimal('20')}])
        with mock.patch.object(request_service, '_snapshot', side_effect=[{'budget': 'Factory'}, {'budget': 'BackOff'}]), \
                mock.patch.object(request_service, '_expense_lines',
                                  side_effect=[{'Line 1': 'no G/L · 5.00'}, {'Line 1': '5670001 · 20.00'}]), \
                mock.patch.object(request_service, '_write_expense_lines') as write:
            changes = request_service.edit_expense(row, cleaned)
        self.assertEqual((row.budget_code, row.partner_code, row.amount, row.expense_tds_code),
                         ('BackOff', 'VENDA000101', Decimal('20'), 'C194-2'))
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
