"""The OMS side of the Payments & Expenses board: raised, paid and pending requests."""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from advance_payment.models import (
    AdvanceRequest, Department, FlowStatus, LogAction, RequestFlow, RequestLog, RequestStatus,
)
from advance_payment.services import flow as flow_service
from advance_payment.services import oms_dashboard as board
from workflow.models import WorkflowModule
from workflow.tests.factories import make_stage, make_user, make_workflow

TODAY = date(2026, 10, 14)


def at(day, hour=10):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour))


class OmsBoard(TestCase):
    def setUp(self):
        self.creator = make_user('ob-creator', extra_pages=['Advance_Payment'])
        self.payer = make_user('ob-payer', extra_pages=['Advance_Payment_Approval'])
        module, _ = WorkflowModule.objects.get_or_create(code=flow_service.MODULE_CODE, defaults={'name': 'AP'})
        self.route = make_workflow(module, code='AP_OB')
        self.payment = make_stage(self.route, 1, self.payer, name='Payment Approval')
        self.department = Department.objects.create(name='Finance')
        self.n = 0

    def request(self, *, raised, amount, status=RequestStatus.IN_APPROVAL, company='OIL',
                against='AGAINST_BILL', completed=None):
        self.n += 1
        advance = AdvanceRequest.objects.create(
            request_no=f'AP-OB-{self.n:04d}', company=company, request_type='VENDOR', payment_against=against,
            department=self.department, partner_code='V1', partner_name=f'Vendor {self.n}', amount=Decimal(amount),
            payment_date=raised, created_by=self.creator, status=status)
        AdvanceRequest.objects.filter(pk=advance.pk).update(created_on=at(raised))
        pending = status == RequestStatus.IN_APPROVAL
        RequestFlow.objects.create(
            request=advance, workflow=self.route, total_stages=1,
            status=FlowStatus.PENDING if pending else FlowStatus.COMPLETED,
            current_stage=self.payment if pending else None, current_user=self.payer if pending else None)
        if completed:
            log = RequestLog.objects.create(request=advance, action=LogAction.COMPLETED, actor=self.payer)
            RequestLog.objects.filter(pk=log.pk).update(created_on=at(completed, 15))
        return advance

    def test_raised_and_paid_in_the_period_against_the_one_before(self):
        self.request(raised=date(2026, 10, 2), amount='1000')                      # raised, pending
        self.request(raised=date(2026, 9, 28), amount='400', status=RequestStatus.COMPLETED,
                     completed=date(2026, 10, 3))                                  # raised before, paid now
        self.request(raised=date(2026, 9, 20), amount='250', against='AGAINST_PO')  # the period before
        k = board.build('2026-10-01', '2026-10-14', today=TODAY)['kpis']
        self.assertEqual((k['raised']['count'], k['raised']['amount']), (1, 1000))
        self.assertEqual((k['paid']['count'], k['paid']['amount']), (1, 400))
        # The 14 days before: 17–30 Sep — two raised there (650).
        self.assertEqual(k['raised']['previous'], 650)
        self.assertEqual(k['days_to_pay'], 5.2)

    def test_pending_now_by_stage_by_person_and_by_age(self):
        self.request(raised=date(2026, 10, 13), amount='1000')
        self.request(raised=date(2026, 9, 1), amount='300', against='AGAINST_PO')
        returned = self.request(raised=date(2026, 10, 10), amount='50', status=RequestStatus.RETURNED)
        RequestFlow.objects.filter(request=returned).update(status=FlowStatus.RETURNED, current_stage=None)
        data = board.build('2026-10-01', '2026-10-14', today=TODAY)
        self.assertEqual(data['kpis']['pending'], {'count': 3, 'amount': 1350, 'returned': 1})
        self.assertEqual({w['name']: w['entries'] for w in data['waiting_at']}, {'Payment Approval': 2, 'With creator': 1})
        self.assertEqual(data['oldest'][0]['payee'], 'Vendor 2')
        self.assertEqual(sum(b['entries'] for b in data['ageing']), 3)
        self.assertEqual(data['ageing'][-1]['entries'], 1)  # raised on 1 Sep: over 15 days

    def test_bill_or_advance_and_the_company_filter(self):
        self.request(raised=date(2026, 10, 2), amount='100', against='AGAINST_BILL')
        self.request(raised=date(2026, 10, 3), amount='200', against='AGAINST_PO', company='MART')
        data = board.build('2026-10-01', '2026-10-14', today=TODAY)
        self.assertEqual({b['name']: b['amount'] for b in data['bill_or_advance']}, {'Bill': 100, 'Advance': 200})
        mart = board.build('2026-10-01', '2026-10-14', company='mart', today=TODAY)
        self.assertEqual(mart['kpis']['raised']['amount'], 200)

    def test_by_payment_purpose_and_budget_head(self):
        a = self.request(raised=date(2026, 10, 2), amount='100')
        b = self.request(raised=date(2026, 10, 3), amount='300')
        AdvanceRequest.objects.filter(pk=a.pk).update(purpose_code='FREIGHT_OUT', purpose_label='Freight Outward',
                                                      budget_code='DEL', budget_name='Del Bkhp')
        AdvanceRequest.objects.filter(pk=b.pk).update(request_type='EXPENSE', budget_code='FACTORY', budget_name='Factory')
        data = board.build('2026-10-01', '2026-10-14', today=TODAY)
        self.assertEqual({p['name']: p['amount'] for p in data['by_purpose']}, {'Expense': 300, 'Freight Outward': 100})
        self.assertEqual([h['name'] for h in data['by_budget']], ['Factory', 'Del Bkhp'])

    def test_refuses_a_bad_company_or_range(self):
        with self.assertRaises(board.DashboardInvalid):
            board.build('2026-10-01', '2026-10-14', company='ACME')
        with self.assertRaises(board.DashboardInvalid):
            board.build('2026-10-14', '2026-10-01')
