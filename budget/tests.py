"""Budget approval: routing, intake, change detection, auto-approval, the flow and SAP's gate.

No database and no SAP: rows are stand-ins, managers and HANA are patched.
"""
import contextlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from budget import hierarchy
from budget.models import ItemStatus, LogAction
from budget.services import auto, flow, routing, sap, sync
from OMS.settings import _company_schemas

TEST = {'OIL': 'TEST_JIVO_OIL_HANADB', 'BEVERAGES': 'TEST_JIVO_BEVERAGES_HANADB'}


def line(n, acct='5670001', budget='Del Bkhp', amount='100.00', **extra):
    return {'obj_type': 18, 'draft_entry': 7, 'line_num': n, 'acct_code': acct, 'budget_code': budget,
            'amount': Decimal(amount), **extra}


class Routing(SimpleTestCase):
    def test_a_head_becomes_a_stable_route(self):
        self.assertEqual(routing.head_key('Sales RE'), 'SALES_RE')
        self.assertEqual(routing.head_key('R & D'), 'R_D')
        self.assertEqual(routing.route_for('Factory', '5650003'), 'FACTORY')

    def test_electricity_gets_its_own_route(self):
        self.assertEqual(routing.route_for('BackOff', '5680011'), 'BACKOFF_ELECTRICITY')

    def test_no_budget_code_no_route(self):
        self.assertIsNone(routing.route_for('', '5670001'))
        self.assertIsNone(routing.route_for('  ', '5670001'))


class Schemas(SimpleTestCase):
    def test_parsed_from_the_env_value(self):
        self.assertEqual(_company_schemas(' oil=TEST_A , BEVERAGES=TEST_B,junk'), {'OIL': 'TEST_A', 'BEVERAGES': 'TEST_B'})
        self.assertEqual(_company_schemas(''), {})

    @override_settings(BUDGET_SAP_SCHEMAS={})
    def test_unset_means_switched_off(self):
        with self.assertRaisesRegex(sap.SapUnavailable, 'not switched on for OIL'):
            sap.schema_for('OIL')


class Intake(SimpleTestCase):
    def test_follows_draft_approval(self):
        sql = sap.intake_sql('TEST_JIVO_OIL_HANADB', 'OIL')
        for part in ('"ODRF"', '"OPDF"', '"OBTF"', '"OcrCode3" != \'Sal CF\'', '"BtfStatus" = \'O\'',
                     '"Debit" <> 0', "'5100008'", "'5630016'", ">= '2025-04-01'"):
            self.assertIn(part, sql)
        self.assertNotIn('JIVO_BEVERAGES', sql)

    def test_only_oil_payments_also_take_status_p(self):
        self.assertIn("IN ('Y', 'P')", sap.intake_sql('S', 'OIL'))
        self.assertIn("IN ('Y')", sap.intake_sql('S', 'BEVERAGES'))

    @override_settings(BUDGET_SAP_SCHEMAS=TEST)
    def test_an_unreadable_sap_raises_never_returns_nothing(self):
        with mock.patch.object(sap, 'HANAConnection', side_effect=ConnectionError('down')):
            with self.assertRaisesRegex(sap.SapUnavailable, 'Could not read OIL drafts'):
                sap.gated_lines('OIL')


class ChangeDetection(SimpleTestCase):
    def test_the_fingerprint_ignores_order_and_sees_any_change(self):
        rows = [line(0), line(1, budget='Factory')]
        self.assertEqual(sync.fingerprint(rows), sync.fingerprint(list(reversed(rows))))
        self.assertNotEqual(sync.fingerprint(rows), sync.fingerprint([line(0), line(1, budget='Factory', amount='101')]))
        self.assertNotEqual(sync.fingerprint(rows), sync.fingerprint([line(0)]))

    def test_a_draft_splits_into_one_item_per_route(self):
        routes, unrouted = sync.by_route([line(0, budget='Factory'), line(1, budget='Factory', acct='5680011'),
                                          line(2, budget='Factory'), line(3, budget='')])
        self.assertEqual({k: [r['line_num'] for r in v] for k, v in routes.items()},
                         {'FACTORY': [0, 2], 'FACTORY_ELECTRICITY': [1]})
        self.assertEqual([r['line_num'] for r in unrouted], [3])

    def test_lines_are_grouped_by_draft(self):
        groups = sync.group([line(0), line(1), {**line(0), 'draft_entry': 8}])
        self.assertEqual(sorted((k, len(v)) for k, v in groups.items()), [((18, 7), 2), ((18, 8), 1)])


