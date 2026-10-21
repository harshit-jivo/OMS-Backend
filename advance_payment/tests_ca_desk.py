"""The routing of 2026-10-19: the CA desk, bills straight to Payment, freight to Navdeep.

No database: routes are data, and the seed command's stage writer is fed a
stand-in for the stage table.

* Every route ends Payment -> CA (Navdeep) -> Audit -> Final (an Expense:
  Payment -> CA -> Expense Audit). A route seeded before CA still runs.
* A bill, whatever it is for, goes straight to Payment: no Department Head.
* Freight goes to Navdeep first, then Payment, bills included: no head.
* Mart's Employee Imprest up to 20,000 goes to Navdeep, above it to Prabhjot.
* Re-seeding matches stages by NAME, so a request waiting at Audit stays at
  Audit when CA is inserted before it.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment import hierarchy, purposes
from advance_payment.management.commands import seed_payment_workflows as seed
from advance_payment.models import StageRole
from advance_payment.services import flow as flow_service
from advance_payment.tests_hierarchy import _po_request
from advance_payment.tests_requests import _clean, vendor_bill_request

BY_CODE = {r['code']: r for r in hierarchy.routes()}


def _named(names):
    return flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, n in enumerate(names, start=1)])


class TheCaDesk(SimpleTestCase):
    def test_is_its_own_role_by_its_name(self):
        self.assertEqual(StageRole.of('CA Approval'), StageRole.CA)
        self.assertEqual(StageRole.of('ca  approval'), StageRole.CA)
        self.assertEqual(hierarchy.PEOPLE['ca'], 'Navdeep')

    def test_sits_between_payment_and_audit_and_a_route_without_it_still_runs(self):
        with_ca = _named(['HOD', 'Payment Approval', 'CA Approval', 'Audit Approval', 'Final Approval'])
        self.assertEqual([r for _s, r in with_ca],
                         [StageRole.APPROVAL, StageRole.PAYMENT, StageRole.CA, StageRole.AUDIT, StageRole.FINAL])
        without = _named(['HOD', 'Payment Approval', 'Audit Approval', 'Final Approval'])  # seeded before CA
        self.assertEqual([r for _s, r in without][-3:], [StageRole.PAYMENT, StageRole.AUDIT, StageRole.FINAL])
        expense = _named(['Payment Approval', 'CA Approval', 'Expense Audit Approval'])
        self.assertEqual(flow_service.posting_role(expense), StageRole.AUDIT)
        with self.assertRaises(flow_service.FlowError):
            _named(['Payment Approval', 'Audit Approval', 'CA Approval', 'Final Approval'])  # out of order

    def test_sees_the_account_and_may_send_back_to_payment(self):
        self.assertIn(StageRole.CA, flow_service.ACCOUNT_ROLES)
        self.assertIn(StageRole.CA, flow_service.SEND_BACK_ROLES)

    def test_is_on_every_route_after_its_approvals(self):
        for route in BY_CODE.values():
            with self.subTest(route['code']):
                names = [n for n, _r in hierarchy.full_stages(route)]
                if route.get('expense'):  # approvals first, then Accounts, CA, its Audit
                    self.assertEqual(names[-3:-1], ['Accounts Approval', 'CA Approval'])
                elif route.get('approvals_first'):  # moved: its approvals, then Payment
                    self.assertEqual(names[-4:-2], ['Payment Approval', 'CA Approval'])
                else:  # Payment first, then approvals, then CA, Audit, Final
                    self.assertEqual(names[0], 'Payment Approval')
                    self.assertEqual(names[-3], 'CA Approval')


class BillsGoStraightToPayment(SimpleTestCase):
    def test_one_route_with_no_approvals_takes_every_bill_but_freight(self):
        route = BY_CODE['AP_AGAINST_BILL']
        self.assertEqual(route['company'], 'ALL')
        self.assertEqual(route['stages'], [])
        self.assertIn(hierarchy.AGAINST_BILL, route['query'])
        self.assertIn(f'NOT {hierarchy.FREIGHT_ROUTED}', route['query'])
        for code, other in BY_CODE.items():
            if code in ('AP_AGAINST_BILL', 'AP_ACCOUNT', 'AP_ACCOUNT_DIRECTOR', 'AP_IMPREST_JOURNAL') or other.get('expense')                     or other.get('never_chosen') \
                    or code.startswith(('AP_FREIGHT_', 'AP_STAFF_ADVANCE_FACTORY')):
                continue
            with self.subTest(code):
                self.assertIn(hierarchy.NOT_BILL, other['query'])

    def test_no_department_head_is_asked_whatever_the_purpose_or_type(self):
        for purpose, rtype in (('RENT', 'VENDOR'), ('UTILITIES', 'VENDOR'), ('EMP_IMPREST', 'EMPLOYEE_IMPREST')):
            with self.subTest(purpose):
                self.assertFalse(purposes.needs_department_head('OIL', rtype, purpose, '', 'AGAINST_BILL'))
        self.assertTrue(purposes.needs_department_head('OIL', 'VENDOR', 'RENT', '', 'AGAINST_PO'))
        cleaned = _clean(vendor_bill_request(purpose_code='RENT', department_head_code='TEMP0001'))
        self.assertIsNone(cleaned.fields['department_head'])


class FreightGoesToNavdeepFirst(SimpleTestCase):
    def test_every_freight_purpose_in_every_company_bills_included(self):
        for code in purposes.FREIGHT_PURPOSES:
            with self.subTest(code):
                route = BY_CODE[f'AP_{code}']
                self.assertEqual(route['company'], 'ALL')
                self.assertEqual(route['stages'], [(hierarchy.FREIGHT_STAGE, 'navdeep')])
                self.assertNotIn('payment_against', route['query'])
                self.assertNotIn('company', route['query'])

    def test_no_department_head_but_an_employee_imprest_keeps_its_own(self):
        self.assertFalse(purposes.needs_department_head('OIL', 'VENDOR', 'FREIGHT_IN', '', 'AGAINST_PO'))
        self.assertFalse(purposes.needs_department_head('BEVERAGES', 'VENDOR', 'FREIGHT_IMPORT', '', 'AGAINST_BILL'))
        self.assertTrue(purposes.needs_department_head('OIL', 'EMPLOYEE_IMPREST', 'FREIGHT_IN', '', 'ADVANCE'))
        cleaned = _clean(_po_request(purpose_code='FREIGHT_OUT', department_head_code='TEMP0001'))
        self.assertIsNone(cleaned.fields['department_head'])


class MartSmallImprests(SimpleTestCase):
    def test_up_to_twenty_thousand_navdeep_above_prabhjot(self):
        small = BY_CODE['AP_MART_IMPREST_SMALL']
        self.assertEqual(small['stages'], [(hierarchy.IMPREST_STAGE, 'navdeep')])
        self.assertIn("request_type = 'EMPLOYEE_IMPREST' AND amount <= 20000", small['query'])
        self.assertIn(hierarchy.NOT_BILL, small['query'])
        self.assertIn(f'NOT {hierarchy.MART_SMALL_IMPREST}', BY_CODE['AP_MART']['query'])
        self.assertEqual(BY_CODE['AP_MART']['stages'], [(hierarchy.OWNER_STAGE, 'mart')])


class _Stages:
    """A stand-in for `WorkflowStage`: rows in a list, and the two queries `_write_stages` makes."""

    def __init__(self, rows):
        self.rows = rows
        self.objects = self
        self._pk = 100

    def __call__(self, workflow, name):
        stand_in = self

        class Row(SimpleNamespace):
            def save(row):
                if getattr(row, 'pk', None) is None:
                    stand_in._pk += 1
                    row.pk = stand_in._pk
                    stand_in.rows.append(row)
        return Row(workflow=workflow, name=name, pk=None, sequence=None, is_active=True, user=None)

    def filter(self, **kw):
        if 'pk' in kw:
            hit = [r for r in self.rows if r.pk == kw['pk']]
            return SimpleNamespace(update=lambda **u: [setattr(r, k, v) for r in hit for k, v in u.items()])
        return SimpleNamespace(order_by=lambda *a: sorted(self.rows, key=lambda r: (not r.is_active, r.sequence)))


def _row(pk, name, sequence, user='old'):
    row = SimpleNamespace(pk=pk, name=name, sequence=sequence, is_active=True, user=user)
    row.save = lambda: None
    return row


class ReSeedingKeepsWaitingRequestsOnTheirStage(SimpleTestCase):
    def test_payment_moves_first_and_ca_goes_in_the_same_rows_kept(self):
        head, payment, audit, final = (_row(1, 'Department Head Approval', 1), _row(2, 'Payment Approval', 2),
                                       _row(3, 'Audit Approval', 3), _row(4, 'Final Approval', 4))
        table = _Stages([head, payment, audit, final])
        users = {role: f'user:{role}' for role in ('payment', 'ca', 'audit', 'final', 'final')}
        with mock.patch.object(seed, 'WorkflowStage', table):
            seed.Command._write_stages('wf', hierarchy.full_stages(BY_CODE['AP_RENT']), users)
        live = sorted((r for r in table.rows if r.is_active), key=lambda r: r.sequence)
        self.assertEqual([r.name for r in live], ['Payment Approval', 'Department Head Approval', 'CA Approval',
                                                  'Audit Approval', 'Final Approval'])
        # The same rows, so requests on them stay put.
        self.assertEqual([live[0], live[1], live[3], live[4]], [payment, head, audit, final])
        self.assertEqual(sorted(r.sequence for r in table.rows), list(range(1, 6)))

    def test_a_stage_the_route_drops_is_retired_and_numbered_first(self):
        head, payment, audit, final = (_row(1, 'Department Head Approval', 1), _row(2, 'Payment Approval', 2),
                                       _row(3, 'Audit Approval', 3), _row(4, 'Final Approval', 4))
        table = _Stages([head, payment, audit, final])
        users = {role: f'user:{role}' for role in ('payment', 'ca', 'audit', 'final', 'navdeep')}
        with mock.patch.object(seed, 'WorkflowStage', table):
            seed.Command._write_stages('wf', hierarchy.full_stages(BY_CODE['AP_FREIGHT_IN']), users)
        live = sorted((r for r in table.rows if r.is_active), key=lambda r: r.sequence)
        self.assertEqual([(r.name, r.user) for r in live], [
            ('Payment Approval', 'user:payment'), ('CA Approval', 'user:ca'),
            ('Audit Approval', 'user:audit'), ('Final Approval', 'user:final')])
        self.assertIs(live[0], payment)
        self.assertIs(live[2], audit)
        self.assertFalse(head.is_active)
        self.assertLess(head.sequence, min(r.sequence for r in live))


class TheAccountsDesk(SimpleTestCase):
    """2026-10-20: an Expense route's Payment desk is the Accounts desk (Satnam Singh)."""

    def test_the_payment_row_is_renamed_in_place_so_its_requests_stay(self):
        owner, director, payment, ca, audit = (
            _row(1, 'Budget Owner Approval', 1), _row(2, 'Director Approval', 2), _row(3, 'Payment Approval', 3),
            _row(4, 'CA Approval', 4), _row(5, 'Expense Audit Approval', 5))
        table = _Stages([owner, director, payment, ca, audit])
        users = {role: f'user:{role}' for role in ('backoffice', 'director', 'accounts', 'ca', 'expense_audit')}
        with mock.patch.object(seed, 'WorkflowStage', table):
            seed.Command._write_stages('wf', hierarchy.full_stages(BY_CODE['AP_EXP_BEV_BACKOFF']), users)
        live = sorted((r for r in table.rows if r.is_active), key=lambda r: r.sequence)
        self.assertEqual([(r.name, r.user) for r in live], [
            ('Budget Owner Approval', 'user:backoffice'), ('Director Approval', 'user:director'),
            ('Accounts Approval', 'user:accounts'), ('CA Approval', 'user:ca'),
            ('Expense Audit Approval', 'user:expense_audit')])
        self.assertIs(live[2], payment)
        self.assertEqual(len(table.rows), 5)

    def test_a_payment_route_keeps_its_payment_desk(self):
        self.assertEqual(hierarchy.full_stages(BY_CODE['AP_RENT'])[0], ('Payment Approval', 'payment'))
        stages = hierarchy.full_stages(BY_CODE['AP_EXP_BEV_BACKOFF'])
        self.assertNotIn(('Payment Approval', 'payment'), stages)
        named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in enumerate(stages, 1)])
        self.assertEqual([r for _s, r in named][-3:], [StageRole.PAYMENT, StageRole.CA, StageRole.AUDIT])


