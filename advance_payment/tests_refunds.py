"""Customer refunds, what OMS reserves against SAP documents, and Payment returning a request.

No database and no SAP: querysets and lookups are stood in for.
"""
import contextlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.models import DocumentKind, RequestStatus, StageRole
from advance_payment.services import flow as flow_service
from advance_payment.services import requests as request_service
from advance_payment.services import reservations
from advance_payment.services import voucher as voucher_service
from advance_payment.tests_requests import DAY, advance_of, line, payout

BUDGETS = [{'kind': 'BUDGET', 'code': 'Sales', 'name': 'Sales'}]


def _clean(data):
    with mock.patch('advance_payment.services.sap.budgets', return_value=BUDGETS):
        return request_service.clean(data)


def item(entry, amount, obj, direction, line_no=0, open_amount=None):
    return {'kind': 'LEDGER', 'sap_doc_entry': entry, 'sap_line': line_no, 'sap_object': obj,
            'direction': direction, 'sap_doc_num': str(entry), 'original_amount': open_amount or amount,
            'paid_amount': '0', 'open_amount': open_amount or amount, 'mode': 'FIXED', 'amount': amount}


def refund(**overrides):
    """A customer who paid 100 and was invoiced 80: 20 to give back."""
    data = {
        'company': 'OIL', 'request_type': 'CUSTOMER', 'payment_against': 'AGAINST_LEDGER',
        'partner_code': 'CUSTA000846', 'partner_name': 'ISHWER CHAND & SONS', 'payment_date': '2026-10-01',
        'remarks': 'Excess payment', 'owner_label': 'Sales desk', 'budget_code': 'Sales',
        'purpose_code': 'CUSTOMER_REFUND',
        'documents': [item(210827, '100', 24, 'CREDIT', line_no=1), item(74506, '80', 13, 'DEBIT')],
    }
    data.update(overrides)
    return data


class ACustomerRefund(SimpleTestCase):
    def test_is_its_credits_less_its_invoices(self):
        cleaned = _clean(refund())
        self.assertEqual(cleaned.fields['amount'], Decimal('20.00'))
        self.assertEqual([(d['sap_object'], d['direction'], d['sap_line']) for d in cleaned.documents],
                         [(24, 'CREDIT', 1), (13, 'DEBIT', 0)])

    def test_refuses_more_invoices_than_credits(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'credits less debits'):
            _clean(refund(documents=[item(210827, '50', 24, 'CREDIT', 1), item(74506, '80', 13, 'DEBIT')]))

    def test_refuses_an_item_it_cannot_be_applied_to(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'only incoming payments, credit memos'):
            _clean(refund(documents=[item(28797, '100', 46, 'DEBIT')]))

    def test_an_invoice_has_no_journal_line(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'journal line it cannot have'):
            _clean(refund(documents=[item(74506, '80', 13, 'DEBIT', line_no=2),
                                     item(210827, '100', 24, 'CREDIT', 1)]))

    def test_two_lines_of_one_journal_entry_are_two_items(self):
        cleaned = _clean(refund(documents=[item(5001, '30', 30, 'CREDIT', 0), item(5001, '40', 30, 'CREDIT', 1)]))
        self.assertEqual(cleaned.fields['amount'], Decimal('70.00'))

    def test_on_account_is_a_typed_amount(self):
        cleaned = _clean(refund(payment_against='ON_ACCOUNT', documents=[], amount='236'))
        self.assertEqual((cleaned.fields['amount'], cleaned.documents), (Decimal('236.00'), []))

    def test_posts_as_sap_posts_its_own_refunds(self):
        docs = [SimpleNamespace(kind=DocumentKind.LEDGER, sap_doc_entry=210827, sap_line=1, sap_object=24,
                                sap_doc_num='210827', amount=Decimal('100')),
                SimpleNamespace(kind=DocumentKind.LEDGER, sap_doc_entry=74506, sap_line=0, sap_object=13,
                                sap_doc_num='626050737', amount=Decimal('80'))]
        body = voucher_service.build_payload(
            advance_of('CUSTOMER', documents=docs, amount='20', partner='CUSTA000846'),
            payout(line(1, 'NEFT', '20')), posting_date=DAY, series=2601, bpl_id=1, memo='OMS AP-2026-0007/1')
        self.assertEqual((body['DocType'], body['CardCode']), ('rCustomer', 'CUSTA000846'))
        self.assertEqual(body['PaymentInvoices'], [
            {'LineNum': 0, 'DocEntry': 210827, 'DocLine': 1, 'InvoiceType': 'it_Receipt', 'SumApplied': 100.0},
            {'LineNum': 1, 'DocEntry': 74506, 'DocLine': 0, 'InvoiceType': 'it_Invoice', 'SumApplied': 80.0},
        ])
        self.assertEqual(body['TransferSum'], 20.0)
        self.assertIn('Refund against 210827, 626050737', body['Remarks'])

    def test_on_account_posts_no_lines(self):
        body = voucher_service.build_payload(
            advance_of('CUSTOMER', amount='236', partner='CUSTA001144'), payout(line(1, 'NEFT', '236')),
            posting_date=DAY, series=2601, bpl_id=1, memo='OMS AP-2026-0008/1')
        self.assertEqual(body['DocType'], 'rCustomer')
        self.assertNotIn('PaymentInvoices', body)


