"""Payment requests: the rules that decide money and routing.

No database and no SAP, like `tests.py`: the rows are stand-ins and the
managers and SAP calls are patched. What is pinned:

* a route is any number of freely named approval stages, then Payment,
  Audit and Final by name, last and in order — anything else is refused;
* the server recomputes what the form sends: line totals, percentages, the
  open amount, and which Type / Payment Against pairs exist;
* the payment details must add up and fit SAP's one bank per payment;
* the SAP payload follows what Oil's own outgoing payments look like;
* a payment whose answer was lost is adopted from SAP, never posted twice;
* SAP is written once, when Final approves, and a request already in SAP
  is not posted again.
"""
import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.models import StageRole
from advance_payment.services import flow as flow_service
from advance_payment.services import payout as payout_service
from advance_payment.services import requests as request_service
from advance_payment.services import voucher as voucher_service


def stage(pk, name, sequence):
    return SimpleNamespace(id=pk, pk=pk, name=name, sequence=sequence, user_id=100 + pk)


FULL_ROUTE = [
    stage(1, 'Sub-HOD Approval', 1), stage(2, 'HOD Approval', 2), stage(3, 'Director Approval', 3),
    stage(4, 'Payment Approval', 4), stage(5, 'Audit Approval', 5), stage(6, 'Final Approval', 6),
]


class TheRoute(SimpleTestCase):
    def test_any_stages_before_payment_are_approvals(self):
        named = flow_service.roles(FULL_ROUTE)
        self.assertEqual([r for _s, r in named], [StageRole.APPROVAL] * 3 + [
            StageRole.PAYMENT, StageRole.AUDIT, StageRole.FINAL])

    def test_the_approvals_may_be_named_freely_and_be_any_number(self):
        five = [stage(10 + i, name, i) for i, name in enumerate(
            ['Team Lead', 'Sub-HOD Approval', 'HOD Approval', 'Plant Head', 'Gurpreet VG'], start=1)]
        named = flow_service.roles(five + FULL_ROUTE[3:])
        self.assertEqual([r for _s, r in named][:5], [StageRole.APPROVAL] * 5)

    def test_a_route_may_have_no_approvals_at_all(self):
        named = flow_service.roles(FULL_ROUTE[3:])
        self.assertEqual(named[0][1], StageRole.PAYMENT)

    def test_names_are_matched_ignoring_case_and_spacing(self):
        self.assertEqual(StageRole.of('  audit   APPROVAL '), StageRole.AUDIT)
        self.assertEqual(StageRole.of('Director Approval'), StageRole.APPROVAL)

    def test_refuses_an_approval_after_payment(self):
        route = FULL_ROUTE[:4] + [stage(9, 'Director Approval', 5)] + FULL_ROUTE[4:]
        with self.assertRaisesRegex(flow_service.FlowError, 'must end with three stages'):
            flow_service.roles(route)

    def test_refuses_the_fixed_stages_out_of_order(self):
        swapped = [FULL_ROUTE[0], FULL_ROUTE[4], FULL_ROUTE[3], FULL_ROUTE[5]]
        with self.assertRaisesRegex(flow_service.FlowError, 'in that order'):
            flow_service.roles(swapped)

    def test_refuses_a_route_without_audit(self):
        with self.assertRaisesRegex(flow_service.FlowError, 'Audit Approval'):
            flow_service.roles([FULL_ROUTE[3], FULL_ROUTE[5]])


FINANCE = SimpleNamespace(pk=35, id=35, name='Finance')


#: The company's Budget and Sub Budget cost centres, as SAP lists them.
BUDGETS = [
    {'kind': 'BUDGET', 'code': 'BackOff', 'name': 'Back Office'},
    {'kind': 'BUDGET', 'code': 'Factory', 'name': 'Factory'},
    {'kind': 'SUB_BUDGET', 'code': 'Accounts', 'name': 'Accounts'},
    {'kind': 'SUB_BUDGET', 'code': 'IT', 'name': 'IT'},
]


def _clean(data, *, subs=(92, 88), sub_found=True):
    """`requests.clean` with the department tables and SAP's budgets stood in for."""
    department_qs = mock.Mock()
    department_qs.first.return_value = FINANCE

    sub_qs = mock.Mock()
    sub_qs.exists.return_value = bool(subs)
    sub_qs.filter.return_value.first.return_value = (
        SimpleNamespace(pk=92, id=92, name='AP') if sub_found else None)
    with mock.patch.object(request_service.Department.objects, 'filter', return_value=department_qs), \
            mock.patch.object(request_service.SubDepartment.objects, 'filter', return_value=sub_qs), \
            mock.patch('advance_payment.services.sap.budgets', return_value=BUDGETS):
        return request_service.clean(data)


