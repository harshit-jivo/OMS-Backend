"""PRDO — the rules worth holding down.

Most of these are things the JSAP predecessor got wrong, documented with
evidence in `docs/Approvals/PRODUCTION_ORDER_JSAP_SAP.md`:

* DocEntry is unique only PER COMPANY — JSAP had no company column, so 11 OIL
  orders were recorded against Beverages;
* the request IS the record; nothing copies itemCode into a second table, so
  the 18% drift JSAP carries cannot happen;
* an order SAP has moved out of Planned is retired, not left waiting — JSAP
  has 17 waiting since as far back as Oct 2025;
* an order that cannot be routed is NOT stored;
* an unreachable SAP raises rather than looking like "nothing to do" — this is
  the failure that ran silently for 33 days;
* approval needs BOTH the permission AND the stage assignment, from the
  session — JSAP took the approver's id from the request body;
* SAP's release-gate exemption is reported, not hidden.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.companies import BEVERAGES, OIL
from workflow.models import (
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowUserReplacement,
)
from workflow.services import conditions

from production.models import (
    FlowStatus,
    LogAction,
    ProductionOrder,
    ProductionOrderActionLog,
    ProductionOrderFlow,
    SapOrderStatus,
    SapOrderType,
    SapWriteStatus,
)
from production.services import flow as flow_service
from production.services import gate as gate_service
from production.services import sap as sap_service
from production.services import sync as sync_service

User = get_user_model()

DOC_TABLE = 'production.production_order'


def make_user(username, *keys):
    user = User.objects.create_user(username=username, password='test-pass-12345')
    if keys:
        user.extra_pages = list(keys)
        user.save(update_fields=['extra_pages'])
    return user


def sap_row(doc_entry=9001, item_code='FG0000030', **over):
    """One row shaped like `sap.planned_orders` returns."""
    row = {
        'sap_doc_entry': doc_entry,
        'sap_doc_num': doc_entry,
        'item_code': item_code,
        'sap_status': SapOrderStatus.PLANNED,
        'order_type': SapOrderType.STANDARD,
        'planned_qty': Decimal('1000'),
        'warehouse': 'BH-PF',
        'post_date': datetime.date(2026, 9, 10),
        'due_date': datetime.date(2026, 9, 20),
        'start_date': None,
        'remarks': '',
        'sap_user_sign': 32,
        'batch_no': '',
        'mfg_date': None,
        'expiry_date': None,
        'item_name': 'MUSTARD KACHI GHANI 1 LTR 20 PCS',
        'item_group': 'MUSTARD',
        'item_series': 389,
        'sal_factor2': Decimal('20'),
        'sal_pack_un': Decimal('1'),
        'sap_created_by': 'RAVINDER SINGH',
    }
    row.update(over)
    return row


def make_order(company=OIL, doc_entry=9001, **over):
    row = sap_row(doc_entry=doc_entry, **over)
    return ProductionOrder.objects.create(
        company=company, synced_at=timezone.now(), **row)


class _Base(TestCase):
    """One module, two workflows — the FG/PM split JSAP actually runs."""

    @classmethod
    def setUpTestData(cls):
        cls.module, _ = WorkflowModule.objects.get_or_create(
            code='PRDO', defaults={'name': 'Production Order'})
        cls.viewer = make_user('pr-viewer', 'Production_Order')
        cls.fg_approver = make_user(
            'pr-fg', 'Production_Order', 'Production_Order_Approval')
        cls.pm_approver = make_user(
            'pr-pm', 'Production_Order', 'Production_Order_Approval')
        cls.standin = make_user(
            'pr-standin', 'Production_Order', 'Production_Order_Approval')
        cls.outsider = make_user('pr-outsider', 'Production_Order')

        cls.wf_fg = Workflow.objects.create(
            module=cls.module, code='PRDO_OIL_FG', name='OIL finished goods',
            company=OIL)
        q = WorkflowQuery.objects.create(
            workflow=cls.wf_fg, name='oil-fg', company=OIL,
            query_text=(f"SELECT id FROM {DOC_TABLE} "
                        f"WHERE company = 'OIL' AND item_code NOT LIKE 'PM%'"))
        conditions.validate_and_stamp(q)
        cls.fg_stage = WorkflowStage.objects.create(
            workflow=cls.wf_fg, name='Sales', sequence=1, user=cls.fg_approver)

        cls.wf_pm = Workflow.objects.create(
            module=cls.module, code='PRDO_OIL_PM', name='OIL packaging',
            company=OIL)
        q2 = WorkflowQuery.objects.create(
            workflow=cls.wf_pm, name='oil-pm', company=OIL,
            query_text=(f"SELECT id FROM {DOC_TABLE} "
                        f"WHERE company = 'OIL' AND item_code LIKE 'PM%'"))
        conditions.validate_and_stamp(q2)
        cls.pm_stage = WorkflowStage.objects.create(
            workflow=cls.wf_pm, name='Store', sequence=1, user=cls.pm_approver)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client


# ---------------------------------------------------------------------------
# The data model
# ---------------------------------------------------------------------------

class DataModelTests(_Base):

    def _tables(self):
        with connection.cursor() as cur:
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema = 'production'""")
            return {r[0] for r in cur.fetchall()}

    def test_the_schema_holds_exactly_three_tables(self):
        self.assertEqual(
            self._tables(),
            {'production_order', 'production_order_flow',
             'production_order_action_logs'})

    def test_there_is_no_task_table(self):
        tables = self._tables()
        for banned in ('production_order_task', 'production_task',
                       'approval_task', 'workflow_task'):
            self.assertNotIn(banned, tables)

    def test_doc_entry_is_unique_only_within_a_company(self):
        """The bug that put 11 OIL orders under Beverages in JSAP.

        DocEntry sequences run per company, so the same number legitimately
        exists in two companies and must be storable twice.
        """
        make_order(company=OIL, doc_entry=10799)
        make_order(company=BEVERAGES, doc_entry=10799)      # must be allowed
        self.assertEqual(ProductionOrder.objects.filter(sap_doc_entry=10799).count(), 2)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_order(company=OIL, doc_entry=10799)    # must not

    def test_the_order_carries_no_foreign_key_to_an_oms_user(self):
        """The SAP creator is a snapshot, not an identity OMS can resolve.

        A SAP login lives in a different namespace from an OMS user; JSAP's
        `createdBy` meant two different things in two tables.
        """
        names = {f.name for f in ProductionOrder._meta.get_fields()}
        self.assertIn('sap_created_by', names)
        self.assertIn('sap_user_sign', names)
        self.assertNotIn('created_by', names)

    def test_quantities_are_derived_not_stored(self):
        order = make_order(planned_qty=Decimal('1000'), sal_factor2=Decimal('20'),
                           sal_pack_un=Decimal('1'))
        self.assertEqual(order.planned_boxes, Decimal('50'))
        self.assertEqual(order.planned_litres, Decimal('1000'))
        names = {f.name for f in ProductionOrder._meta.get_fields()}
        self.assertNotIn('planned_boxes', names)
        self.assertNotIn('planned_litres', names)

    def test_a_negative_quantity_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_order(planned_qty=Decimal('0'))


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