@contextlib.contextmanager
def _usage_rows(*rows, unadjusted=None, facts=None):
    """`RequestDocument.objects.filter(...).values_list(...)` answering these rows.

    `unadjusted(company, paid_lines)` stands in for SAP's on-account balances;
    by default every paid advance is still wholly on account. `facts` stands in
    for SAP's creation date and total (`{doc_entry: {created_on, doc_total}}`);
    by default nothing is known, which falls back to SAP's open amount less
    what OMS holds.
    """
    qs = mock.Mock()
    qs.values_list.return_value = list(rows)
    fake = unadjusted or (lambda company, lines: [amount for _key, _request, amount in lines])
    with mock.patch.object(reservations.RequestDocument.objects, 'filter', return_value=qs), \
            mock.patch.object(reservations, 'unadjusted', side_effect=fake), \
            mock.patch.object(reservations, 'sap_facts', return_value=dict(facts or {})):
        yield


class WhatOmsHolds(SimpleTestCase):
    # (sap_doc_entry, sap_line, amount, request_id, status)
    ROWS = [(14008, 0, Decimal('10'), 1, RequestStatus.IN_APPROVAL),
            (14008, 0, Decimal('15'), 2, RequestStatus.RETURNED),
            (14008, 0, Decimal('40'), 3, RequestStatus.COMPLETED),
            (14008, 0, Decimal('99'), 4, RequestStatus.REJECTED),
            (14008, 0, Decimal('99'), 5, RequestStatus.CANCELLED)]

    def setUp(self):
        # The advisory lock is Postgres's; there is no database here.
        patcher = mock.patch.object(reservations, 'lock')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_in_approval_reserves_completed_pays_rejected_releases(self):
        with _usage_rows(*self.ROWS):
            use = reservations.usage('OIL', DocumentKind.PO, [(14008, 0)])[(14008, 0)]
        self.assertEqual((use['reserved'], use['paid'], use['requests']), (Decimal('25'), Decimal('40'), 5))

    def test_a_po_also_loses_what_oms_has_paid_a_bill_does_not(self):
        use = {'reserved': Decimal('25'), 'paid': Decimal('40')}
        # SAP never sees an advance against a PO (it posts on account) ...
        self.assertEqual(reservations.available(DocumentKind.PO, '100', use), Decimal('35'))
        # ... but a bill's open amount in SAP already fell when OMS paid it.
        self.assertEqual(reservations.available(DocumentKind.BILL, '100', use), Decimal('75'))

    def test_the_request_being_edited_does_not_hold_against_itself(self):
        with _usage_rows(*self.ROWS):
            use = reservations.usage('OIL', DocumentKind.PO, [(14008, 0)], exclude_request=1)[(14008, 0)]
        self.assertEqual(use['reserved'], Decimal('15'))

    def test_refuses_a_line_beyond_what_is_left(self):
        line_ = {'kind': DocumentKind.PO, 'sap_doc_entry': 14008, 'sap_line': 0, 'sap_doc_num': '126226600',
                 'open_amount': Decimal('100'), 'amount': Decimal('36')}
        with _usage_rows(*self.ROWS):
            found = reservations.problems('OIL', [line_])
        self.assertEqual(len(found), 1)
        self.assertIn('Purchase order 126226600: only 35 is available', found[0])
        with _usage_rows(*self.ROWS):
            self.assertEqual(reservations.problems('OIL', [{**line_, 'amount': Decimal('35')}]), [])

    def test_a_used_up_document_is_not_offered_again(self):
        rows = [{'doc_entry': 14008, 'open_amount': '65'}, {'doc_entry': 14009, 'open_amount': '50'}]
        with _usage_rows(*self.ROWS):
            offered = reservations.annotate('OIL', DocumentKind.PO, rows, key=lambda r: (r['doc_entry'], 0),
                                            open_field='open_amount')
        self.assertEqual([r['doc_entry'] for r in offered], [14009])
        self.assertEqual(offered[0]['oms'], {'tracked': True, 'reserved': '0', 'paid': '0', 'unadjusted': '0',
                                             'available': '50', 'requests': 0})

    def test_any_po_is_its_total_less_what_oms_paid_goods_received_ignored(self):
        # Old or new: SAP's received (400, the GRPOs) is never deducted. Open =
        # 500 total less 40 paid via OMS = 460; available = that less 25 held.
        for created in ('2025-07-26', '2026-10-08'):
            rows = [{'doc_entry': 14008, 'open_amount': '100', 'doc_total': '500', 'received_amount': '400',
                     'created_on': created}]
            with _usage_rows(*self.ROWS), self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
                offered = reservations.annotate('OIL', DocumentKind.PO, rows, key=lambda r: (r['doc_entry'], 0),
                                                open_field='open_amount', total_field='doc_total',
                                                paid_field='received_amount')
            self.assertEqual((offered[0]['oms']['tracked'], offered[0]['oms']['available']), (True, '435'))
            self.assertEqual((offered[0]['open_amount'], offered[0]['received_amount']), ('460', '40'))

    def test_a_new_bill_is_its_total_less_what_oms_paid(self):
        # Created on/after the 8 Oct cut-off: SAP's paid-to-date (400) ignored.
        # Open = 500 total less 40 paid via OMS = 460; available = less 25 held.
        rows = [{'doc_entry': 14008, 'balance_due': '100', 'doc_total': '500', 'paid_to_date': '400',
                 'created_on': '2026-10-09'}]
        with _usage_rows(*self.ROWS), self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
            offered = reservations.annotate('OIL', DocumentKind.BILL, rows, key=lambda r: (r['doc_entry'], 0),
                                            open_field='balance_due', total_field='doc_total',
                                            paid_field='paid_to_date')
        self.assertEqual((offered[0]['oms']['tracked'], offered[0]['oms']['available']), (True, '435'))
        self.assertEqual((offered[0]['balance_due'], offered[0]['paid_to_date']), ('460', '40'))

    def test_an_older_bill_keeps_sap_real_balance_less_what_oms_holds(self):
        # Created before the cut-off: paid in SAP before OMS. SAP's balance (100)
        # less 25 held = 75. Figures not rewritten.
        rows = [{'doc_entry': 14008, 'balance_due': '100', 'doc_total': '500', 'paid_to_date': '400',
                 'created_on': '2026-09-21'}]
        with _usage_rows(*self.ROWS), self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
            offered = reservations.annotate('OIL', DocumentKind.BILL, rows, key=lambda r: (r['doc_entry'], 0),
                                            open_field='balance_due', total_field='doc_total',
                                            paid_field='paid_to_date')
        self.assertEqual((offered[0]['oms']['tracked'], offered[0]['oms']['available']), (False, '75'))
        self.assertEqual((offered[0]['balance_due'], offered[0]['paid_to_date']), ('100', '400'))

    def test_a_save_against_an_older_bill_is_held_to_sap_balance(self):
        line_ = {'kind': DocumentKind.BILL, 'sap_doc_entry': 14008, 'sap_line': 0, 'sap_doc_num': '126226600',
                 'open_amount': Decimal('100'), 'amount': Decimal('76')}
        facts = {14008: {'created_on': date(2025, 7, 26), 'doc_total': '500'}}
        with _usage_rows(*self.ROWS, facts=facts), self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
            found = reservations.problems('OIL', [line_])
        self.assertIn('only 75 is available', found[0])

    def test_a_save_from_the_cut_off_is_held_to_total_less_paid_and_held(self):
        line_ = {'kind': DocumentKind.BILL, 'sap_doc_entry': 14008, 'sap_line': 0, 'sap_doc_num': '126226600',
                 'open_amount': Decimal('460'), 'amount': Decimal('436')}
        facts = {14008: {'created_on': '2026-10-09', 'doc_total': '500'}}
        with _usage_rows(*self.ROWS, facts=facts), self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
            found = reservations.problems('OIL', [line_])
            self.assertEqual(reservations.problems('OIL', [{**line_, 'amount': Decimal('435')}]), [])
        self.assertEqual(len(found), 1)
        self.assertIn('only 435 is available — of its total 500, OMS has paid 40 and other requests hold 25',
                      found[0])

    def test_a_save_against_an_older_po_is_held_to_its_total(self):
        line_ = {'kind': DocumentKind.PO, 'sap_doc_entry': 14008, 'sap_line': 0, 'sap_doc_num': '126226600',
                 'open_amount': Decimal('460'), 'amount': Decimal('436')}
        facts = {14008: {'created_on': date(2025, 7, 26), 'doc_total': '500'}}
        with _usage_rows(*self.ROWS, facts=facts), self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
            found = reservations.problems('OIL', [line_])
        self.assertIn('only 435 is available — of its total 500', found[0])

    def test_what_oms_alone_tracks(self):
        with self.settings(ADVANCE_PAYMENT_TRACK_FROM='2026-10-08'):
            self.assertTrue(reservations.tracked(DocumentKind.PO, '2020-01-01'))    # every PO
            self.assertTrue(reservations.tracked(DocumentKind.BILL, '2026-10-08'))   # a new bill
            self.assertFalse(reservations.tracked(DocumentKind.BILL, '2026-10-07'))  # an older bill
            self.assertFalse(reservations.tracked(DocumentKind.BILL, None))          # unknown: SAP's balance
            self.assertFalse(reservations.tracked(DocumentKind.LEDGER, '2026-10-09'))
            self.assertTrue(reservations.since_cut_off(None))                     # the notice: shown when unknown
            self.assertFalse(reservations.since_cut_off('2026-10-05'))

    def test_a_save_is_refused_when_the_last_rupee_is_already_held(self):
        cleaned = request_service.CleanRequest(
            fields={'company': 'OIL'},
            documents=[{'kind': DocumentKind.PO, 'sap_doc_entry': 14008, 'sap_line': 0,
                        'open_amount': Decimal('100'), 'amount': Decimal('50')}])
        with _usage_rows(*self.ROWS), self.assertRaisesRegex(request_service.RequestInvalid, 'only 35'):
            request_service._reserve(cleaned)


