"""Open bills and POs by date, a page at a time, with the total (the Send Bills & POs page)."""
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.services import sap

ROWS = [{'doc_entry': 501, 'balance_due': '100', 'open_amount': '100'}]


def _get(view, path):
    request = APIRequestFactory().get(path)
    force_authenticate(request, user=SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True))
    return view.as_view()(request)


class TheSql(SimpleTestCase):
    def test_paging_is_coerced_to_whole_numbers(self):
        self.assertEqual(sap._paging('50', '100'), 'LIMIT 50 OFFSET 100')
        self.assertEqual(sap._paging(None, '-5'), f'LIMIT {sap.DEFAULT_LIMIT} OFFSET 0')
        self.assertEqual(sap._paging('x', 'y; DROP'), f'LIMIT {sap.DEFAULT_LIMIT} OFFSET 0')

    def test_the_date_is_a_bound_parameter(self):
        with mock.patch.object(sap, '_schema', return_value='JIVO_OIL_HANADB'):
            parts, params = sap._open_po_query('OIL', 'VENDA000101', None, date(2026, 9, 1))
        self.assertEqual(parts['since'], 'AND T0."DocDate" >= ?')
        self.assertEqual(params, ['VENDA000101', date(2026, 9, 1)])


@mock.patch.object(views.ap_perms.CanViewLookups, 'has_permission', return_value=True)
class TheEndpoints(SimpleTestCase):
    def test_a_page_of_bills_since_a_date_with_the_total(self, _perm):
        with mock.patch.object(views.sap_service, 'open_invoices', return_value=list(ROWS)) as listed, \
                mock.patch.object(views.sap_service, 'count_open_invoices', return_value=167) as counted, \
                mock.patch.object(views.reservations, 'annotate', side_effect=lambda *a, **k: a[2]) as annotate:
            response = _get(views.OpenInvoicesView,
                            '/x/?company=OIL&party_type=vendor&from_date=2026-09-01&offset=50&limit=50')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['total'], 167)
        self.assertEqual(listed.call_args.kwargs['from_date'], date(2026, 9, 1))
        self.assertEqual(listed.call_args.kwargs['offset'], '50')
        self.assertEqual(counted.call_args.kwargs['from_date'], date(2026, 9, 1))
        # On a page, fully-held documents stay (marked), so the pages and the total agree.
        self.assertFalse(annotate.call_args.kwargs['drop_exhausted'])

    def test_without_an_offset_nothing_is_counted_and_used_up_ones_are_dropped(self, _perm):
        with mock.patch.object(views.sap_service, 'open_purchase_orders', return_value=list(ROWS)), \
                mock.patch.object(views.sap_service, 'count_open_purchase_orders') as counted, \
                mock.patch.object(views.reservations, 'annotate', side_effect=lambda *a, **k: a[2]) as annotate:
            response = _get(views.OpenPurchaseOrdersView, '/x/?company=OIL&card_code=VENDA000101')
        self.assertNotIn('total', response.data['data'])
        counted.assert_not_called()
        self.assertTrue(annotate.call_args.kwargs['drop_exhausted'])

    def test_a_bad_date_is_refused(self, _perm):
        response = _get(views.OpenPurchaseOrdersView, '/x/?company=OIL&from_date=last-month')
        self.assertEqual(response.status_code, 400)
        self.assertIn('from_date must be a date', response.data['message'])