class IntakeTests(_Base):

    def _sap(self, rows):
        return mock.patch.object(sap_service, 'planned_orders',
                                 return_value=rows)

    def test_a_finished_good_routes_to_the_fg_workflow(self):
        with self._sap([sap_row(item_code='FG0000030')]):
            result = sync_service.intake(OIL)
        self.assertEqual(result.created, 1)
        flow = ProductionOrderFlow.objects.get()
        self.assertEqual(flow.workflow, self.wf_fg)
        self.assertEqual(flow.current_stage, self.fg_stage)
        self.assertEqual(flow.current_user, self.fg_approver)

    def test_packaging_material_routes_to_the_pm_workflow(self):
        with self._sap([sap_row(doc_entry=9002, item_code='PM0000121')]):
            sync_service.intake(OIL)
        flow = ProductionOrderFlow.objects.get()
        self.assertEqual(flow.workflow, self.wf_pm)
        self.assertEqual(flow.current_user, self.pm_approver)

    def test_an_order_that_cannot_be_routed_is_not_stored(self):
        """JSAP created the row and left it orphaned with no flow, silently."""
        with self._sap([sap_row(doc_entry=9003, item_code='XX0000001')]):
            with mock.patch.object(
                    flow_service.selection, 'select_for_module',
                    side_effect=flow_service.WorkflowError('no workflow matches')):
                result = sync_service.intake(OIL)

        self.assertEqual(ProductionOrder.objects.count(), 0)
        self.assertEqual(ProductionOrderFlow.objects.count(), 0)
        self.assertEqual(len(result.unroutable), 1)
        self.assertEqual(result.unroutable[0]['doc_entry'], 9003)

    def test_running_twice_creates_one_request(self):
        rows = [sap_row()]
        with self._sap(rows):
            sync_service.intake(OIL)
            second = sync_service.intake(OIL)
        self.assertEqual(ProductionOrder.objects.count(), 1)
        self.assertEqual(second.created, 0)

    def test_a_changed_snapshot_is_refreshed_and_logged(self):
        with self._sap([sap_row(planned_qty=Decimal('1000'))]):
            sync_service.intake(OIL)
        with self._sap([sap_row(planned_qty=Decimal('1200'))]):
            result = sync_service.intake(OIL)

        self.assertEqual(result.updated, 1)
        order = ProductionOrder.objects.get()
        self.assertEqual(order.planned_qty, Decimal('1200'))
        log = order.action_logs.filter(action=LogAction.SYNC).last()
        self.assertIn('planned_qty', log.action_data)
        self.assertEqual(log.action_data['planned_qty']['new'], '1200')

    def test_a_decided_request_is_never_overwritten_by_a_later_sync(self):
        """Once approved, the record must keep saying what was decided on."""
        with self._sap([sap_row(planned_qty=Decimal('1000'))]):
            sync_service.intake(OIL)
        flow = ProductionOrderFlow.objects.get()
        with mock.patch.object(sap_service, 'write_approval'):
            flow_service.approve(flow, user=self.fg_approver)

        with self._sap([sap_row(planned_qty=Decimal('99999'))]):
            sync_service.intake(OIL)
        self.assertEqual(ProductionOrder.objects.get().planned_qty,
                         Decimal('1000'))

    def test_an_unreachable_sap_raises_rather_than_looking_empty(self):
        """The 33-day silent failure, in one assertion.

        "SAP has nothing planned" and "we never managed to ask SAP" must not
        be the same answer.
        """
        with mock.patch.object(sap_service, 'planned_orders',
                               side_effect=sap_service.SapUnavailable('down')):
            with self.assertRaises(sap_service.SapUnavailable):
                sync_service.intake(OIL)


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