class PaymentReturnsToTheCreator(SimpleTestCase):
    def test_payment_may_return_audit_and_final_may_not(self):
        self.assertIn(StageRole.PAYMENT, flow_service.RETURN_TO_CREATOR_ROLES)
        self.assertNotIn(StageRole.AUDIT, flow_service.RETURN_TO_CREATOR_ROLES)
        self.assertNotIn(StageRole.FINAL, flow_service.RETURN_TO_CREATOR_ROLES)

    def test_the_payment_desk_is_offered_the_button(self):
        advance = SimpleNamespace(created_by_id=9,
                                  flow=SimpleNamespace(current_role=StageRole.PAYMENT, status='PENDING'))
        with mock.patch.object(flow_service, 'is_actor', return_value=True), \
                mock.patch.object(flow_service, 'creator_may_change', return_value=False), \
                mock.patch.object(flow_service, 'payment_users', return_value=set()):
            can = flow_service.abilities(advance, SimpleNamespace(pk=1))
        self.assertTrue(can['return_to_creator'])
        self.assertTrue(can['edit_payout'])


class ARefundsBranch(SimpleTestCase):
    """Before posting: a refund's items must still be open, and on one branch."""

    def _advance(self, *docs):
        advance = advance_of('CUSTOMER', documents=list(docs), amount='20', partner='CUSTA000846')
        return advance

    def _item(self, entry, line_no=0, num=None):
        return SimpleNamespace(kind=DocumentKind.LEDGER, sap_doc_entry=entry, sap_line=line_no,
                               sap_doc_num=num or str(entry), amount=Decimal('1'))

    def test_takes_the_branch_of_its_journal_lines(self):
        items = {(210827, 1): {'bpl_id': 2, 'open': '100'}, (74506, 0): {'bpl_id': 2, 'open': '80'}}
        with mock.patch.object(voucher_service.sap_service, 'ledger_items', return_value=items):
            self.assertEqual(voucher_service._branch(self._advance(self._item(210827, 1), self._item(74506))),
                             (2, ''))

    def test_refuses_an_item_no_longer_open(self):
        with mock.patch.object(voucher_service.sap_service, 'ledger_items', return_value={}):
            bpl, problem = voucher_service._branch(self._advance(self._item(210827, 1, '626210827')))
        self.assertIsNone(bpl)
        self.assertIn('626210827 no longer open', problem)

    def test_refuses_items_on_two_branches(self):
        items = {(210827, 1): {'bpl_id': 1, 'open': '100'}, (74506, 0): {'bpl_id': 2, 'open': '80'}}
        with mock.patch.object(voucher_service.sap_service, 'ledger_items', return_value=items):
            _bpl, problem = voucher_service._branch(self._advance(self._item(210827, 1), self._item(74506)))
        self.assertIn('different SAP branches', problem)


