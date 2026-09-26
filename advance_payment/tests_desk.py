"""What the Payments Approval desk LISTS, and what it says about each row.

The desk is one approver's queue plus their own record — never a view of how
other approvers stand on a request:

* waiting at a stage that is theirs today      -> listed, `awaiting_me`
* approved / rejected / returned / sent back
  by them                                       -> listed, `my_decision`
* anything else, including completed requests
  on a route where they pay                     -> NOT listed

That last group stays OPENABLE (`readable_request_ids`): the Payment and Final
holders record the UTR after paying, sometimes on a request a stand-in approved.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.models import (
    AdvanceRequest,
    Department,
    FlowStatus,
    LogAction,
    RequestFlow,
    RequestLog,
    RequestStatus,
)
from advance_payment.services import flow as flow_service
from workflow.models import WorkflowModule
from workflow.tests.factories import make_stage, make_user, make_workflow

DESK_KEY = views.APPROVAL_KEY


class DeskFixture(TestCase):
    def setUp(self):
        self.me = make_user('ap-approver', extra_pages=[DESK_KEY])
        self.other = make_user('ap-other', extra_pages=[DESK_KEY])
        self.creator = make_user('ap-creator', extra_pages=['Advance_Payment'])

        module, _ = WorkflowModule.objects.get_or_create(
            code=flow_service.MODULE_CODE, defaults={'name': 'Advance Payment'})
        self.workflow = make_workflow(module, code='AP_TEST')
        self.my_stage = make_stage(self.workflow, 1, self.me, name='Approval')
        self.their_stage = make_stage(self.workflow, 2, self.other, name='Payment Approval')
        self.department = Department.objects.create(name='Finance')
        self._n = 0

    def request(self, *, at=None, status=RequestStatus.IN_APPROVAL, flow_status=FlowStatus.PENDING):
        self._n += 1
        advance = AdvanceRequest.objects.create(
            request_no=f'AP-TEST-{self._n:04d}', company='OIL', request_type='VENDOR',
            payment_against='ADVANCE', department=self.department,
            partner_code='V001', partner_name='A Vendor', amount=Decimal('1000'),
            payment_date=date(2026, 9, 1), created_by=self.creator, status=status)
        RequestFlow.objects.create(
            request=advance, workflow=self.workflow, status=flow_status,
            current_stage=at, current_user=getattr(at, 'user', None), total_stages=2)
        return advance

    def decide(self, advance, action, *, by):
        RequestLog.objects.create(request=advance, action=action, actor=by,
                                  stage=self.my_stage, stage_name=self.my_stage.name)

    def desk(self, user=None):
        request = APIRequestFactory().get('/advance-payments/requests/', {'scope': 'desk'})
        force_authenticate(request, user=user or self.me)
        response = views.RequestListView.as_view()(request)
        self.assertEqual(response.status_code, 200, response.data)
        return {row['id']: row for row in response.data['data']['results']}


class WhatTheDeskLists(DeskFixture):
    def test_lists_what_waits_at_my_stage(self):
        waiting = self.request(at=self.my_stage)
        rows = self.desk()
        self.assertIn(waiting.pk, rows)
        self.assertTrue(rows[waiting.pk]['flow']['awaiting_me'])
        self.assertIsNone(rows[waiting.pk]['my_decision'])

    def test_lists_what_i_approved_even_once_it_has_moved_on(self):
        moved_on = self.request(at=self.their_stage)
        self.decide(moved_on, LogAction.APPROVED, by=self.me)
        rows = self.desk()
        self.assertIn(moved_on.pk, rows)
        self.assertFalse(rows[moved_on.pk]['flow']['awaiting_me'])
        self.assertEqual(rows[moved_on.pk]['my_decision']['action'], 'APPROVED')

    def test_lists_what_i_rejected_returned_or_sent_back(self):
        for action in (LogAction.REJECTED, LogAction.RETURNED, LogAction.SENT_BACK):
            advance = self.request(status=RequestStatus.REJECTED, flow_status=FlowStatus.REJECTED)
            self.decide(advance, action, by=self.me)
            with self.subTest(action=action):
                self.assertEqual(self.desk()[advance.pk]['my_decision']['action'], action)

    def test_does_not_list_what_waits_at_someone_elses_stage(self):
        theirs = self.request(at=self.their_stage)
        self.assertNotIn(theirs.pk, self.desk())

    def test_does_not_list_what_someone_else_decided(self):
        advance = self.request(status=RequestStatus.REJECTED, flow_status=FlowStatus.REJECTED)
        self.decide(advance, LogAction.REJECTED, by=self.other)
        self.assertNotIn(advance.pk, self.desk())

    def test_work_on_a_request_is_not_a_decision(self):
        # Saving payment details or recording a UTR touches a request without
        # deciding it: no row on the desk for that.
        advance = self.request(status=RequestStatus.COMPLETED, flow_status=FlowStatus.COMPLETED)
        RequestLog.objects.create(request=advance, action=LogAction.UTR_RECORDED, actor=self.me)
        self.assertNotIn(advance.pk, self.desk())

    def test_a_completed_request_on_my_payment_route_is_not_listed_but_opens(self):
        payer_workflow = make_workflow(self.workflow.module, code='AP_PAYER')
        make_stage(payer_workflow, 1, self.me, name='Payment Approval')
        advance = self.request(status=RequestStatus.COMPLETED, flow_status=FlowStatus.COMPLETED)
        RequestFlow.objects.filter(request=advance).update(workflow=payer_workflow)
        self.assertNotIn(advance.pk, self.desk())
        # ...but the UTR can still be recorded from a direct link.
        self.assertIn(advance.pk, flow_service.readable_request_ids(self.me))


class MyDecision(DeskFixture):
    def test_is_the_latest_when_a_request_came_round_again(self):
        # Approved in round 1, returned later, now waiting at my stage again.
        advance = self.request(at=self.my_stage)
        self.decide(advance, LogAction.APPROVED, by=self.me)
        self.decide(advance, LogAction.RETURNED, by=self.me)
        row = self.desk()[advance.pk]
        self.assertTrue(row['flow']['awaiting_me'])  # the desk files it under Pending
        self.assertEqual(row['my_decision']['action'], 'RETURNED')

    def test_is_my_own_not_another_approvers(self):
        advance = self.request(at=self.their_stage)
        self.decide(advance, LogAction.APPROVED, by=self.me)
        self.decide(advance, LogAction.REJECTED, by=self.other)
        self.assertEqual(self.desk()[advance.pk]['my_decision']['action'], 'APPROVED')
        self.assertEqual(self.desk(self.other)[advance.pk]['my_decision']['action'], 'REJECTED')

    def test_the_list_looks_decisions_up_in_one_query(self):
        for _ in range(5):
            advance = self.request(at=self.their_stage)
            self.decide(advance, LogAction.APPROVED, by=self.me)
        with self.assertNumQueries(1):
            decisions = flow_service.my_decisions(self.me)
        self.assertEqual(len(decisions), 5)