class TheRequestsMovedToNavdeep(SimpleTestCase):
    """The requests waiting at an approval stage on 2026-10-19 were moved to Navdeep, then Payment."""

    def test_two_routes_never_chosen_navdeep_then_payment_ca_and_the_rest(self):
        moved = {r['code']: r for r in hierarchy.moved_routes() if r['code'] != 'AP_MOVED_TO_DIRECTOR'}
        self.assertEqual(set(moved), {'AP_MOVED_TO_NAVDEEP', 'AP_EXP_MOVED_TO_NAVDEEP'})
        for route in moved.values():
            with self.subTest(route['code']):
                self.assertEqual(route['query'], 'SELECT id FROM advance_payment_request WHERE 1 = 0')
                self.assertEqual(route['stages'], [(hierarchy.MOVED_STAGE, 'navdeep')])
        # Payment first; Navdeep's own approval is his CA one (2026-10-20).
        self.assertEqual([n for n, _r in hierarchy.full_stages(moved['AP_MOVED_TO_NAVDEEP'])],
                         ['Payment Approval', 'CA Approval', 'Audit Approval', 'Final Approval'])
        self.assertEqual([n for n, _r in hierarchy.full_stages(moved['AP_EXP_MOVED_TO_NAVDEEP'])],
                         ['Accounts Approval', 'CA Approval', 'Expense Audit Approval'])
        self.assertTrue(all(code in BY_CODE for code in moved))  # seeded with the rest