ZERO_ = Decimal('0')


class APoAdvanceOnceAdjusted(SimpleTestCase):
    """An OMS advance holds its PO only while SAP still has it on account."""

    ROWS = [(14008, 0, Decimal('10'), 3, RequestStatus.COMPLETED)]

    def test_counts_only_what_is_still_on_account(self):
        # PO 100, 50 received (SAP open 50), the 10 advance set off against the bill.
        with _usage_rows(*self.ROWS, unadjusted=lambda company, lines: [Decimal('0')]):
            use = reservations.usage('OIL', DocumentKind.PO, [(14008, 0)])[(14008, 0)]
        self.assertEqual(reservations.available(DocumentKind.PO, '50', use), Decimal('50'))
        # Not yet set off: it still holds its 10.
        with _usage_rows(*self.ROWS):
            use = reservations.usage('OIL', DocumentKind.PO, [(14008, 0)])[(14008, 0)]
        self.assertEqual(reservations.available(DocumentKind.PO, '50', use), Decimal('40'))

    def _unadjusted(self, lines, total, balances):
        vouchers = mock.Mock()
        vouchers.filter.return_value.values_list.return_value = [(3, 29440)]
        totals = mock.Mock()
        totals.filter.return_value.values_list.return_value = [(3, Decimal(total))]
        with mock.patch.object(reservations, 'SapVoucher', mock.Mock(objects=vouchers)), \
                mock.patch.object(reservations, 'AdvanceRequest', mock.Mock(objects=totals)), \
                mock.patch('advance_payment.services.sap.payments_unadjusted', **balances):
            return reservations.unadjusted('OIL', lines)

    def test_a_payment_over_two_pos_shares_its_balance_by_what_each_paid(self):
        lines = [((14008, 0), 3, Decimal('60')), ((14009, 0), 3, Decimal('40'))]
        self.assertEqual(self._unadjusted(lines, '100', {'return_value': {29440: Decimal('50')}}),
                         [Decimal('30.00'), Decimal('20.00')])

    def test_an_unreadable_sap_counts_the_whole_payment(self):
        from advance_payment.services import sap as sap_service

        lines = [((14008, 0), 3, Decimal('60'))]
        self.assertEqual(self._unadjusted(lines, '60', {'side_effect': sap_service.SapUnavailable('down')}),
                         [Decimal('60')])


