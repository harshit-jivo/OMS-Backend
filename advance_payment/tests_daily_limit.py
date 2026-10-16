"""The Factory daily limit: Ziyaul's cap on a day's Factory payment requests.

Pinned (user, 2026-10-15/16): what counts (Factory / FACT_COM, or a factory
purpose in any department; never Oil Purchase; Oil and Beverages each on its
own); a request past the limit is refused when raised, and an edit that adds
to it; the last limit carries forward; a rejected or withdrawn request gives
its amount back; only Ziyaul sets it.
"""
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from advance_payment.models import AdvanceRequest, FactoryDailyLimit, RequestStatus
from advance_payment.services import clock
from advance_payment.services import daily_limit
from advance_payment.services import requests as request_service
from workflow.tests.factories import make_user

TODAY = date(2026, 10, 16)


def at(day, hour=11):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour), clock.INDIA)


def fields(**overrides):
    out = {'company': 'OIL', 'request_type': 'VENDOR', 'payment_against': 'AGAINST_BILL',
           'partner_code': 'VENDA000001', 'partner_name': 'Spares Co', 'amount': Decimal('40000'),
           'payment_date': TODAY, 'budget_code': 'Factory', 'purpose_code': 'CONSUMABLES',
           'remarks': 'Spares', 'owner_label': 'Plant'}
    out.update(overrides)
    return out