class ReconcileTests(_Base):

    def setUp(self):
        with mock.patch.object(sap_service, 'planned_orders',
                               return_value=[sap_row()]):
            sync_service.intake(OIL)
        self.order = ProductionOrder.objects.get()
        self.flow = self.order.flow

    def test_an_order_sap_released_elsewhere_is_retired(self):
        """The exit JSAP lacked — 17 of its orders still wait since Oct 2025."""
        result = sync_service.SyncResult(company=OIL)
        with mock.patch.object(sap_service, 'statuses_for',
                               return_value={self.order.sap_doc_entry: 'L'}):
            sync_service.reconcile(OIL, result)

        self.flow.refresh_from_db()
        self.assertEqual(self.flow.status, FlowStatus.OBSOLETE)
        self.assertIsNone(self.flow.current_stage_id)
        self.assertIsNone(self.flow.current_user_id)
        self.assertEqual(result.retired, 1)
        self.assertTrue(self.order.action_logs.filter(
            action=LogAction.OBSOLETE).exists())

    def test_an_order_still_planned_is_left_alone(self):
        result = sync_service.SyncResult(company=OIL)
        with mock.patch.object(sap_service, 'statuses_for',
                               return_value={self.order.sap_doc_entry: 'P'}):
            sync_service.reconcile(OIL, result)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.status, FlowStatus.PENDING)
        self.assertEqual(result.retired, 0)

    def test_an_order_that_vanished_from_sap_is_retired(self):
        result = sync_service.SyncResult(company=OIL)
        with mock.patch.object(sap_service, 'statuses_for', return_value={}):
            sync_service.reconcile(OIL, result)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.status, FlowStatus.OBSOLETE)

    def test_a_retired_request_leaves_the_queue(self):
        with mock.patch.object(sap_service, 'statuses_for',
                               return_value={self.order.sap_doc_entry: 'C'}):
            sync_service.reconcile(OIL, sync_service.SyncResult(company=OIL))
        self.assertEqual(flow_service.pending_for(self.fg_approver).count(), 0)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

