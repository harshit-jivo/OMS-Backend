"""Tests for the Payments Dashboard's participants table, drill-down and grant.

Complements `tests_analytics.py`, which pins the KPI and chart definitions.
This file covers the parts added for the full dashboard:

  * who appears in Collection Performance, and why (four participation paths
    across two identity types)
  * search, sorting and pagination over that table
  * the per-person drill-down
  * `Payments_Dashboard`, the grant that guards every analytics endpoint

The identity model is the subtle part. A participant is `(kind, id)` where kind
is `person` (a CollectionPerson) or `user` (an OMS login). The two id spaces
overlap, so keying rows by a bare integer would sum one person's collections
onto another's — `test_person_and_user_ids_do_not_collide` is the regression
guard for exactly that.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from . import analytics, analytics_person
from .models import (
    BankDeposit,
    CollectionPerson,
    PaymentReceipt,
)
from .permissions import PAYMENTS_DASHBOARD, granted_keys
from .views import (
    CollectionPerformanceView,
    PaymentDashboardView,
    PersonAnalyticsView,
)

TODAY = date(2026, 8, 5)

# A company code no real row uses, so these assertions hold on a shared
# database as well as a throwaway test one.
CO = 'ZZDASH'


class DashboardFixtureMixin:
    """Receipts and deposits under a company nothing else writes to."""

    @classmethod
    def setUpTestData(cls):
        cls.person = CollectionPerson.objects.create(
            name='Amit Dash', code='DASH-AMIT')
        cls.other = CollectionPerson.objects.create(
            name='Goldy Dash', code='DASH-GOLDY')

    def dash(self, **kw):
        kw.setdefault('company', CO)
        kw.setdefault('preset', 'today')
        return analytics.dashboard(today=TODAY, **kw)

    def people(self, **kw):
        return self.dash(**kw)['collection_performance']['results']

    def receipt(self, amount, *, status='POSTED', advance=False, day=TODAY,
                person=None, creator=None):
        receipt = PaymentReceipt.objects.create(
            receipt_no=f'RCP-DASH-{PaymentReceipt.objects.count() + 1:06d}',
            company=CO, card_code='C1', card_name='Customer One',
            payment_date=day, total_amount=Decimal(amount), status=status,
            is_advance=advance,
            received_from_type='PERSON' if person else 'PARTY',
            received_from_person=person,
        )
        if creator:
            # .update() because created_by comes from TimeStampedModel and is
            # normally set from the request, which these fixtures bypass.
            PaymentReceipt.objects.filter(pk=receipt.pk).update(
                created_by=creator)
        return receipt

    def deposit(self, amount, *, status='POSTED', day=TODAY, person=None,
                creator=None):
        deposit = BankDeposit.objects.create(
            deposit_no=f'DEP-DASH-{BankDeposit.objects.count() + 1:06d}',
            company=CO, deposit_date=day, deposit_type='CASH',
            collected_amount=Decimal(amount), deposit_amount=Decimal(amount),
            status=status, deposited_by=person,
            bank_gl_account='_SYS001', bank_display_name='HDFC')
        if creator:
            BankDeposit.objects.filter(pk=deposit.pk).update(created_by=creator)
        return deposit


class ParticipationTests(DashboardFixtureMixin, TestCase):
    """Who appears in the table, and why."""

    def test_a_receipt_creator_is_listed(self):
        """Path 3: the login that recorded the receipt."""
        user = get_user_model().objects.create_user(
            username='dash-recorder', password='x')
        self.receipt(1000, creator=user)

        row = {r['key']: r for r in self.people()}[f'user:{user.id}']
        self.assertEqual(row['received'], 1000.0)
        self.assertIn('recorded', row['roles'])

    def test_a_deposit_submitter_is_listed(self):
        """Path 4: the login that raised the deposit."""
        user = get_user_model().objects.create_user(
            username='dash-submitter', password='x')
        self.deposit(500, creator=user)

        row = {r['key']: r for r in self.people()}[f'user:{user.id}']
        self.assertEqual(row['deposited'], 500.0)
        self.assertIn('submitted', row['roles'])

    def test_a_collector_and_a_banker_are_listed(self):
        """Paths 1 and 2, both CollectionPerson."""
        self.receipt(300, person=self.person)
        self.deposit(400, person=self.other)

        rows = {r['name']: r for r in self.people()}
        self.assertIn('collected', rows['Amit Dash']['roles'])
        self.assertIn('banked', rows['Goldy Dash']['roles'])

    def test_person_and_user_ids_do_not_collide(self):
        """The whole reason rows are keyed by (kind, id).

        A CollectionPerson and a User with the same primary key are different
        people. A bare integer key would merge them and double one figure.
        """
        user = get_user_model().objects.create_user(
            username='dash-collides', password='x')
        CollectionPerson.objects.filter(pk=self.person.pk).update(id=user.id)
        person = CollectionPerson.objects.get(pk=user.id)

        self.receipt(700, person=person, creator=user)

        rows = {r['key']: r for r in self.people()}
        self.assertIn(f'person:{person.id}', rows)
        self.assertIn(f'user:{user.id}', rows)
        # 700 attributed to each, NOT 1400 on one merged row.
        self.assertEqual(rows[f'person:{person.id}']['received'], 700.0)
        self.assertEqual(rows[f'user:{user.id}']['received'], 700.0)

    def test_roles_explain_a_row_with_no_receipts(self):
        """Someone who only banked still shows why they are listed."""
        self.deposit(400, person=self.other)

        row = {r['name']: r for r in self.people()}['Goldy Dash']
        self.assertEqual(row['received'], 0.0)
        self.assertEqual(row['roles'], ['banked'])
        self.assertEqual(row['role_labels'], ['Banked deposits'])

    def test_every_participant_is_listed_not_just_a_top_few(self):
        """No cut-off: the table is meant to show the whole team."""
        for i in range(9):
            person = CollectionPerson.objects.create(
                name=f'Bulk{i} Dash', code=f'DASH-B{i}')
            self.receipt(100 * (i + 1), person=person)

        table = self.dash(page_size=100)['collection_performance']
        self.assertEqual(table['pagination']['total'], 9)
        self.assertEqual(len(table['results']), 9)


class TableControlTests(DashboardFixtureMixin, TestCase):
    """Search, sorting and pagination."""

    def test_search_matches_name_or_code(self):
        self.receipt(100, person=self.person)
        self.receipt(200, person=self.other)

        self.assertEqual([r['name'] for r in self.people(search='goldy')],
                         ['Goldy Dash'])
        # Code matches too, so a code can be pasted straight in.
        self.assertEqual([r['name'] for r in self.people(search='DASH-AMIT')],
                         ['Amit Dash'])
        self.assertEqual(self.people(search='nobody at all'), [])

    def test_search_is_case_insensitive(self):
        self.receipt(100, person=self.person)
        self.assertEqual(len(self.people(search='AMIT')), 1)
        self.assertEqual(len(self.people(search='amit')), 1)

    def test_sorting_by_each_column(self):
        self.receipt(100, person=self.person)      # Amit
        self.receipt(900, person=self.other)       # Goldy

        self.assertEqual(
            self.people(sort='received', direction='desc')[0]['name'],
            'Goldy Dash')
        self.assertEqual(
            self.people(sort='name', direction='asc')[0]['name'], 'Amit Dash')
        self.assertEqual(
            self.people(sort='name', direction='desc')[0]['name'], 'Goldy Dash')

    def test_an_unknown_sort_field_falls_back_rather_than_erroring(self):
        """A stale client, or a hand-edited URL, must not 500."""
        self.receipt(100, person=self.person)
        self.assertTrue(self.people(sort='; DROP TABLE payments'))

    def test_pagination_splits_without_dropping_or_repeating(self):
        for i in range(7):
            person = CollectionPerson.objects.create(
                name=f'Page{i} Dash', code=f'DASH-P{i}')
            self.receipt(100 * (i + 1), person=person)

        seen, page = [], 1
        while True:
            table = self.dash(page=page, page_size=3)['collection_performance']
            seen += [r['key'] for r in table['results']]
            if page >= table['pagination']['total_pages']:
                break
            page += 1

        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)          # no row on two pages

    def test_page_number_is_clamped_to_what_exists(self):
        self.receipt(100, person=self.person)
        table = self.dash(page=999)['collection_performance']
        self.assertEqual(table['pagination']['page'], 1)
        self.assertEqual(len(table['results']), 1)

    def test_bars_scale_across_the_whole_set_not_the_page(self):
        """Otherwise a bar would change length depending on its page."""
        for i in range(5):
            person = CollectionPerson.objects.create(
                name=f'Bar{i} Dash', code=f'DASH-BAR{i}')
            self.receipt(100 * (i + 1), person=person)

        page2 = self.dash(page=2, page_size=2)['collection_performance']
        # Nobody on page 2 is the overall leader, so none may read 100%.
        self.assertTrue(
            all(r['received_percent'] < 100 for r in page2['results']))


class PersonDetailTests(DashboardFixtureMixin, TestCase):
    """The drill-down behind a table row."""

    def detail(self, kind, pk, **kw):
        kw.setdefault('company', CO)
        kw.setdefault('preset', 'today')
        return analytics_person.person_detail(kind, pk, today=TODAY, **kw)

    def test_totals_match_the_row_that_was_clicked(self):
        """The first thing anyone checks when opening a drill-down."""
        self.receipt(600, person=self.person, advance=False)
        self.receipt(150, person=self.person, advance=True)
        self.deposit(300, person=self.person)

        row = {r['name']: r for r in self.people()}['Amit Dash']
        detail = self.detail('person', self.person.id)

        self.assertEqual(detail['kpis']['received_total'], row['received'])
        self.assertEqual(detail['kpis']['deposit_total'], row['deposited'])
        self.assertEqual(detail['kpis']['against_invoice'], 600.0)
        self.assertEqual(detail['kpis']['advance_payment'], 150.0)

    def test_a_user_drilldown_reads_created_by_not_the_person_link(self):
        """kind decides WHICH relationship is being asked about."""
        user = get_user_model().objects.create_user(
            username='dash-detail-user', password='x')
        self.receipt(800, creator=user)
        self.receipt(250, person=self.person)      # different participant

        self.assertEqual(
            self.detail('user', user.id)['kpis']['received_total'], 800.0)
        self.assertEqual(
            self.detail('person', self.person.id)['kpis']['received_total'],
            250.0)

    def test_recent_activity_interleaves_receipts_and_deposits(self):
        self.receipt(100, person=self.person, day=date(2026, 8, 3))
        self.deposit(200, person=self.person, day=date(2026, 8, 4))

        detail = self.detail('person', self.person.id, preset='custom',
                             date_from=date(2026, 8, 1), date_to=TODAY)

        kinds = {e['kind'] for e in detail['recent_activity']}
        self.assertEqual(kinds, {'RECEIPT', 'DEPOSIT'})
        dates = [e['date'] for e in detail['recent_activity']]
        self.assertEqual(dates, sorted(dates, reverse=True))   # newest first

    def test_timeline_has_one_point_per_active_day(self):
        self.receipt(100, person=self.person, day=date(2026, 8, 3))
        self.deposit(200, person=self.person, day=date(2026, 8, 4))

        detail = self.detail('person', self.person.id, preset='custom',
                             date_from=date(2026, 8, 1), date_to=TODAY)
        self.assertEqual([p['date'] for p in detail['timeline']],
                         ['2026-08-03', '2026-08-04'])
        self.assertEqual(detail['timeline'][0]['received'], 100.0)
        self.assertEqual(detail['timeline'][1]['deposited'], 200.0)

    def test_timeline_is_omitted_for_a_window_too_wide_to_plot(self):
        """A year of daily points cannot render legibly on a phone."""
        self.receipt(100, person=self.person)
        detail = self.detail('person', self.person.id, preset='custom',
                             date_from=date(2025, 1, 1),
                             date_to=date(2026, 8, 5))
        self.assertEqual(detail['timeline'], [])

    def test_missing_person_returns_none_so_the_view_can_404(self):
        self.assertIsNone(self.detail('person', 99999999))
        self.assertIsNone(self.detail('user', 99999999))


class DashboardPermissionTests(TestCase):
    """`Payments_Dashboard` guards every analytics endpoint.

    The dashboard aggregates across every receipt and deposit in the company,
    which is more than any individual collector sees operationally. Hiding the
    menu entry is a usability affordance; these assert the server refuses.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.granted = User.objects.create_user(
            username='dash-perm-granted', password='x',
            extra_pages=[PAYMENTS_DASHBOARD])
        # Every ACTION permission but NOT the dashboard grant.
        cls.worker = User.objects.create_user(
            username='dash-perm-worker', password='x',
            extra_pages=['Payments_Create', 'Payments_Approve',
                         'Deposit_Create', 'Deposit_Approve'])
        cls.nobody = User.objects.create_user(
            username='dash-perm-nobody', password='x', extra_pages=[])
        cls.admin = User.objects.create_user(
            username='dash-perm-admin', password='x', is_superuser=True)

    def call(self, view, user, path='/api/payments/dashboard/', **kwargs):
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=user)
        return view.as_view()(request, **kwargs)

    def test_dashboard_requires_the_grant(self):
        self.assertEqual(
            self.call(PaymentDashboardView, self.granted).status_code, 200)
        self.assertEqual(
            self.call(PaymentDashboardView, self.nobody).status_code, 403)

    def test_action_permissions_do_not_grant_the_dashboard(self):
        """Doing the work is not the same as seeing everyone's totals."""
        self.assertEqual(
            self.call(PaymentDashboardView, self.worker).status_code, 403)

    def test_the_grant_alone_confers_no_action_permission(self):
        """The separation runs both ways."""
        keys = granted_keys(self.granted)
        self.assertEqual(keys, {PAYMENTS_DASHBOARD})

    def test_every_analytics_endpoint_is_guarded(self):
        """Not only the one the menu links to."""
        self.assertEqual(
            self.call(CollectionPerformanceView, self.nobody,
                      '/api/payments/dashboard/collection-performance/'
                      ).status_code, 403)
        self.assertEqual(
            self.call(PersonAnalyticsView, self.nobody,
                      '/api/payments/dashboard/person/user/1/',
                      kind='user', pk=1).status_code, 403)

    def test_admin_holds_it_implicitly(self):
        self.assertIn(PAYMENTS_DASHBOARD, granted_keys(self.admin))
        self.assertEqual(
            self.call(PaymentDashboardView, self.admin).status_code, 200)

    def test_an_unknown_participant_type_is_a_404_not_a_500(self):
        response = self.call(
            PersonAnalyticsView, self.granted,
            '/api/payments/dashboard/person/bogus/1/', kind='bogus', pk=1)
        self.assertEqual(response.status_code, 404)