@override_settings(ADVANCE_PAYMENT_FACTORY_LIMIT=True)
class Base(TestCase):
    def setUp(self):
        self.ziyaul = make_user('ziyaul')
        self.creator = make_user('dl-creator', extra_pages=['Advance_Payment'])
        self.n = 0
        from django.core.cache import cache

        cache.clear()  # SAP's due bills are cached between requests
        patcher = mock.patch.object(clock, 'today', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def raised(self, day=TODAY, status=RequestStatus.IN_APPROVAL, **overrides):
        self.n += 1
        advance = AdvanceRequest.objects.create(request_no=f'AP-DL-{self.n:04d}', created_by=self.creator,
                                                status=status, **fields(**overrides))
        AdvanceRequest.objects.filter(pk=advance.pk).update(created_on=at(day))
        advance.refresh_from_db()
        return advance

    def limit(self, amount, company='OIL', day=TODAY):
        return FactoryDailyLimit.objects.create(company=company, day=day, amount=Decimal(amount),
                                                set_by=self.ziyaul)


class WhatCounts(Base):
    def test_factory_departments_and_factory_purposes_but_never_oil(self):
        self.assertTrue(daily_limit.counts('OIL', 'Factory', 'CONSUMABLES'))
        self.assertTrue(daily_limit.counts('BEVERAGES', 'FACT_COM', 'REPAIRS'))
        # A factory purpose counts in any department — packing material bought under BackOff.
        for purpose in ('PACKING_MATERIAL', 'RAW_MATERIAL', 'SEMI_FINISHED', 'FA_CONSUMABLES'):
            self.assertTrue(daily_limit.counts('OIL', 'BackOff', purpose), purpose)
        # Never an oil purchase, even under Factory; never Mart; not other spending.
        self.assertFalse(daily_limit.counts('OIL', 'Factory', 'OIL_PURCHASE'))
        self.assertFalse(daily_limit.counts('MART', 'Factory', 'PACKING_MATERIAL'))
        self.assertFalse(daily_limit.counts('OIL', 'Sales', 'ADVERTISING'))

    def test_uses_the_days_live_requests_in_that_company_only(self):
        self.raised(amount=Decimal('10000'))
        self.raised(amount=Decimal('5000'), budget_code='BackOff', purpose_code='PACKING_MATERIAL')
        self.raised(amount=Decimal('999999'), purpose_code='OIL_PURCHASE')            # oil: never
        self.raised(amount=Decimal('7000'), status=RequestStatus.REJECTED)             # given back
        self.raised(amount=Decimal('8000'), status=RequestStatus.CANCELLED)            # given back
        self.raised(amount=Decimal('3000'), status=RequestStatus.RETURNED)             # still alive
        self.raised(amount=Decimal('2000'), company='BEVERAGES')                       # its own limit
        self.raised(amount=Decimal('4000'), day=date(2026, 10, 15))                    # yesterday's
        self.assertEqual(daily_limit.used_on('OIL', TODAY), Decimal('18000'))
        self.assertEqual(daily_limit.used_on('BEVERAGES', TODAY), Decimal('2000'))


class RaisingOne(Base):
    def test_is_refused_past_the_limit_and_says_by_how_much(self):
        self.limit('100000')
        self.raised(amount=Decimal('70000'))
        self.assertEqual(daily_limit.check_new(fields(amount=Decimal('30000'))), [])
        found = daily_limit.check_new(fields(amount=Decimal('40000')))
        self.assertEqual(len(found), 1)
        self.assertIn('limit ₹1,00,000.00, already raised ₹70,000.00, left ₹30,000.00', found[0])
        self.assertIn('this request is ₹40,000.00, ₹10,000.00 over', found[0])

    def test_through_the_real_save(self):
        self.limit('50000')
        cleaned = request_service.CleanRequest(fields=fields(amount=Decimal('60000')))
        with self.assertRaises(request_service.RequestInvalid):
            request_service.create(cleaned, user=self.creator)
        self.assertFalse(AdvanceRequest.objects.exists())
        request_service.create(request_service.CleanRequest(fields=fields(amount=Decimal('50000'))),
                               user=self.creator)
        self.assertEqual(AdvanceRequest.objects.count(), 1)

    def test_oil_purchases_and_other_spending_are_never_held_back(self):
        self.limit('1000')
        self.assertEqual(daily_limit.check_new(fields(amount=Decimal('5000000'), purpose_code='OIL_PURCHASE')), [])
        self.assertEqual(daily_limit.check_new(fields(amount=Decimal('5000000'), budget_code='Sales',
                                                      purpose_code='ADVERTISING')), [])

    def test_yesterdays_limit_carries_forward_and_none_until_one_is_set(self):
        self.assertEqual(daily_limit.check_new(fields(amount=Decimal('9999999'))), [])
        self.limit('20000', day=date(2026, 10, 14))
        found = daily_limit.check_new(fields(amount=Decimal('25000')))
        self.assertTrue(found)
        self.assertEqual(daily_limit.limit_for('OIL', TODAY).day, date(2026, 10, 14))

    def test_a_rejected_request_gives_its_amount_back(self):
        self.limit('50000')
        first = self.raised(amount=Decimal('50000'))
        self.assertTrue(daily_limit.check_new(fields(amount=Decimal('1000'))))
        AdvanceRequest.objects.filter(pk=first.pk).update(status=RequestStatus.REJECTED)
        self.assertEqual(daily_limit.check_new(fields(amount=Decimal('1000'))), [])


class EditingOne(Base):
    def test_an_edit_adding_to_it_is_checked_against_the_day_it_was_raised(self):
        self.limit('100000', day=date(2026, 10, 15))
        mine = self.raised(amount=Decimal('60000'), day=date(2026, 10, 15))
        self.raised(amount=Decimal('30000'), day=date(2026, 10, 15))
        self.assertEqual(daily_limit.check_edit(mine, fields(amount=Decimal('70000'))), [])
        self.assertTrue(daily_limit.check_edit(mine, fields(amount=Decimal('80000'))))

    def test_an_unchanged_or_smaller_one_is_never_refused_after_the_limit_is_lowered(self):
        mine = self.raised(amount=Decimal('60000'))
        self.limit('10000')
        self.assertEqual(daily_limit.check_edit(mine, fields(amount=Decimal('60000'))), [])
        self.assertEqual(daily_limit.check_edit(mine, fields(amount=Decimal('50000'))), [])

    def test_moving_into_factory_is_checked(self):
        self.limit('10000')
        mine = self.raised(amount=Decimal('20000'), budget_code='Sales', purpose_code='ADVERTISING')
        self.assertTrue(daily_limit.check_edit(mine, fields(amount=Decimal('20000'))))


class SettingIt(Base):
    def test_only_ziyaul_sets_it_and_every_change_is_kept(self):
        with self.assertRaises(daily_limit.LimitInvalid):
            daily_limit.set_limit('OIL', '100000', user=self.creator)
        daily_limit.set_limit('OIL', '100000', user=self.ziyaul)
        row = daily_limit.set_limit('OIL', '150000', user=self.ziyaul)
        self.assertEqual(row.amount, Decimal('150000'))
        self.assertEqual(FactoryDailyLimit.objects.count(), 1)
        self.assertEqual([(c.old_amount, c.new_amount) for c in row.changes.order_by('changed_on')],
                         [(None, Decimal('100000')), (Decimal('100000'), Decimal('150000'))])

    def test_refuses_a_past_day_mart_and_nonsense(self):
        for company, amount, day in (('OIL', '1000', date(2026, 10, 15)), ('MART', '1000', None),
                                     ('OIL', '0', None), ('OIL', 'lots', None)):
            with self.subTest(company=company, amount=amount), self.assertRaises(daily_limit.LimitInvalid):
                daily_limit.set_limit(company, amount, user=self.ziyaul, day=day)
        # A later day may be set ahead.
        daily_limit.set_limit('OIL', '5000', user=self.ziyaul, day=date(2026, 10, 17))


class TheScreen(Base):
    def test_reads_for_anyone_and_saves_for_ziyaul_alone(self):
        self.limit('100000', day=date(2026, 10, 15))
        self.raised(amount=Decimal('25000'))
        client = APIClient()
        client.force_authenticate(self.creator)
        with mock.patch('advance_payment.services.sap.factory_due_bills',
                        return_value={'bills': 3, 'open': '500000', 'packing': '200000'}):
            data = client.get('/api/advance-payments/factory-limit/', {'company': 'OIL'}).json()['data']
        self.assertEqual(data['limit']['amount'], '100000.00')
        self.assertTrue(data['limit']['carried'])
        self.assertEqual((data['used'], data['left']), ('25000.00', '75000.00'))
        self.assertEqual([r['request_no'] for r in data['requests']], ['AP-DL-0001'])
        self.assertFalse(data['can_set'])
        self.assertEqual(data['open_bills']['open'], '500000')
        self.assertEqual(client.put('/api/advance-payments/factory-limit/', {'company': 'OIL', 'amount': '1'},
                                    format='json').status_code, 403)
        client.force_authenticate(self.ziyaul)
        saved = client.put('/api/advance-payments/factory-limit/', {'company': 'OIL', 'amount': '200000'},
                           format='json')
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['data']['limit']['amount'], '200000.00')
        self.assertFalse(saved.json()['data']['limit']['carried'])
        self.assertTrue(saved.json()['data']['can_set'])


