"""Phase 4.2 -- query-count audit for `tracker` list endpoints.

Same technique as `orders.tests_query_audit`: seed N invoices, capture the
query count via `django.test.utils.CaptureQueriesContext`, seed more, capture
again, and assert the count did not scale.

This module found TWO real ones:

1. `InvoiceListSerializer.get_editable` calls `tracker_pages_for(user)` for
   every row still sitting, unlocked, at the entry stage -- and
   `tracker_pages_for` -> `core.permissions.role_names` ->
   `User.all_role_names()` re-runs the `extra_roles` M2M query (a fresh,
   uncached `.values_list()`) every single time it is called. `request.user`
   is the same Python object for every row in one `many=True` serialization
   pass, so the query count scaled 1:1 with the number of matching invoices in
   the response. Fixed in `tracker/serializers.py` by caching the computed
   page set on the request for the rest of that serialization pass.

2. `StuckAlertSerializer.get_notified` called `obj.notifications.select_related
   ('user').all()`, even though `AlertsView` already prefetches
   `notifications__user` for exactly this loop. `.select_related()` clones the
   manager's queryset, and Django does not carry the prefetch's `_result_cache`
   across that clone -- so every alert re-queried its notifications from
   scratch. Fixed by reading `obj.notifications.all()` (no `.select_related()`),
   which uses the already-prefetched cache.

These tests are the regression guard for both fixes.

Run with::

    python manage.py test tracker --settings=OMS.test_settings
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User, UserRole

from .management.commands.seed_tracker import STAGES
from .models import (
    Branch, Category, GstRate, GstType, Invoice, InvoiceMode, Stage, Unit,
    UserStageAccess,
)
from .services import create_invoice
from .views import AdminInvoicesView, AlertsView, InvoiceListCreateView, MyQueueView


def _query_count(view_callable, request):
    with CaptureQueriesContext(connection) as ctx:
        response = view_callable(request)
        assert response.status_code == 200, (response.status_code, getattr(response, 'data', None))
    return len(ctx.captured_queries)


class TrackerListQueryAuditTestCase(TestCase):
    """Shared lookups for every test in this module -- mirrors the fixture in
    `tracker.tests_stage_transitions.TrackerFlowTestCase`."""

    @classmethod
    def setUpTestData(cls):
        for spec in STAGES:
            Stage.objects.update_or_create(code=spec['code'], defaults=spec)
        cls.stages = {s.code: s for s in Stage.objects.all()}

        cls.gst_type = GstType.objects.create(name='IGST')
        cls.gst_rate = GstRate.objects.create(label='18%', rate=Decimal('18'))
        cls.unit = Unit.objects.create(name='Unit A')
        cls.branch = Branch.objects.create(name='Delhi')
        cls.mode = InvoiceMode.objects.create(name='Regular')
        cls.oil = Category.objects.create(name='Oil')

        entry_role = UserRole.objects.create(name='tracker_entry', display_name='Tracker Entry')
        cls.entry_user = User.objects.create_user(
            username='qa-tk-entry', password='pw', name='Entry Desk', role=entry_role)

        admin_role = UserRole.objects.create(name='tracker_admin', display_name='Tracker Admin')
        cls.admin_user = User.objects.create_user(
            username='qa-tk-admin', password='pw', name='Tracker Admin', role=admin_role)
        # AlertsView scopes a non-superuser to their assigned stages; the admin
        # user needs an explicit grant on 'entry' to see alerts parked there.
        UserStageAccess.objects.get_or_create(
            user=cls.admin_user, stage=cls.stages['entry'], defaults={'is_active': True})

    def _add_invoices(self, count, created_by):
        base = Invoice.objects.count()
        for i in range(count):
            create_invoice(
                created_by=created_by,
                invoice_date=date(2026, 8, 1),
                effective_month=date(2026, 8, 1),
                party_name='ACME',
                invoice_number=f'INV-{base + i}',
                taxable_value=Decimal('1000.00'),
                invoice_value=Decimal('1180.00'),
                gst_type=self.gst_type,
                gst_rate=self.gst_rate,
                category=self.oil,
                unit=self.unit,
                branch=self.branch,
                mode=self.mode,
            )

    def _request(self, path, user):
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=user)
        return request


class MyQueueViewQueryAuditTests(TrackerListQueryAuditTestCase):
    """Invoices sitting, unlocked, at the shared entry desk -- the exact shape
    that triggered `get_editable`'s per-row role lookup."""

    def test_query_count_does_not_scale_with_invoice_count(self):
        self._add_invoices(3, created_by=self.entry_user)
        count_at_3 = _query_count(
            MyQueueView.as_view(), self._request('/api/tracker/my-queue/', self.entry_user))

        self._add_invoices(3, created_by=self.entry_user)  # 6 invoices total
        count_at_6 = _query_count(
            MyQueueView.as_view(), self._request('/api/tracker/my-queue/', self.entry_user))

        self.assertEqual(
            count_at_3, count_at_6,
            f'MyQueueView issued {count_at_3} queries for 3 invoices but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )


class AdminInvoicesViewQueryAuditTests(TrackerListQueryAuditTestCase):
    """The tracker-admin master list -- same serializer, same fix."""

    def test_query_count_does_not_scale_with_invoice_count(self):
        self._add_invoices(3, created_by=self.admin_user)
        count_at_3 = _query_count(
            AdminInvoicesView.as_view(), self._request('/api/tracker/all-invoices/', self.admin_user))

        self._add_invoices(3, created_by=self.admin_user)
        count_at_6 = _query_count(
            AdminInvoicesView.as_view(), self._request('/api/tracker/all-invoices/', self.admin_user))

        self.assertEqual(
            count_at_3, count_at_6,
            f'AdminInvoicesView issued {count_at_3} queries for 3 invoices but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )


class InvoiceListCreateViewQueryAuditTests(TrackerListQueryAuditTestCase):
    """The entry desk's own list -- same serializer, same fix, different
    permission class (`IsTrackerEntry`)."""

    def test_query_count_does_not_scale_with_invoice_count(self):
        self._add_invoices(3, created_by=self.entry_user)
        count_at_3 = _query_count(
            InvoiceListCreateView.as_view(),
            self._request('/api/tracker/invoices/', self.entry_user))

        self._add_invoices(3, created_by=self.entry_user)
        count_at_6 = _query_count(
            InvoiceListCreateView.as_view(),
            self._request('/api/tracker/invoices/', self.entry_user))

        self.assertEqual(
            count_at_3, count_at_6,
            f'InvoiceListCreateView issued {count_at_3} queries for 3 '
            f'invoices but {count_at_6} for 6 -- query count scales with row '
            f'count (N+1).',
        )


class AlertsViewQueryAuditTests(TrackerListQueryAuditTestCase):
    """`StuckAlert` list -- already select_related/prefetch_related; confirms
    that holds under load rather than assuming it."""

    def _add_alerts(self, count, created_by):
        from .models import StuckAlert
        from django.utils import timezone

        self._add_invoices(count, created_by=created_by)
        entry_stage = self.stages['entry']
        invoices = list(
            entry_stage.current_invoices.order_by('-id')[:count]
        )
        for invoice in invoices:
            StuckAlert.objects.create(
                invoice=invoice, stage=entry_stage,
                stage_entered_at=timezone.now(),
                days_stuck=Decimal('5'), threshold_days=2, is_active=True,
            )

    def test_query_count_does_not_scale_with_alert_count(self):
        self._add_alerts(3, created_by=self.admin_user)
        count_at_3 = _query_count(
            AlertsView.as_view(), self._request('/api/tracker/alerts/', self.admin_user))

        self._add_alerts(3, created_by=self.admin_user)
        count_at_6 = _query_count(
            AlertsView.as_view(), self._request('/api/tracker/alerts/', self.admin_user))

        self.assertEqual(
            count_at_3, count_at_6,
            f'AlertsView issued {count_at_3} queries for 3 alerts but '
            f'{count_at_6} for 6 -- query count scales with row count (N+1).',
        )