class ApprovalTests(_Base):

    def setUp(self):
        with mock.patch.object(sap_service, 'planned_orders',
                               return_value=[sap_row()]):
            sync_service.intake(OIL)
        self.order = ProductionOrder.objects.get()

    def approve_url(self):
        return reverse('production-request-approve', args=[self.order.pk])

    def reject_url(self):
        return reverse('production-request-reject', args=[self.order.pk])

    def test_the_stage_user_may_approve_and_sap_is_written(self):
        with mock.patch.object(sap_service, 'write_approval',
                               return_value='{"status": "SUCCESS"}') as write:
            response = self.client_for(self.fg_approver).post(
                self.approve_url(), {'remarks': 'ok'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.order.flow.refresh_from_db()
        self.assertEqual(self.order.flow.status, FlowStatus.APPROVED)
        self.assertIsNone(self.order.flow.current_stage_id)
        write.assert_called_once()

    def test_the_permission_alone_does_not_let_you_approve(self):
        """pm_approver holds the key but owns a different stage."""
        response = self.client_for(self.pm_approver).post(
            self.approve_url(), {}, format='json')
        self.assertEqual(response.status_code, 403)
        self.order.flow.refresh_from_db()
        self.assertEqual(self.order.flow.status, FlowStatus.PENDING)

    def test_the_stage_alone_does_not_let_you_approve(self):
        """Holding the stage without the key approves nothing."""
        WorkflowStage.objects.filter(pk=self.fg_stage.pk).update(
            user=self.outsider)
        response = self.client_for(self.outsider).post(
            self.approve_url(), {}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_rejecting_without_a_reason_is_a_400_not_a_409(self):
        response = self.client_for(self.fg_approver).post(
            self.reject_url(), {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_rejection_ends_the_flow_and_is_written_to_sap(self):
        with mock.patch.object(sap_service, 'write_rejection',
                               return_value='{"status": "SUCCESS"}') as write:
            response = self.client_for(self.fg_approver).post(
                self.reject_url(), {'remarks': 'wrong batch'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.order.flow.refresh_from_db()
        self.assertEqual(self.order.flow.status, FlowStatus.REJECTED)
        self.assertIsNone(self.order.flow.current_user_id)
        # SAP's gate blocks on a missing row too, but "no row" would then mean
        # both "rejected" and "nobody has looked at it".
        write.assert_called_once()
        self.assertTrue(response.data['data']['sap_written'])

    def test_a_failed_rejection_write_does_not_undo_the_rejection(self):
        with mock.patch.object(
                sap_service, 'write_rejection',
                side_effect=sap_service.SapWriteError('HANA refused')):
            response = self.client_for(self.fg_approver).post(
                self.reject_url(), {'remarks': 'wrong batch'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.order.flow.refresh_from_db()
        self.assertEqual(self.order.flow.status, FlowStatus.REJECTED)
        self.assertFalse(response.data['data']['sap_written'])

    def test_the_sap_write_can_be_retried_for_a_rejection(self):
        with mock.patch.object(
                sap_service, 'write_rejection',
                side_effect=sap_service.SapWriteError('HANA refused')):
            self.client_for(self.fg_approver).post(
                self.reject_url(), {'remarks': 'no'}, format='json')

        url = reverse('production-retry-sap', args=[self.order.pk])
        with mock.patch.object(sap_service, 'write_rejection',
                               return_value='{"status": "SUCCESS"}') as write:
            response = self.client_for(self.fg_approver).post(url, {}, format='json')
        self.assertEqual(response.status_code, 200)
        write.assert_called_once()

    def test_a_pending_flow_cannot_be_written_to_sap(self):
        url = reverse('production-retry-sap', args=[self.order.pk])
        response = self.client_for(self.fg_approver).post(url, {}, format='json')
        self.assertEqual(response.status_code, 409)

    def test_a_failed_sap_write_does_not_undo_the_approval(self):
        with mock.patch.object(
                sap_service, 'write_approval',
                side_effect=sap_service.SapWriteError('HANA refused')):
            response = self.client_for(self.fg_approver).post(
                self.approve_url(), {}, format='json')

        self.assertEqual(response.status_code, 200)
        self.order.flow.refresh_from_db()
        self.assertEqual(self.order.flow.status, FlowStatus.APPROVED)
        self.assertFalse(response.data['data']['sap_written'])

    def test_changing_the_stage_user_reroutes_a_waiting_request(self):
        """No migration, no copied user — the flow points at the STAGE."""
        self.assertEqual(flow_service.pending_for(self.fg_approver).count(), 1)
        WorkflowStage.objects.filter(pk=self.fg_stage.pk).update(
            user=self.standin)
        self.assertEqual(flow_service.pending_for(self.fg_approver).count(), 0)
        self.assertEqual(flow_service.pending_for(self.standin).count(), 1)

    def test_a_replacement_moves_the_queue_without_touching_the_stage(self):
        today = timezone.localdate()
        WorkflowUserReplacement.objects.create(
            old_user=self.fg_approver, new_user=self.standin,
            start_date=today - datetime.timedelta(days=1),
            end_date=today + datetime.timedelta(days=1),
            reason='leave')

        self.assertEqual(flow_service.pending_for(self.standin).count(), 1)
        self.assertEqual(flow_service.pending_for(self.fg_approver).count(), 0)
        self.fg_stage.refresh_from_db()
        self.assertEqual(self.fg_stage.user, self.fg_approver)

    def test_a_second_approval_of_a_finished_flow_is_a_409(self):
        with mock.patch.object(sap_service, 'write_approval'):
            self.client_for(self.fg_approver).post(self.approve_url(), {},
                                                   format='json')
        response = self.client_for(self.fg_approver).post(
            self.approve_url(), {}, format='json')
        self.assertEqual(response.status_code, 409)


# ---------------------------------------------------------------------------
# History and the SAP gate
# ---------------------------------------------------------------------------

class HistoryAndGateTests(_Base):

    def test_history_records_the_stage_by_reference_not_by_name(self):
        """A renamed stage must read correctly in old history too."""
        with mock.patch.object(sap_service, 'planned_orders',
                               return_value=[sap_row()]):
            sync_service.intake(OIL)
        order = ProductionOrder.objects.get()
        with mock.patch.object(sap_service, 'write_approval'):
            flow_service.approve(order.flow, user=self.fg_approver)

        WorkflowStage.objects.filter(pk=self.fg_stage.pk).update(
            name='Sales (renamed)')
        log = order.action_logs.get(action=LogAction.APPROVE)
        self.assertEqual(log.stage.name, 'Sales (renamed)')
        names = {f.name for f in ProductionOrderActionLog._meta.get_fields()}
        self.assertNotIn('stage_name', names)
        self.assertNotIn('stage_sequence', names)

    def test_the_sync_log_has_no_actor(self):
        with mock.patch.object(sap_service, 'planned_orders',
                               return_value=[sap_row()]):
            sync_service.intake(OIL)
        log = ProductionOrderActionLog.objects.get(action=LogAction.SYNC)
        self.assertIsNone(log.acted_by_id)

    def test_an_order_by_the_exempt_user_is_flagged(self):
        """SAP releases these regardless. An approval that was never going to
        be enforced must not look like one that was."""
        exempt = make_order(doc_entry=9500, sap_user_sign=33,
                            sap_created_by='Gautam CHanana')
        normal = make_order(doc_entry=9501, sap_user_sign=32)
        self.assertTrue(gate_service.is_exempt(exempt))
        self.assertFalse(gate_service.is_exempt(normal))
        self.assertIn('33', gate_service.exemption_reason(exempt))
        self.assertIsNone(gate_service.exemption_reason(normal))

    def test_raw_materials_are_exempt(self):
        rm = make_order(doc_entry=9502, item_series=392)
        self.assertTrue(gate_service.is_exempt(rm))

    def test_non_standard_orders_are_exempt(self):
        special = make_order(doc_entry=9503, order_type=SapOrderType.SPECIAL)
        self.assertTrue(gate_service.is_exempt(special))