class SwitchedOff(Base):
    """Off for now (user, 2026-10-16): nothing is refused, whatever the limit."""

    @override_settings(ADVANCE_PAYMENT_FACTORY_LIMIT=False)
    def test_refuses_nothing(self):
        self.limit('1000')
        mine = self.raised(amount=Decimal('5000'))
        self.assertEqual(daily_limit.check_new(fields(amount=Decimal('900000'))), [])
        self.assertEqual(daily_limit.check_edit(mine, fields(amount=Decimal('900000'))), [])
        self.assertFalse(daily_limit.summary('OIL', user=self.ziyaul)['enabled'])


class TheDueBills(Base):
    """The page's SAP figure: only bills due today or before, by vendor (user, 2026-10-16)."""

    def test_by_vendor_largest_first_each_with_its_bills(self):
        from datetime import datetime as dt

        from advance_payment.services import sap

        rows = [
            {'doc_entry': 1, 'doc_num': 101, 'card_code': 'V1', 'card_name': 'Tins', 'party_ref': 'T/1',
             'doc_date': dt(2026, 9, 1), 'due_date': dt(2026, 10, 1), 'doc_total': '50000', 'open': '40000',
             'packing': '40000'},
            {'doc_entry': 2, 'doc_num': 102, 'card_code': 'V2', 'card_name': 'Spares', 'party_ref': '',
             'doc_date': dt(2026, 9, 20), 'due_date': dt(2026, 10, 16), 'doc_total': '90000', 'open': '90000',
             'packing': '0'},
            {'doc_entry': 3, 'doc_num': 103, 'card_code': 'V1', 'card_name': 'Tins', 'party_ref': '',
             'doc_date': dt(2026, 8, 1), 'due_date': dt(2026, 9, 1), 'doc_total': '30000', 'open': '30000',
             'packing': '30000'},
        ]
        with mock.patch.object(sap, '_schema', return_value='S'), \
                mock.patch.object(sap, '_run', return_value=rows) as run:
            out = sap.factory_due_bills('OIL', today=TODAY)
        self.assertIn('"DocDueDate" <= ?', run.call_args.args[1])
        self.assertEqual(run.call_args.args[2][0], TODAY)
        self.assertEqual((out['bills'], out['open'], out['packing']), (3, '160000', '70000'))
        self.assertEqual([(v['card_code'], v['amount']) for v in out['vendors']], [('V2', '90000'), ('V1', '70000')])
        tins = out['vendors'][1]
        self.assertEqual([(b['doc_num'], b['days_overdue']) for b in tins['bills']], [(103, 45), (101, 15)])

    def test_the_page_shows_the_totals_and_the_bills_are_their_own_call(self):
        client = APIClient()
        client.force_authenticate(self.creator)
        found = {'bills': 1, 'open': '9', 'packing': '0', 'vendors': [{'card_code': 'V1', 'card_name': 'X',
                                                                      'amount': '9', 'bills': []}]}
        from django.core.cache import cache

        cache.clear()
        with mock.patch('advance_payment.services.sap.factory_due_bills', return_value=found):
            page = client.get('/api/advance-payments/factory-limit/', {'company': 'OIL'}).json()['data']
            bills = client.get('/api/advance-payments/factory-limit/bills/', {'company': 'OIL'}).json()['data']
        self.assertNotIn('vendors', page['open_bills'])
        self.assertEqual(bills['vendors'][0]['card_code'], 'V1')
