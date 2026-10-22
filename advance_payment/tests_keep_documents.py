"""Payment or Audit pays only some of a request's documents (2026-10-22). No database: the
flow, the documents and the payout are stood in for."""
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.models import LedgerDirection, LogAction, RequestType, StageRole
from advance_payment.services import flow as flow_service


def _doc(pk, amount, number, direction=''):
    return SimpleNamespace(pk=pk, amount=Decimal(amount), sap_doc_num=number, sap_doc_entry=pk, kind='BILL',
                           vendor_ref=f'REF{pk}', direction=direction)


class _Docs(list):
    def all(self):
        return self

    def filter(self, pk__in):
        gone = self.deleted

        class _Q:
            def delete(self_inner):
                gone.extend(pk__in)
        return _Q()


def _advance(docs, request_type=RequestType.VENDOR):
    documents = _Docs(docs)
    documents.deleted = []
    return SimpleNamespace(request_type=request_type, documents=documents, amount=sum(d.amount for d in docs),
                           save=mock.Mock(), request_no='AP-1')


class KeepingSomeDocuments(SimpleTestCase):
    def run_keep(self, advance, keep, role=StageRole.PAYMENT, payout=None, remarks='Not due yet'):
        flow = SimpleNamespace(current_role=role, request=advance, current_stage=SimpleNamespace(name='Payment Approval'))
        payouts = mock.Mock()
        payouts.filter.return_value.first.return_value = payout
        with mock.patch.object(flow_service, '_acting', return_value=flow), \
                mock.patch.object(flow_service.Payout, 'objects', payouts), \
                mock.patch.object(flow_service, 'log') as log, \
                mock.patch.object(flow_service, '_save'), \
                mock.patch.object(flow_service.notify, 'documents_taken_off') as told:
            # Under its transaction: there is no database here.
            flow_service.keep_documents.__wrapped__(advance, keep, user=SimpleNamespace(pk=1), remarks=remarks)
        return log, told

    def test_the_rest_are_taken_off_the_amount_follows_and_the_creator_is_told(self):
        advance = _advance([_doc(1, '700630', 609264380), _doc(2, '240376', 609264386)])
        line = SimpleNamespace(method='RTGS', amount=Decimal('941006'), save=mock.Mock())
        payout = SimpleNamespace(tds_code='', tds_rate=None, tds_amount=Decimal('0'),
                                 lines=SimpleNamespace(all=lambda: [line]), save=mock.Mock())
        log, told = self.run_keep(advance, [1], payout=payout)
        self.assertEqual(advance.documents.deleted, [2])
        self.assertEqual(advance.amount, Decimal('700630'))
        self.assertEqual(line.amount, Decimal('700630'))  # the one method follows
        self.assertEqual(log.call_args.args[1], LogAction.EDITED)
        data = log.call_args.kwargs['data']
        self.assertEqual(data['documents_taken_off'][0]['number'], 609264386)
        self.assertEqual(data['amount'], {'old': '941006.00', 'new': '700630.00'})
        self.assertEqual(told.call_args.args[1][0]['vendor_ref'], 'REF2')

    def test_tds_is_taken_again_at_its_rate(self):
        advance = _advance([_doc(1, '100000', 1), _doc(2, '50000', 2)])
        line = SimpleNamespace(method='NEFT', amount=Decimal('147000'), save=mock.Mock())
        payout = SimpleNamespace(tds_code='C194', tds_rate=Decimal('2'), tds_amount=Decimal('3000'),
                                 lines=SimpleNamespace(all=lambda: [line]), save=mock.Mock())
        self.run_keep(advance, [1], payout=payout)
        self.assertEqual(payout.tds_amount, Decimal('2000'))
        self.assertEqual(line.amount, Decimal('98000'))

    def test_audit_cannot_leave_several_methods_not_adding_up(self):
        advance = _advance([_doc(1, '100', 1), _doc(2, '50', 2)])
        lines = [SimpleNamespace(method='NEFT', amount=Decimal('100')), SimpleNamespace(method='CASH', amount=Decimal('50'))]
        payout = SimpleNamespace(tds_code='', tds_rate=None, tds_amount=Decimal('0'),
                                 lines=SimpleNamespace(all=lambda: lines))
        with self.assertRaises(flow_service.FlowError):
            self.run_keep(advance, [1], role=StageRole.AUDIT, payout=payout)
        self.assertEqual(advance.documents.deleted, [])

    def test_refused_without_a_reason_nothing_untick_everything_untick_or_elsewhere(self):
        docs = [_doc(1, '100', 1), _doc(2, '50', 2)]
        for keep, kwargs in (([1], {'remarks': ''}), ([1, 2], {}), ([], {}), ([1], {'role': StageRole.FINAL}),
                             ([9], {})):
            with self.subTest(keep=keep, **kwargs), self.assertRaises(flow_service.FlowError):
                self.run_keep(_advance(docs), keep, **kwargs)

    def test_a_ledger_item_owed_by_the_partner_counts_against_the_rest(self):
        advance = _advance([_doc(1, '500', 1, LedgerDirection.CREDIT), _doc(2, '100', 2, LedgerDirection.DEBIT),
                            _doc(3, '50', 3, LedgerDirection.CREDIT)])
        self.run_keep(advance, [1, 2])
        self.assertEqual(advance.amount, Decimal('400'))
