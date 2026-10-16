"""Each company's own vendor codes (2026-10-16).

ARNAV TRANSPORT is VENDA000956 in Oil and VENDA000935 in Mart: a Mart invoice
saved with Oil's code is never matched by the Save-in-SAP sweep, which looks
for number + vendor code inside the invoice's company. Pinned: the company rule
(branch wins over unit), the entry form's vendor list follows it, and a code
from another company is refused.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tracker import sap as tsap
from tracker.models import Branch, Category, GstRate, GstType, InvoiceMode, Unit
from tracker.serializers import InvoiceWriteSerializer

OIL, BEV, MART = 'JIVO_OIL_HANADB', 'JIVO_BEVERAGES_HANADB', 'JIVO_MART_HANADB'
CODES = {OIL: {'VENDA000956'}, MART: {'VENDA000935'}, BEV: {'VENDA000798'}}


@override_settings(HANA_OIL_COMPANY_DB=OIL, HANA_BEVERAGE_COMPANY_DB=BEV, HANA_MART_COMPANY_DB=MART)
class EachCompanysOwnVendors(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mart = Branch.objects.create(name='Mart')
        cls.wellness = Branch.objects.create(name='Wellness')
        cls.oil = Unit.objects.create(name='Oil')
        cls.beverages = Unit.objects.create(name='Beverages')
        cls.data = dict(invoice_date=date(2026, 10, 10), effective_month=date(2026, 10, 1),
                        party_name='ARNAV TRANSPORT SERVICE',
                        invoice_number='ATS:420', taxable_value=Decimal('100'),
                        gst_type=GstType.objects.create(name='IGST').pk,
                        gst_rate=GstRate.objects.create(label='18%', rate=Decimal('18')).pk,
                        category=Category.objects.create(name='Transport').pk,
                        mode=InvoiceMode.objects.create(name='Regular').pk)

    def test_the_company_follows_branch_then_unit(self):
        self.assertEqual(tsap.company_schema('Mart', 'Oil'), MART)   # branch wins
        self.assertEqual(tsap.company_schema('Wellness', 'Beverages'), BEV)
        self.assertEqual(tsap.company_schema('Wellness', 'Oil'), OIL)

    def check(self, branch, unit, code):
        data = {**self.data, 'branch': branch.pk, 'unit': unit.pk, 'party_code': code}
        with mock.patch.object(tsap, 'vendor_codes', side_effect=lambda schema: CODES[schema]):
            serializer = InvoiceWriteSerializer(data=data)
            return serializer.is_valid(), serializer.errors

    def test_a_code_from_another_company_is_refused(self):
        ok, errors = self.check(self.mart, self.oil, 'VENDA000956')   # Oil's code on a Mart invoice
        self.assertFalse(ok)
        self.assertIn("is not a vendor in Mart's SAP", str(errors['party_code']))
        self.assertTrue(self.check(self.mart, self.oil, 'VENDA000935')[0])
        self.assertTrue(self.check(self.wellness, self.oil, 'VENDA000956')[0])

    def test_sap_unreachable_does_not_block_entry(self):
        data = {**self.data, 'branch': self.mart.pk, 'unit': self.oil.pk, 'party_code': 'VENDA000956'}
        with mock.patch.object(tsap, 'vendor_codes', return_value=None):
            s = InvoiceWriteSerializer(data=data)
            self.assertTrue(s.is_valid(), s.errors)

    def test_the_vendor_list_is_the_invoices_company(self):
        user = get_user_model().objects.create_superuser(username='tk-vendors', password='pw')
        client = APIClient()
        client.force_authenticate(user)
        with mock.patch.object(tsap, 'fetch_vendors', return_value=[]) as fetch, \
                mock.patch('tracker.views.IsTrackerEntry.has_permission', return_value=True):
            client.get('/api/tracker/vendors/', {'branch': self.mart.pk, 'unit': self.oil.pk})
            self.assertEqual(fetch.call_args.kwargs['schema'], MART)
            client.get('/api/tracker/vendors/', {'branch': self.wellness.pk, 'unit': self.beverages.pk})
            self.assertEqual(fetch.call_args.kwargs['schema'], BEV)