class PaymentFirst(SimpleTestCase):
    """2026-10-20: Payment first, then the approvals, then CA, Audit, Final."""

    def test_the_order_and_the_earlier_shape_still_run(self):
        new = _named(['Payment Approval', 'HOD', 'Director', 'CA Approval', 'Audit Approval', 'Final Approval'])
        self.assertTrue(flow_service.payment_first(new))
        old = _named(['HOD', 'Payment Approval', 'CA Approval', 'Audit Approval', 'Final Approval'])
        self.assertFalse(flow_service.payment_first(old))
        expense = _named(['Payment Approval', 'Owner', 'CA Approval', 'Expense Audit Approval'])
        self.assertEqual(flow_service.posting_role(expense), StageRole.AUDIT)
        for bad in (['HOD', 'Payment Approval', 'Director', 'CA Approval', 'Audit Approval', 'Final Approval'],
                    ['Payment Approval', 'CA Approval', 'HOD', 'Audit Approval', 'Final Approval'],
                    ['Payment Approval', 'HOD']):
            with self.subTest(bad), self.assertRaises(flow_service.FlowError):
                _named(bad)

    def test_navdeeps_own_approval_is_dropped_where_he_holds_ca(self):
        for code in ('AP_FREIGHT_IN', 'AP_MART_IMPREST_SMALL', 'AP_TA_DA', 'AP_INCENTIVE', 'AP_IMPREST_JOURNAL'):
            with self.subTest(code):
                roles = [r for _n, r in hierarchy.full_stages(BY_CODE[code])]
                self.assertEqual(roles.count('ca'), 1)
                self.assertNotIn('navdeep', roles)