class AutoApproval(SimpleTestCase):
    def test_no_budget_for_the_month_never_auto_approves(self):
        self.assertEqual(auto.within_budget(None, Decimal('0'), Decimal('1')), (False, 'no SAP budget for the month'))

    def test_within_and_over_budget(self):
        self.assertEqual(auto.within_budget(Decimal('1000'), Decimal('900'), Decimal('100')), (True, ''))
        self.assertEqual(auto.within_budget(Decimal('1000'), Decimal('900'), Decimal('100.01'))[0], False)

    def test_exempt_covers_the_configured_user_and_the_stand_in(self):
        stand_in = SimpleNamespace(configured_user_id=5, effective_user_id=9)
        with mock.patch.object(auto, 'get_stage_assignment', return_value=stand_in):
            self.assertTrue(auto.exempt(1, [5]))
            self.assertTrue(auto.exempt(1, [9]))
            self.assertFalse(auto.exempt(1, [7]))

    def test_switched_off_does_nothing(self):
        with mock.patch.object(auto.BudgetSettings, 'load', return_value=SimpleNamespace(auto_approve_enabled=False)):
            self.assertEqual(auto.run().considered, 0)


class TheHierarchy(SimpleTestCase):
    def setUp(self):
        self.routes = {r['code']: r for r in hierarchy.routes()}

    def test_every_head_has_a_route_and_an_electricity_route(self):
        self.assertEqual(len(self.routes), 2 * (len(hierarchy.OWNERS['OIL']) + len(hierarchy.OWNERS['BEVERAGES'])))
        self.assertTrue(all(len(code) <= 60 for code in self.routes))
        self.assertFalse(any('SAL' in r['route'] and 'SALES' not in r['route'] for r in self.routes.values()))

    def test_who_approves(self):
        self.assertEqual(self.routes['BUD_OIL_FACTORY']['stages'], [(hierarchy.OWNER_STAGE, 'gagan')])
        self.assertEqual(self.routes['BUD_BEV_FACTORY']['stages'], [(hierarchy.OWNER_STAGE, 'arvinder')])
        self.assertEqual(self.routes['BUD_OIL_BACKOFF_ELECTRICITY']['stages'],
                         [(hierarchy.OWNER_STAGE, 'nirmal'), (hierarchy.DIRECTOR_STAGE, hierarchy.DIRECTOR)])
        self.assertEqual(self.routes['BUD_OIL_NPD2']['stages'][-1], (hierarchy.DIRECTOR_STAGE, hierarchy.DIRECTOR))
        self.assertEqual(self.routes['BUD_OIL_R_D']['stages'], [(hierarchy.DIRECTOR_STAGE, hierarchy.DIRECTOR)])
        self.assertEqual(self.routes['BUD_OIL_OTE_ELECTRICITY']['stages'],
                         [(hierarchy.DIRECTOR_STAGE, hierarchy.DIRECTOR)])

    def test_each_query_reads_one_route_of_one_company(self):
        self.assertEqual(self.routes['BUD_BEV_SALES_RE']['query'],
                         'SELECT id FROM "budget"."budget_item" WHERE company = \'BEVERAGES\' AND route = \'SALES_RE\'')


def item(**extra):
    values = dict(pk=3, status=ItemStatus.PENDING, current_stage_id=10, current_stage=SimpleNamespace(sequence=1),
                  workflow=object(), version=4, draft=SimpleNamespace(), save=mock.Mock())
    values.update(extra)
    return SimpleNamespace(**values)


@contextlib.contextmanager
def _flow(stages, locked):
    with mock.patch.object(flow, '_locked', return_value=locked), \
            mock.patch.object(flow, 'log') as log, \
            mock.patch.object(flow, 'effective_user_id', return_value=21), \
            mock.patch.object(flow, 'refresh_draft_status') as refresh, \
            mock.patch.object(flow.selection, 'stages_for', return_value=stages),             mock.patch.object(flow, 'notify_service'):
        yield log, refresh


#: The flow without its `@transaction.atomic` wrapper: no database here.
approve = flow.approve.__wrapped__
reject = flow.reject.__wrapped__