def vendor_bill_request(**overrides):
    data = {
        'company': 'OIL', 'request_type': 'VENDOR', 'payment_against': 'AGAINST_BILL',
        'partner_code': 'VENDA001465', 'partner_name': 'GODAMWALE TRADING',
        'department_id': 35, 'sub_department_id': 92, 'payment_date': '2026-10-01',
        'priority': 'HIGH', 'remarks': 'Part settlement', 'owner_label': 'Finance desk',
        'budget_code': 'BackOff', 'sub_budget_code': 'Accounts',
        'documents': [
            {'kind': 'BILL', 'sap_doc_entry': 501, 'sap_doc_num': '126226523', 'original_amount': '300000',
             'paid_amount': '0', 'open_amount': '288746', 'mode': 'FIXED', 'amount': '200000'},
            {'kind': 'BILL', 'sap_doc_entry': 502, 'sap_doc_num': '126226524', 'original_amount': '50000',
             'paid_amount': '10000', 'open_amount': '40000', 'mode': 'FIXED', 'amount': '40000'},
        ],
    }
    data.update(overrides)
    return data


class WhatTheServerAccepts(SimpleTestCase):
    def test_a_bill_request_is_worth_its_lines(self):
        cleaned = _clean(vendor_bill_request())
        self.assertEqual(cleaned.fields['amount'], Decimal('240000.00'))
        self.assertEqual([d['sap_doc_entry'] for d in cleaned.documents], [501, 502])
        self.assertEqual(cleaned.fields['sub_department'].name, 'AP')

    def test_a_typed_amount_is_ignored_where_documents_decide_it(self):
        cleaned = _clean(vendor_bill_request(amount='999999'))
        self.assertEqual(cleaned.fields['amount'], Decimal('240000.00'))

    def test_refuses_a_line_above_what_is_open(self):
        data = vendor_bill_request()
        data['documents'][1]['amount'] = '40000.01'
        with self.assertRaisesRegex(request_service.RequestInvalid, 'more than the 40000.00 still open'):
            _clean(data)

    def test_a_bill_is_paid_by_amount_only(self):
        data = vendor_bill_request()
        data['documents'][0].update(mode='PERCENT', percentage='50')
        with self.assertRaisesRegex(request_service.RequestInvalid, 'by amount only'):
            _clean(data)

    def test_a_po_percentage_is_worth_its_share_of_what_is_open(self):
        data = vendor_bill_request(payment_against='AGAINST_PO', expected_date='2026-11-01', documents=[
            {'kind': 'PO', 'sap_doc_entry': 14008, 'sap_doc_num': '126226600', 'original_amount': '500000',
             'paid_amount': '150000', 'open_amount': '350000', 'mode': 'PERCENT', 'percentage': '25',
             'amount': '1'}])
        cleaned = _clean(data)
        self.assertEqual(cleaned.documents[0]['amount'], Decimal('87500.00'))
        self.assertEqual(cleaned.fields['amount'], Decimal('87500.00'))

    def test_against_po_needs_the_expected_bill_date(self):
        data = vendor_bill_request(payment_against='AGAINST_PO', documents=[
            {'kind': 'PO', 'sap_doc_entry': 14008, 'open_amount': '350000', 'amount': '1000'}])
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Expected Bill Date'):
            _clean(data)

    def test_refuses_a_pair_the_form_does_not_offer(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not offered'):
            _clean(vendor_bill_request(payment_against='ADVANCE', documents=[], amount='1000'))

    def test_a_department_with_sub_departments_needs_one(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Sub-department of Finance'):
            _clean(vendor_bill_request(sub_department_id=None))

    def test_keeps_the_payment_purpose_with_sap_names(self):
        cleaned = _clean(vendor_bill_request())
        self.assertEqual(
            {k: cleaned.fields[k] for k in ('budget_code', 'budget_name', 'sub_budget_code', 'sub_budget_name')},
            {'budget_code': 'BackOff', 'budget_name': 'Back Office',
             'sub_budget_code': 'Accounts', 'sub_budget_name': 'Accounts'})

    def test_the_payment_purpose_is_required(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, r'Payment Purpose \(Sub Budget\)'):
            _clean(vendor_bill_request(sub_budget_code=''))

    def test_refuses_a_budget_sap_does_not_have(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Budget "Nowhere" is not an active budget'):
            _clean(vendor_bill_request(budget_code='Nowhere'))

    def test_a_sub_budget_is_not_a_budget(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Budget "IT" is not an active budget'):
            _clean(vendor_bill_request(budget_code='IT'))

    def test_the_owner_is_found_by_the_code_in_their_label(self):
        preshit = SimpleNamespace(pk=12, employee_code='JWPL0030')
        with mock.patch.object(request_service.Employee.objects, 'alive') as alive:
            alive.return_value.filter.return_value.first.return_value = preshit
            cleaned = _clean(vendor_bill_request(owner_label='Preshit Singh (jwpl0030)'))
        alive.return_value.filter.assert_called_once_with(employee_code='JWPL0030')
        self.assertIs(cleaned.fields['owner_employee'], preshit)

    def test_only_an_employee_may_be_not_in_sap(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not yet in SAP'):
            _clean(vendor_bill_request(partner_code='NOSAP:JWPL0999'))

    def test_an_employee_advance_by_emi(self):
        cleaned = _clean({
            'company': 'OIL', 'request_type': 'EMPLOYEE_ADVANCE', 'payment_against': 'ADVANCE',
            'partner_code': 'NOSAP:JWPL0999', 'partner_name': 'NEW JOINER', 'amount': '60000',
            'department_id': 35, 'sub_department_id': 92, 'payment_date': '2026-10-01',
            'return_method': 'EMI', 'installments': 6, 'emi_amount': '10000',
            'expected_from_date': '2026-11-01', 'expected_to_date': '2027-04-01',
            'owner_label': 'HR', 'remarks': 'Relocation advance',
            'budget_code': 'BackOff', 'sub_budget_code': 'IT',
        })
        self.assertTrue(cleaned.fields['partner_not_in_sap'])
        self.assertEqual(cleaned.fields['installments'], 6)
        self.assertEqual(cleaned.documents, [])


def line(pk, method, amount, account='1104107', **extra):
    values = dict(pk=pk, id=pk, method=method, amount=Decimal(amount), from_account=account,
                  cheque_number='', cheque_bank='', cheque_date=None, cash_notes=None)
    values.update(extra)
    row = SimpleNamespace(**values)
    row.get_method_display = lambda: {'UPI': 'UPI', 'CASH': 'Cash', 'NEFT': 'NEFT',
                                      'RTGS': 'RTGS', 'IMPS': 'IMPS', 'CHEQUE': 'Cheque'}[method]
    return row


def payout(*lines, **extra):
    values = dict(beneficiary_name='GODAMWALE TRADING', to_account_number='50200057911744',
                  to_ifsc='HDFC0001452')
    values.update(extra)
    row = SimpleNamespace(**values)
    row.lines = mock.Mock()
    row.lines.all.return_value = list(lines)
    return row


def payout_problems(amount, the_payout):
    advance = SimpleNamespace(amount=Decimal(amount))
    with mock.patch.object(payout_service.Payout.objects, 'filter') as found:
        found.return_value.first.return_value = the_payout
        return payout_service.problems(advance)


class ThePaymentDetails(SimpleTestCase):
    def test_complete_details_have_no_problems(self):
        self.assertEqual(payout_problems('240000', payout(line(1, 'NEFT', '240000'))), [])

    def test_the_methods_must_add_up_to_the_request(self):
        problems = payout_problems('240000', payout(line(1, 'NEFT', '200000')))
        self.assertIn('The payment methods add up to 200000, but the request is for 240000.', problems)

    def test_upi_only_below_one_lakh(self):
        problems = payout_problems('100000', payout(line(1, 'UPI', '100000')))
        self.assertTrue(any('UPI is only for amounts below' in p for p in problems))

    def test_one_bank_per_sap_payment(self):
        problems = payout_problems('240000', payout(
            line(1, 'NEFT', '140000', '1104107'), line(2, 'RTGS', '100000', '1104106')))
        self.assertTrue(any('same account' in p for p in problems))

    def test_cash_notes_must_equal_the_amount(self):
        problems = payout_problems('5000', payout(line(1, 'CASH', '5000', '1105001', cash_notes=[
            {'denomination': 500, 'quantity': 9}])))
        self.assertIn('Method 1: the cash notes add up to 4500, not 5000.', problems)

    def test_cash_needs_no_payee_account(self):
        problems = payout_problems('5000', payout(line(1, 'CASH', '5000', '1105001', cash_notes=[
            {'denomination': 500, 'quantity': 10}]), to_account_number='', to_ifsc=''))
        self.assertEqual(problems, [])


def advance_of(request_type='VENDOR', *, documents=(), amount='240000', partner='VENDA001465'):
    advance = SimpleNamespace(
        request_no='AP-2026-0007', request_type=request_type, partner_code=partner,
        partner_name='GODAMWALE TRADING', amount=Decimal(amount), currency='INR',
        remarks='Part settlement', company='OIL')
    advance.documents = mock.Mock()
    advance.documents.all.return_value = list(documents)
    return advance


def doc(entry, amount, kind='BILL', num=None):
    return SimpleNamespace(kind=kind, sap_doc_entry=entry, sap_doc_num=num or str(entry),
                           amount=Decimal(amount))


DAY = datetime.date(2026, 9, 25)


class TheSapPayment(SimpleTestCase):
    def build(self, advance, the_payout):
        return voucher_service.build_payload(advance, the_payout, posting_date=DAY, series=2601,
                                             bpl_id=1, memo='OMS AP-2026-0007/1')

    def test_a_bill_payment_names_its_bills(self):
        body = self.build(advance_of(documents=[doc(501, '200000'), doc(502, '40000')]),
                          payout(line(1, 'NEFT', '240000')))
        self.assertEqual(body['DocType'], 'rSupplier')
        self.assertEqual(body['CardCode'], 'VENDA001465')
        self.assertEqual(body['PaymentInvoices'], [
            {'LineNum': 0, 'DocEntry': 501, 'InvoiceType': 'it_PurchaseInvoice', 'SumApplied': 200000.0},
            {'LineNum': 1, 'DocEntry': 502, 'InvoiceType': 'it_PurchaseInvoice', 'SumApplied': 40000.0},
        ])
        self.assertEqual((body['TransferAccount'], body['TransferSum']), ('1104107', 240000.0))
        self.assertEqual((body['Series'], body['BPLID'], body['JournalRemarks']),
                         (2601, 1, 'OMS AP-2026-0007/1'))
        self.assertTrue(body['Remarks'].startswith('OMS AP-2026-0007 | GODAMWALE TRADING | Bill 501, 502'))

    def test_a_po_advance_is_on_account(self):
        body = self.build(advance_of(documents=[doc(14008, '87500', kind='PO')], amount='87500'),
                          payout(line(1, 'NEFT', '87500')))
        self.assertNotIn('PaymentInvoices', body)
        self.assertIn('PO 14008', body['Remarks'])

    def test_an_employee_advance_goes_to_their_advance_account(self):
        body = self.build(advance_of('EMPLOYEE_ADVANCE', amount='60000', partner='11133228'),
                          payout(line(1, 'IMPS', '60000')))
        self.assertEqual(body['DocType'], 'rAccount')
        self.assertNotIn('CardCode', body)
        self.assertEqual(body['PaymentAccounts'][0]['AccountCode'], '11133228')
        self.assertEqual(body['PaymentAccounts'][0]['SumPaid'], 60000.0)

    def test_cash_and_a_cheque_split_into_their_fields(self):
        body = self.build(advance_of(amount='15000'), payout(
            line(1, 'CASH', '5000', '1105001'),
            line(2, 'CHEQUE', '10000', '1104107', cheque_number='252525')))
        self.assertEqual((body['CashAccount'], body['CashSum']), ('1105001', 5000.0))
        self.assertEqual((body['TransferAccount'], body['TransferSum']), ('1104107', 10000.0))
        self.assertEqual(body['TransferReference'], 'CHQ 252525')


class PostingOnce(SimpleTestCase):
    def test_a_payment_already_in_sap_is_adopted_not_posted_again(self):
        advance = mock.Mock(pk=7, request_no='AP-2026-0007', company='OIL')
        advance.vouchers.filter.return_value.count.return_value = 0
        advance.vouchers.aggregate.return_value = {'n': 1}
        with mock.patch.object(voucher_service.AdvanceRequest.objects, 'get', return_value=advance), \
                mock.patch.object(voucher_service.Payout.objects, 'filter') as payouts, \
                mock.patch.object(voucher_service, '_branch', return_value=(1, '')), \
                mock.patch.object(voucher_service, 'build_payload', return_value={'x': 1}), \
                mock.patch.object(voucher_service, '_company_db', return_value='TEST_OIL'), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_series', return_value=2601), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_by_memo',
                                  return_value={'doc_entry': 29150, 'doc_num': 926466970}) as by_memo, \
                mock.patch('payments.sap_client.request') as sap_post, \
                mock.patch.object(voucher_service.SapVoucher.objects, 'create') as create:
            payouts.return_value.first.return_value = object()
            outcome = voucher_service.post(advance, user=SimpleNamespace(pk=1))
        by_memo.assert_called_once_with('OIL', 'OMS AP-2026-0007/1')
        sap_post.assert_not_called()
        self.assertTrue(outcome.ok)
        self.assertEqual(create.call_args.kwargs['status'], 'POSTED')
        self.assertEqual(create.call_args.kwargs['version'], 2)
        self.assertEqual(create.call_args.kwargs['sap_doc_entry'], 29150)

    def test_a_refusal_is_recorded_as_a_failed_voucher(self):
        from payments.sap_client import SapError

        advance = mock.Mock(pk=7, request_no='AP-2026-0007', company='OIL')
        advance.vouchers.filter.return_value.count.return_value = 0
        advance.vouchers.aggregate.return_value = {'n': None}
        with mock.patch.object(voucher_service.AdvanceRequest.objects, 'get', return_value=advance), \
                mock.patch.object(voucher_service.Payout.objects, 'filter') as payouts, \
                mock.patch.object(voucher_service, '_branch', return_value=(1, '')), \
                mock.patch.object(voucher_service, 'build_payload', return_value={'x': 1}), \
                mock.patch.object(voucher_service, '_company_db', return_value='TEST_OIL'), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_series', return_value=2601), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_by_memo', return_value=None), \
                mock.patch('payments.sap_client.request',
                           side_effect=SapError('Balance due exceeded', status_code=400)), \
                mock.patch.object(voucher_service.SapVoucher.objects, 'create') as create:
            payouts.return_value.first.return_value = object()
            outcome = voucher_service.post(advance, user=SimpleNamespace(pk=1))
        self.assertFalse(outcome.ok)
        self.assertIn('Balance due exceeded', outcome.message)
        self.assertEqual(create.call_args.kwargs['status'], 'FAILED')
        self.assertEqual(create.call_args.kwargs['payload'], {'x': 1})


class FinalPosts(SimpleTestCase):
    def run_final(self, *, live, ok=True):
        flow = SimpleNamespace(request=mock.Mock())
        posted = SimpleNamespace(pk=2, version=1, sap_doc_num=926466971, sap_doc_entry=29151)
        with mock.patch.object(voucher_service, 'live', return_value=live), \
                mock.patch.object(voucher_service, 'post') as post, \
                mock.patch.object(flow_service, 'log') as log:
            post.return_value = (voucher_service.Outcome(True, posted) if ok else
                                 voucher_service.Outcome(False, posted, 'SAP refused the payment'))
            error = flow_service._post_voucher(flow, SimpleNamespace(pk=1))
        return error, post, [c.args[1] for c in log.call_args_list]

    def test_final_approval_posts_the_payment(self):
        error, post, logged = self.run_final(live=None)
        self.assertEqual(error, '')
        post.assert_called_once()
        self.assertEqual(logged, ['SAP_POSTED'])

    def test_a_payment_already_in_sap_is_not_posted_again(self):
        error, post, logged = self.run_final(live=SimpleNamespace(pk=1))
        self.assertEqual(error, '')
        post.assert_not_called()
        self.assertEqual(logged, [])

    def test_a_refusal_is_logged_and_returned(self):
        error, _post, logged = self.run_final(live=None, ok=False)
        self.assertEqual(error, 'SAP refused the payment')
        self.assertEqual(logged, ['SAP_POST_FAILED'])


class TypingTheAccountByHand(SimpleTestCase):
    """At Payment, a payee account that is not one of SAP's needs the user's password."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = mock.Mock(pk=1)
        self.user.check_password.side_effect = lambda raw: raw == 'secret'
        self.advance = SimpleNamespace(pk=14, request_type='VENDOR', company='OIL',
                                       partner_code='VENDA000101')

    def confirm(self, password, *, actor=True, role='PAYMENT'):
        flow = SimpleNamespace(current_role=role)
        with mock.patch.object(flow_service.RequestFlow.objects, 'select_related') as rel, \
                mock.patch.object(flow_service, 'is_actor', return_value=actor):
            rel.return_value.get.return_value = flow
            return flow_service.confirm_password(self.advance, user=self.user, password=password)

    def test_the_right_password_gives_a_token_for_this_request_and_user(self):
        token = self.confirm('secret')
        self.assertTrue(flow_service._token_ok(token, self.advance, self.user))
        self.assertFalse(flow_service._token_ok(token, SimpleNamespace(pk=15), self.user))
        self.assertFalse(flow_service._token_ok(token, self.advance, mock.Mock(pk=2)))
        self.assertFalse(flow_service._token_ok('forged', self.advance, self.user))

    def test_a_wrong_password_is_refused_and_counted(self):
        for _ in range(flow_service.MANUAL_MAX_FAILURES):
            with self.assertRaisesRegex(flow_service.FlowError, 'not right'):
                self.confirm('guess')
        # Locked now, even with the right one.
        with self.assertRaisesRegex(flow_service.FlowError, 'Too many'):
            self.confirm('secret')

    def test_only_the_payment_stage_user_may(self):
        with self.assertRaisesRegex(flow_service.FlowError, 'Only the Payment stage user'):
            self.confirm('secret', actor=False)
        with self.assertRaisesRegex(flow_service.FlowError, 'Only the Payment stage user'):
            self.confirm('secret', role='AUDIT')

    def save(self, data, *, from_sap, stored=None):
        """`save_payout` with its rows stood in for (callers patch the transaction away)."""
        flow = SimpleNamespace(current_role='PAYMENT', request=self.advance, version=3,
                               cycle=1, save=mock.Mock())
        with mock.patch.object(flow_service, '_acting', return_value=flow), \
                mock.patch.object(flow_service, '_from_sap', return_value=from_sap), \
                mock.patch.object(flow_service.Payout.objects, 'filter') as payouts, \
                mock.patch.object(flow_service.payout_service, 'save', return_value=(None, True)) as save, \
                mock.patch.object(flow_service, 'log') as log:
            payouts.return_value.first.return_value = stored
            flow_service.save_payout(self.advance, data, user=self.user)
        return save.call_args.args[1], log

    def test_an_account_from_sap_needs_no_password(self):
        with mock.patch('django.db.transaction.Atomic.__enter__'), \
                mock.patch('django.db.transaction.Atomic.__exit__', return_value=False):
            sent, _log = self.save({'to_account_number': '50100234567812', 'to_ifsc': 'HDFC0001234'},
                                   from_sap=True)
        self.assertFalse(sent['to_account_manual'])

    def test_a_typed_account_needs_the_password(self):
        with mock.patch('django.db.transaction.Atomic.__enter__'), \
                mock.patch('django.db.transaction.Atomic.__exit__', return_value=False):
            with self.assertRaisesRegex(flow_service.FlowError, 'Confirm your password'):
                self.save({'to_account_number': '12345678901', 'to_ifsc': 'SBIN0001234'}, from_sap=False)
            token = self.confirm('secret')
            sent, log = self.save({'to_account_number': '12345678901', 'to_ifsc': 'SBIN0001234',
                                   'manual_token': token}, from_sap=False)
        # The server says it was typed, whatever the client claimed.
        self.assertTrue(sent['to_account_manual'])
        self.assertEqual(log.call_args.kwargs['data'], {'manual_account': True, 'account_last4': '8901'})

    def test_a_typed_account_saved_again_unchanged_needs_no_new_password(self):
        stored = SimpleNamespace(to_account_number='12345678901', to_ifsc='SBIN0001234')
        with mock.patch('django.db.transaction.Atomic.__enter__'), \
                mock.patch('django.db.transaction.Atomic.__exit__', return_value=False):
            sent, _log = self.save({'to_account_number': '12345678901', 'to_ifsc': 'SBIN0001234'},
                                   from_sap=False, stored=stored)
        self.assertTrue(sent['to_account_manual'])

    def test_an_employee_account_is_always_typed(self):
        self.advance.request_type = 'EMPLOYEE_ADVANCE'
        self.assertFalse(flow_service._from_sap(self.advance, '12345678901', 'SBIN0001234'))

    def test_matches_the_payees_sap_accounts_by_number_and_ifsc(self):
        rows = [{'account_number': '50100234567812', 'ifsc': 'HDFC0001234'}]
        with mock.patch.object(flow_service.sap_service, 'partner_bank_accounts', return_value=(rows, '')):
            self.assertTrue(flow_service._from_sap(self.advance, '50100234567812', 'HDFC0001234'))
            self.assertFalse(flow_service._from_sap(self.advance, '50100234567812', 'SBIN0001234'))
            self.assertFalse(flow_service._from_sap(self.advance, '99999999999', 'HDFC0001234'))