class _Flow(SimpleNamespace):
    pass


class PastApprovedStages(SimpleTestCase):
    """From Payment, past approvals already made this round, and past the CA person's own stage."""

    def _walk(self, route, approved=(), ca_user=9, users=None):
        users = users or {}
        flow = _Flow(request='req', at=None)
        stages = [(SimpleNamespace(id=i, name=name), role) for i, (name, role) in enumerate(route, start=1)]

        def nxt(f):
            index = next(i for i, (s, _r) in enumerate(stages) if s.id == f.at)
            return stages[index + 1] if index + 1 < len(stages) else None

        def point(f, stage, _role):
            f.at = stage.id
        flow.at = 1
        with mock.patch.object(flow_service, '_ca_user_id', return_value=ca_user), \
                mock.patch.object(flow_service, '_next', side_effect=nxt), \
                mock.patch.object(flow_service, '_point_at', side_effect=point), \
                mock.patch.object(flow_service, '_approved_this_round', side_effect=lambda f, s: s.id in approved), \
                mock.patch.object(flow_service, 'stage_user_id', side_effect=lambda req, s: users.get(s.id, 100 + s.id)):
            following = flow_service._past_approved(flow, nxt(flow))
        return following[0].name if following else None

    ROUTE = [('Payment Approval', StageRole.PAYMENT), ('HOD', StageRole.APPROVAL),
             ('Director', StageRole.APPROVAL), ('CA Approval', StageRole.CA), ('Audit Approval', StageRole.AUDIT)]

    def test_approvals_already_made_this_round_are_passed(self):
        self.assertEqual(self._walk(self.ROUTE, approved={2, 3}), 'CA Approval')
        self.assertEqual(self._walk(self.ROUTE, approved={2}), 'Director')
        self.assertEqual(self._walk(self.ROUTE), 'HOD')  # a new round: asked again

    def test_the_ca_persons_own_approval_is_passed(self):
        # Navdeep picked as HOD: he approves once, at CA.
        self.assertEqual(self._walk(self.ROUTE, users={2: 9}), 'Director')

    def test_the_timeline_says_so(self):
        plan = [{'role': StageRole.PAYMENT, 'user_id': 1, 'state': 'APPROVED'},
                {'role': StageRole.APPROVAL, 'user_id': 9, 'state': 'UPCOMING'},
                {'role': StageRole.CA, 'user_id': 9, 'state': 'UPCOMING'}]
        self.assertEqual([e['state'] for e in flow_service.mark_with_ca(plan)], ['APPROVED', 'WITH_CA', 'UPCOMING'])