class TheFlow(SimpleTestCase):
    STAGES = [SimpleNamespace(id=10, sequence=1), SimpleNamespace(id=11, sequence=2)]

    def test_approving_a_stage_moves_on_and_writes_v(self):
        it = item()
        with _flow(self.STAGES, it):
            got, gate = approve(it, user=SimpleNamespace(pk=21), version=4)
        self.assertEqual((gate, got.current_stage_id, got.status, got.version), ('V', 11, ItemStatus.PENDING, 5))

    def test_the_last_stage_approves_it_and_writes_a(self):
        it = item(current_stage_id=11, current_stage=SimpleNamespace(sequence=2))
        with _flow(self.STAGES, it) as (_log, refresh):
            got, gate = approve(it, user=SimpleNamespace(pk=21))
        self.assertEqual((gate, got.status, got.current_stage_id), ('A', ItemStatus.APPROVED, None))
        refresh.assert_called_once()

    def test_auto_approval_is_logged_as_such_with_no_actor(self):
        it = item()
        with _flow(self.STAGES, it) as (log, _refresh):
            approve(it, auto=True, remarks='after 48 hours')
        self.assertEqual(log.call_args.kwargs['action'], LogAction.AUTO_APPROVE)
        self.assertIsNone(log.call_args.kwargs['user'])

    def test_a_stale_screen_is_refused(self):
        it = item()
        with _flow(self.STAGES, it):
            with self.assertRaisesRegex(flow.BudgetFlowError, 'changed since you opened it'):
                approve(it, user=SimpleNamespace(pk=21), version=3)

    def test_rejecting_needs_a_reason_and_is_final(self):
        it = item()
        with _flow(self.STAGES, it):
            with self.assertRaisesRegex(flow.BudgetFlowError, 'Say why'):
                reject(it, user=SimpleNamespace(pk=21), remarks=' ')
            got = reject(it, user=SimpleNamespace(pk=21), remarks='Not budgeted')
        self.assertEqual(got.status, ItemStatus.REJECTED)
        with mock.patch.object(flow, 'effective_user_id', return_value=21):
            self.assertEqual(flow.may_act_on(SimpleNamespace(pk=21), got), (False, 'This is already rejected.'))

    def test_only_the_stage_user_today_may_act(self):
        with mock.patch.object(flow, 'effective_user_id', return_value=21):
            self.assertEqual(flow.may_act_on(SimpleNamespace(pk=22), item())[0], False)
            self.assertEqual(flow.may_act_on(SimpleNamespace(pk=21), item()), (True, ''))


class TheGateWrite(SimpleTestCase):
    def _item(self):
        lines = [SimpleNamespace(line_num=0, vis_order=0, acct_code='5670001', budget_code='Del Bkhp',
                                 sub_budget_code='', amount=Decimal('100'))]
        return SimpleNamespace(pk=3, company='OIL', save=mock.Mock(), sap_status=None, sap_status_text='',
                               sap_written_at=None,
                               draft=SimpleNamespace(obj_type=18, draft_entry=7, card_code='V1', doc_date=date(2026, 10, 1)),
                               lines=SimpleNamespace(all=lambda: lines))

    def _conn(self, read_back):
        conn = mock.MagicMock()
        conn.__enter__.return_value.execute.side_effect = lambda sql, params=None: (
            read_back if sql.startswith('SELECT') else [])
        return conn

    @override_settings(BUDGET_SAP_SCHEMAS=TEST)
    def test_writes_and_reads_back(self):
        it = self._item()
        with mock.patch.object(sap, 'HANAConnection', return_value=self._conn([{'line': 0, 'status': 'A'}])):
            text = sap.write_item(it, sap.APPROVED, decided_by='gagan')
        self.assertIn('1 line(s) = A in TEST_JIVO_OIL_HANADB.OMS_BUDGET_APPROVALS', text)
        self.assertEqual(it.sap_status, 'SUCCESS')

    @override_settings(BUDGET_SAP_SCHEMAS=TEST)
    def test_a_read_back_that_disagrees_is_a_failure(self):
        it = self._item()
        with mock.patch.object(sap, 'HANAConnection', return_value=self._conn([{'line': 0, 'status': 'R'}])):
            with self.assertRaisesRegex(sap.SapWriteError, 'different status for lines \\[0\\]'):
                sap.write_item(it, sap.APPROVED)
        self.assertEqual(it.sap_status, 'FAILED')
