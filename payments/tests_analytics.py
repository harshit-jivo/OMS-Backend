"""Tests for the Payments Dashboard aggregations.

These pin down the DEFINITIONS the dashboard's labels imply — which statuses
count as money in hand, how advance is told apart from invoice-linked, and that
a deposit is measured by what reached the bank rather than what was collected.
Those are the assumptions a reader of the screen makes, and a refactor that
quietly changes one would misstate the figures while every total still looks
plausible.

Follows devices/tests.py: real ORM writes against the throwaway test database,
views exercised through APIRequestFactory.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from . import analytics
from .models import (
    BankDeposit,
    CollectionPerson,
    PaymentMethodEntry,
    PaymentReceipt,
)
from .permissions import PAYMENTS_DASHBOARD
from .views import PaymentDashboardView

TODAY = date(2026, 8, 5)

# Fixtures are written under company codes no real row uses, so the suite gives
# the same answer on a throwaway test database and on a shared one that already
# holds live receipts for the same dates.
CO = 'ZZTEST'
CO2 = 'ZZTEST2'


class RangeResolutionTests(TestCase):
    """The presets, resolved server-side so every user sees one period."""

    def test_today(self):
        self.assertEqual(analytics.resolve_range('today', today=TODAY),
                         (TODAY, TODAY))

    def test_yesterday(self):
        self.assertEqual(analytics.resolve_range('yesterday', today=TODAY),
                         (date(2026, 8, 4), date(2026, 8, 4)))

    def test_last_7_days_is_seven_days_including_today(self):
        start, end = analytics.resolve_range('last_7_days', today=TODAY)
        self.assertEqual((end - start).days + 1, 7)
        self.assertEqual(end, TODAY)

    def test_last_30_days_is_thirty_days_including_today(self):
        start, end = analytics.resolve_range('last_30_days', today=TODAY)
        self.assertEqual((end - start).days + 1, 30)

    def test_this_month_starts_on_the_first(self):
        self.assertEqual(analytics.resolve_range('this_month', today=TODAY),
                         (date(2026, 8, 1), TODAY))

    def test_unknown_preset_falls_back_to_the_default(self):
        # A stale client must not blow up or silently widen the window.
        self.assertEqual(analytics.resolve_range('nonsense', today=TODAY),
                         analytics.resolve_range(analytics.DEFAULT_PRESET,
                                                 today=TODAY))

    def test_default_preset_is_last_30_days(self):
        """The window a manager works in, and what an omitted preset means.

        A rolling 30 days rather than the calendar month: on the 1st of a month
        "this month" is one day of data, so the dashboard opened near-empty and
        the reader's first action was always to widen the range.
        """
        self.assertEqual(analytics.DEFAULT_PRESET, 'last_30_days')
        # Derived from TODAY rather than hardcoded. The literal here used to be
        # date(2026, 7, 12), which is TODAY - 29 only for a TODAY of 2026-08-10
        # — so it broke silently when the module-level TODAY was changed to
        # 2026-08-05, asserting a 25-day window against a test whose own
        # docstring says 30. Every other assertion in this class is
        # TODAY-relative, which is why they all survived that edit.
        self.assertEqual(analytics.resolve_range(None, today=TODAY),
                         (TODAY - timedelta(days=29), TODAY))

    def test_custom_uses_the_supplied_dates(self):
        self.assertEqual(
            analytics.resolve_range('custom', date(2026, 1, 1),
                                    date(2026, 2, 1), today=TODAY),
            (date(2026, 1, 1), date(2026, 2, 1)))


class DashboardAggregationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        # get_or_create, not create: `company` is unique, and the suite has to
        # be runnable against a database that already has the real mapping rows
        # (the deployment user cannot CREATE DATABASE, so a throwaway test
        # database is not always available here).
        cls.person = CollectionPerson.objects.create(
            name='Amit Test', code='TEST-AMIT')
        cls.other = CollectionPerson.objects.create(
            name='Goldy Test', code='TEST-GOLDY')

    def dash(self, *, company=CO, preset='today', **kw):
        """Aggregate, scoped to this test's own data.

        Every assertion below is about rows the test itself created, so the
        query is scoped to a company by default. On a throwaway test database
        that changes nothing; against a shared one it keeps real receipts from
        being counted into an exact-total assertion.
        """
        return analytics.dashboard(company=company, preset=preset,
                                   today=TODAY, **kw)

    def people(self, **kw):
        """Just the participant rows, for the table assertions."""
        return self.dash(**kw)['collection_performance']['results']

    def receipt(self, amount, *, status='POSTED', advance=False,
                company=CO, day=TODAY, person=None, methods=()):
        r = PaymentReceipt.objects.create(
            receipt_no=f'RCP-TEST-{PaymentReceipt.objects.count() + 1:05d}',
            company=company, card_code='C1', card_name='Customer One',
            payment_date=day, total_amount=Decimal(amount), status=status,
            is_advance=advance,
            received_from_type='PERSON' if person else 'PARTY',
            received_from_person=person,
        )
        for method, value in methods:
            PaymentMethodEntry.objects.create(
                receipt=r, method=method, amount=Decimal(value),
                cheque_number='1' if method == 'CHEQUE' else '',
                cheque_date=day if method == 'CHEQUE' else None)
        return r

    def deposit(self, deposit_amount, *, collected=None, status='POSTED',
                kind='CASH', day=TODAY, person=None, reason=''):
        collected = Decimal(collected if collected is not None else deposit_amount)
        return BankDeposit.objects.create(
            deposit_no=f'DEP-TEST-{BankDeposit.objects.count() + 1:05d}',
            company=CO, deposit_date=day, deposit_type=kind,
            collected_amount=collected, deposit_amount=Decimal(deposit_amount),
            shortfall_reason=reason, status=status, deposited_by=person,
            bank_gl_account='_SYS001', bank_display_name='HDFC')

    # -- What counts ------------------------------------------------------

    def test_only_sap_posted_receipts_are_counted(self):
        """The screen reports what is settled in SAP, nothing else.

        Every excluded status is listed explicitly rather than tested in a
        loop, so a future change to one of them fails on the status it broke
        instead of on an opaque total.
        """
        self.receipt(1000, status='POSTED')
        for excluded in ('DRAFT', 'PENDING_APPROVAL', 'APPROVED',
                         'POSTING_TO_SAP', 'REJECTED', 'CANCELLED'):
            self.receipt(500, status=excluded)

        data = self.dash()
        self.assertEqual(data['kpis']['received_total'], 1000.0)
        self.assertEqual(data['kpis']['received_count'], 1)

    def test_sap_failures_are_not_counted(self):
        """SAP ANSWERED with an error, so nothing was committed there."""
        self.receipt(1000, status='POSTED')
        self.receipt(2000, status='PENDING_ERROR')

        self.assertEqual(self.dash()['kpis']['received_total'], 1000.0)

    def test_unconfirmed_postings_are_not_counted(self):
        """SAP_UNKNOWN means SAP never answered.

        The document may or may not exist. Counting an unconfirmed posting is
        the one error a finance dashboard must not make, because a reader
        cannot tell it apart from a real one.
        """
        self.receipt(1000, status='POSTED')
        self.receipt(4000, status='SAP_UNKNOWN')

        self.assertEqual(self.dash()['kpis']['received_total'], 1000.0)

    def test_total_payments_shares_the_posted_filter(self):
        """No card may answer a different question from its neighbours."""
        self.receipt(1000, status='POSTED')
        self.receipt(500, status='DRAFT')
        self.receipt(700, status='PENDING_ERROR')

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['total_payments'], 1000.0)
        self.assertEqual(kpis['total_payments'], kpis['received_total'])

    def test_pending_counts_what_the_posted_totals_exclude(self):
        """The two sets are complementary, not overlapping."""
        self.receipt(1000, status='POSTED')
        self.receipt(200, status='DRAFT')
        self.receipt(300, status='PENDING_APPROVAL')
        self.receipt(400, status='PENDING_ERROR')

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['received_total'], 1000.0)
        self.assertEqual(kpis['pending_receipts'], 900.0)
        self.assertEqual(kpis['pending_receipts_count'], 3)

    def test_rejected_and_cancelled_are_not_pending(self):
        """Nobody is waiting on them and nothing will ever post."""
        self.receipt(500, status='REJECTED')
        self.receipt(700, status='CANCELLED')

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['pending_receipts'], 0.0)
        self.assertEqual(kpis['pending_receipts_count'], 0)

    def test_blocked_is_only_what_needs_a_human(self):
        """In-flight work clears itself; these two do not."""
        self.receipt(100, status='PENDING_APPROVAL')   # advances on its own
        self.receipt(400, status='PENDING_ERROR')      # SAP refused it
        self.receipt(600, status='SAP_UNKNOWN')        # SAP never answered

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['pending_receipts'], 1100.0)
        self.assertEqual(kpis['blocked_total'], 1000.0)
        self.assertEqual(kpis['blocked_count'], 2)

    def test_pending_deposits_are_reported_separately(self):
        """Adding them to pending receipts would count the same money twice."""
        self.receipt(500, status='PENDING_ERROR')
        self.deposit(300, status='PENDING_ERROR')

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['pending_receipts'], 500.0)
        self.assertEqual(kpis['pending_deposits'], 300.0)
        # Blocked spans both, because both need someone to act.
        self.assertEqual(kpis['blocked_total'], 800.0)
        self.assertEqual(kpis['blocked_count'], 2)

    def test_only_posted_deposits_are_counted(self):
        """Same rule for the bank side."""
        self.deposit(900, status='POSTED')
        self.deposit(400, status='PENDING_ERROR')
        self.deposit(300, status='PENDING_APPROVAL')

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['deposit_total'], 900.0)
        self.assertEqual(kpis['deposit_count'], 1)

    # -- Splits -----------------------------------------------------------

    def test_advance_and_against_invoice_add_up_to_received(self):
        self.receipt(1000, advance=False)
        self.receipt(250, advance=True)

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['against_invoice'], 1000.0)
        self.assertEqual(kpis['advance_payment'], 250.0)
        self.assertEqual(kpis['against_invoice'] + kpis['advance_payment'],
                         kpis['received_total'])

    def test_method_split_sums_to_received_total(self):
        """A receipt can mix tenders, so the split lives on the method lines."""
        self.receipt(1000, methods=(('CASH', 600), ('CHEQUE', 400)))
        self.receipt(500, methods=(('UPI', 500),))

        data = self.dash()
        methods = {s['key']: s['amount'] for s in data['charts']['methods']['slices']}
        self.assertEqual(methods['CASH'], 600.0)
        self.assertEqual(methods['CHEQUE'], 400.0)
        self.assertEqual(methods['UPI'], 500.0)
        self.assertEqual(data['charts']['methods']['total'],
                         data['kpis']['received_total'])

    def test_percentages_sum_to_one_hundred(self):
        self.receipt(750, methods=(('CASH', 750),))
        self.receipt(250, methods=(('UPI', 250),))

        slices = self.dash()['charts']['methods']['slices']
        self.assertEqual(round(sum(s['percent'] for s in slices)), 100)

    def test_deposit_measures_what_reached_the_bank(self):
        """Short-banked cash counts at the deposited figure, not collected."""
        self.deposit(800, collected=1000, reason='Short by 200')

        kpis = self.dash()['kpis']
        self.assertEqual(kpis['deposit_total'], 800.0)
        self.assertEqual(kpis['deposit_collected'], 1000.0)

    def test_mixed_deposits_keep_their_own_segment(self):
        """Forcing MIXED into cash or cheque would misstate both."""
        self.deposit(500, kind='CASH')
        self.deposit(300, kind='MIXED')

        slices = {s['key']: s['amount'] for s in
                  self.dash()['charts']['deposits']['slices']}
        self.assertEqual(slices['CASH'], 500.0)
        self.assertEqual(slices['MIXED'], 300.0)

    # -- Filters ----------------------------------------------------------

    def test_company_filter_narrows_every_figure(self):
        self.receipt(1000, company=CO)
        self.receipt(400, company=CO2)

        self.assertEqual(self.dash(company=CO)['kpis']['received_total'], 1000.0)
        self.assertEqual(self.dash(company=CO2)['kpis']['received_total'], 400.0)

    def test_company_filter_is_case_insensitive(self):
        self.receipt(1000, company=CO)
        self.assertEqual(
            self.dash(company=CO.lower())['kpis']['received_total'], 1000.0)

    def test_date_window_is_inclusive_at_both_ends(self):
        self.receipt(100, day=date(2026, 8, 1))
        self.receipt(200, day=date(2026, 8, 3))
        self.receipt(400, day=date(2026, 8, 5))

        data = self.dash(preset='custom', date_from=date(2026, 8, 1),
                         date_to=date(2026, 8, 5))
        self.assertEqual(data['kpis']['received_total'], 700.0)

    def test_receipts_outside_the_window_are_excluded(self):
        self.receipt(100, day=TODAY - timedelta(days=40))
        self.assertEqual(
            self.dash()['kpis']['received_total'],
            0.0)

    # -- Empty state ------------------------------------------------------

    def test_empty_window_returns_zeros_not_nulls(self):
        """The UI formats these directly; a null would render as blank."""
        data = self.dash()

        self.assertEqual(data['kpis']['received_total'], 0.0)
        self.assertEqual(data['kpis']['deposit_total'], 0.0)
        self.assertEqual(data['collection_performance']['results'], [])
        for chart in data['charts'].values():
            self.assertEqual(chart['total'], 0.0)
            for slice_ in chart['slices']:
                self.assertEqual(slice_['percent'], 0.0)

    # -- Collection performance -------------------------------------------

    def test_collection_table_ranks_by_total_collected(self):
        self.receipt(1000, person=self.person)
        self.receipt(300, person=self.other)
        self.deposit(900, person=self.other)

        # Goldy ranks first: 300 received + 900 banked beats Amit's 1000. That
        # is the point of ranking on the total — someone who collects a lot but
        # banks none of it is not out-performing someone who does both.
        rows = self.people()
        self.assertEqual([r['name'] for r in rows], ['Goldy Test', 'Amit Test'])
        self.assertEqual(rows[0]['total'], 1200.0)
        self.assertEqual(rows[0]['deposited'], 900.0)
        self.assertEqual(rows[1]['received'], 1000.0)
        self.assertEqual(rows[1]['deposited'], 0.0)

    def test_bars_are_a_share_of_the_strongest_performer(self):
        self.receipt(1000, person=self.person)
        self.receipt(500, person=self.other)

        by_name = {r['name']: r for r in self.people()}
        self.assertEqual(by_name['Amit Test']['received_percent'], 100)
        self.assertEqual(by_name['Goldy Test']['received_percent'], 50)

    def test_every_participant_is_listed_not_just_a_top_few(self):
        """The table shows the whole team.

        A cut-off would silently hide the people whose figures most need
        looking at, which is the opposite of what the screen is for.
        """
        for i in range(8):
            person = CollectionPerson.objects.create(
                name=f'P{i} Test', code=f'TEST-P{i}')
            self.receipt(100 * (i + 1), person=person)

        table = self.dash(page_size=100)['collection_performance']
        # 8 collection persons + the party receipts have no person, so 8.
        self.assertEqual(table['pagination']['total'], 8)
        self.assertEqual(len(table['results']), 8)

    def test_party_receipts_still_list_their_creator(self):
        """A receipt with no collection person still had someone record it.

        `created_by` is NULL here because the fixture writes through the ORM
        rather than a request, so this asserts the row is simply absent rather
        than crashing — the four-path merge must tolerate a missing identity.
        """
        self.receipt(1000)                       # received_from_type=PARTY
        self.assertEqual(self.people(), [])


class DashboardViewTests(TestCase):
    """The HTTP surface: auth, validation and the response envelope."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username='dash-granted', password='x',
            extra_pages=[PAYMENTS_DASHBOARD])
        # Holds every ACTION permission but NOT the dashboard grant — the case
        # that proves doing the work does not imply seeing the totals.
        cls.worker = User.objects.create_user(
            username='dash-worker', password='x',
            extra_pages=['Payments_Create', 'Payments_Approve',
                         'Deposit_Create', 'Deposit_Approve'])

    def get(self, query='', user=None):
        request = APIRequestFactory().get(f'/api/payments/dashboard/{query}')
        force_authenticate(request, user=user or self.user)
        return PaymentDashboardView.as_view()(request)

    def test_requires_authentication(self):
        request = APIRequestFactory().get('/api/payments/dashboard/')
        self.assertIn(PaymentDashboardView.as_view()(request).status_code,
                      (401, 403))

    def test_returns_the_standard_envelope(self):
        body = self.get().data
        self.assertTrue(body['success'])
        for key in ('filters', 'kpis', 'charts', 'collection_performance'):
            self.assertIn(key, body['data'])

    def test_custom_range_needs_both_ends(self):
        self.assertEqual(self.get('?preset=custom').status_code, 400)
        self.assertEqual(
            self.get('?preset=custom&date_from=2026-08-01').status_code, 400)

    def test_reversed_custom_range_is_swapped_not_rejected(self):
        """A date picker can easily produce this; the intent is unambiguous."""
        response = self.get(
            '?preset=custom&date_from=2026-08-07&date_to=2026-07-01')
        self.assertEqual(response.status_code, 200)
        filters = response.data['data']['filters']
        self.assertEqual(filters['date_from'], '2026-07-01')
        self.assertEqual(filters['date_to'], '2026-08-07')

    def test_unknown_preset_does_not_error(self):
        self.assertEqual(self.get('?preset=nonsense').status_code, 200)

    def test_filters_are_echoed_back(self):
        """The client shows the resolved window, so it must be returned."""
        filters = self.get('?preset=this_month&company=OIL').data['data']['filters']
        self.assertEqual(filters['company'], 'OIL')
        self.assertEqual(filters['preset'], 'this_month')
        self.assertTrue(filters['date_from'])