def _po_line(pk, entry, amount, open_when_raised, num=None):
    return SimpleNamespace(pk=pk, kind=DocumentKind.PO, sap_doc_entry=entry, sap_line=0,
                           sap_doc_num=num or str(entry), amount=Decimal(amount),
                           open_amount=Decimal(open_when_raised))


class CheckedAgainstSapNow(SimpleTestCase):
    def _check(self, docs, live, used=None):
        advance = SimpleNamespace(pk=9, company='OIL', partner_code='VENDA000101',
                                  documents=mock.Mock(all=mock.Mock(return_value=docs)))
        with mock.patch('advance_payment.services.sap.live_documents', return_value=live), \
                mock.patch.object(reservations, 'usage', return_value=used or {}):
            return reservations.live_check(advance)

    def test_a_po_cut_below_what_is_paid_is_flagged(self):
        [row] = self._check([_po_line(1, 14008, '60', '100', '126226600')],
                            {14008: {'doc_num': 126226600, 'open': '50', 'status': 'OPEN'}})
        self.assertFalse(row['ok'])
        self.assertTrue(row['changed'])
        self.assertIn('pays 60, but only 50 is left', row['message'])
        self.assertIn('was 100 when raised', row['message'])

    def test_still_fits_after_a_change_is_fine_but_shown_as_changed(self):
        [row] = self._check([_po_line(1, 14008, '40', '100')],
                            {14008: {'doc_num': 1, 'open': '50', 'status': 'OPEN'}})
        self.assertTrue(row['ok'])
        self.assertTrue(row['changed'])

    def test_other_requests_holding_part_of_it_count(self):
        [row] = self._check([_po_line(1, 14008, '40', '100')],
                            {14008: {'doc_num': 1, 'open': '50', 'status': 'OPEN'}},
                            used={(14008, 0): {'reserved': Decimal('15'), 'paid': ZERO_, 'unadjusted': ZERO_}})
        self.assertFalse(row['ok'])
        self.assertEqual((row['available_now'], row['held_by_others']), ('35', '15'))

    def test_a_cancelled_or_vanished_po_is_flagged(self):
        rows = self._check([_po_line(1, 14008, '10', '100'), _po_line(2, 14009, '10', '100')],
                           {14008: {'doc_num': 1, 'open': '100', 'status': 'CANCELLED'}})
        self.assertIn('is cancelled in SAP', rows[0]['message'])
        self.assertIn('no longer exists in SAP', rows[1]['message'])