class TheFixedAssetsMovedToTheDirector(SimpleTestCase):
    """2026-10-20: Fixed Assets waiting at Payment, past their owner, go to the Director, then Payment."""

    def test_director_then_payment_ca_audit_final_never_chosen(self):
        route = BY_CODE['AP_MOVED_TO_DIRECTOR']
        self.assertEqual(route['query'], hierarchy.NEVER_CHOSEN)
        stages = hierarchy.full_stages(route)
        self.assertEqual([n for n, _r in stages], ['Director Approval', 'Payment Approval', 'CA Approval',
                                                   'Audit Approval', 'Final Approval'])
        # The engine still runs the approvals-first shape.
        named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in enumerate(stages, 1)])
        self.assertEqual([r for _s, r in named], [StageRole.APPROVAL, StageRole.PAYMENT, StageRole.CA,
                                                 StageRole.AUDIT, StageRole.FINAL])

    def test_a_new_fixed_asset_request_still_meets_its_owner_then_the_director(self):
        for code in ('AP_FA_CIVIL', 'AP_FA_PM_OIL', 'AP_FA_PM_BEV', 'AP_FA_OTHERS_OIL', 'AP_FA_CONSUMABLES_BEV'):
            with self.subTest(code):
                names = [n for n, _r in hierarchy.full_stages(BY_CODE[code])]
                self.assertEqual(names[:3], ['Payment Approval', 'Budget Owner Approval', 'Director Approval'])