class FinalChecksSapFirst(SimpleTestCase):
    def _post(self, live_rows, existing=None):
        advance = mock.Mock(pk=7, request_no='AP-2026-0007', company='OIL')
        advance.vouchers.filter.return_value.count.return_value = 0
        advance.vouchers.aggregate.return_value = {'n': None}
        with mock.patch.object(voucher_service.AdvanceRequest.objects, 'get', return_value=advance), \
                mock.patch.object(voucher_service.Payout.objects, 'filter') as payouts, \
                mock.patch.object(voucher_service, '_branch', return_value=(1, '')), \
                mock.patch.object(voucher_service, 'build_payload', return_value={'x': 1}), \
                mock.patch.object(voucher_service, '_company_db', return_value='TEST_OIL'), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_series', return_value=2601), \
                mock.patch.object(voucher_service.sap_service, 'outgoing_payment_by_memo', return_value=existing), \
                mock.patch.object(reservations, 'live_check', return_value=live_rows) as live, \
                mock.patch('payments.sap_client.request') as sl, \
                mock.patch.object(voucher_service.SapVoucher.objects, 'create'), \
                mock.patch.object(voucher_service, '_record', create=True):
            payouts.return_value.first.return_value = SimpleNamespace(tds_amount=Decimal('0'))
            try:
                outcome = voucher_service.post(advance, user=SimpleNamespace(pk=1))
            except Exception:  # noqa: BLE001 - the adopted path goes on to record; not under test here
                outcome = None
        return outcome, live, sl

    def test_will_not_post_a_line_sap_no_longer_has_room_for(self):
        outcome, _live, sl = self._post([{'ok': False, 'message': 'Purchase order 1: this request pays 60, '
                                                                   'but only 50 is left.'}])
        self.assertFalse(outcome.ok)
        self.assertIn('SAP has changed since this request was raised', outcome.message)
        self.assertIn('Send it back to Payment', outcome.message)
        sl.assert_not_called()

    def test_an_adopted_payment_is_not_checked_again(self):
        _outcome, live, sl = self._post([], existing={'doc_entry': 1, 'doc_num': 2})
        live.assert_not_called()
        sl.assert_not_called()
