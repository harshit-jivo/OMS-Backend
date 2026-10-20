"""The approval hierarchy: one workflow per purpose, and the Department Head.

No database: the routes are data, and the flow's managers are stood in for.
What is pinned:

* every purpose has a route, and no request can match two (Mart, Staff
  Advance and each purpose are kept apart by their WHERE clauses);
* the "by department" purposes go to the Department Head the requester
  picked, the hierarchy's fixed owners to their person, the Director after
  where the hierarchy says so, and every route ends Payment, Audit, Final;
* the form must name a Department Head exactly when the route has one;
* the Department Head stage is handled by the picked head, not its
  configured placeholder, and the Director picked as head approves once.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment import hierarchy, purposes
from advance_payment.models import (
    DEPARTMENT_HEAD_STAGE, EXPENSE_AUDIT_STAGE, StageRole, is_department_head_stage,
)
from advance_payment.purposes import HEAD_PURPOSES, PAYMENT_PURPOSES, needs_department_head
from advance_payment.services import flow as flow_service
from advance_payment.services import heads
from advance_payment.services import requests as request_service
from advance_payment.tests_requests import _clean, vendor_bill_request

#: The routes a request can be chosen for: not the two the waiting requests were moved onto (2026-10-19).
ALL_ROUTES = [r for r in hierarchy.routes() if not r.get('never_chosen')]
#: The payment routes (by purpose); Expense routes (by budget head) are pinned in `ExpenseRoutes`.
ROUTES = [r for r in ALL_ROUTES if not r.get('expense')]
EXPENSE_ROUTES = [r for r in ALL_ROUTES if r.get('expense')]
BY_CODE = {r['code']: r for r in ALL_ROUTES}


def _routes_for(purpose):
    return [r for r in ROUTES if f"purpose_code = '{purpose}'" in r['query']]


class EveryRequestHasOneRoute(SimpleTestCase):
    def test_every_purpose_has_its_own_workflow(self):
        for code, _label, _group in PAYMENT_PURPOSES:
            with self.subTest(code):
                self.assertTrue(_routes_for(code), f'{code} has no route')
        # +Mart, Staff x4 (non-plant/imprest, Oil plant, Beverages plant, plant above 20,000); Salary x3, Refund x2;
        # Imprest against Journal Entry; Account Payment; the plant's three fixed-asset purposes, one per company;
        # Utilities' and Insurance's bills due within 2 days; every bill (AP_AGAINST_BILL); Mart's small imprests
        self.assertEqual(len(ROUTES), len(PAYMENT_PURPOSES) + 5 + 2 + 1 + 1 + 1 + 3 + 2 + 1 + 1)

    def test_codes_are_unique_and_fit_the_engine(self):
        codes = [r['code'] for r in ROUTES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(len(c) <= 60 for c in codes))

    def test_mart_and_staff_advance_are_kept_out_of_the_purpose_routes(self):
        mart = BY_CODE['AP_MART']['query']
        for part in ("company = 'MART'", "request_type NOT IN ('EXPENSE', 'ACCOUNT')", hierarchy.NOT_BILL,
                     f'NOT {hierarchy.FREIGHT_ROUTED}', f'NOT {hierarchy.MART_SMALL_IMPREST}'):
            self.assertIn(part, mart)
        self.assertIn("request_type IN ('EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST')",
                      BY_CODE['AP_STAFF_ADVANCE']['query'])
        for route in ROUTES:
            if route['code'] in ('AP_MART', 'AP_MART_IMPREST_SMALL', 'AP_IMPREST_JOURNAL', 'AP_ACCOUNT') or \
                    route['code'].startswith('AP_STAFF_ADVANCE'):
                continue
            with self.subTest(route['code']):
                self.assertNotIn('MART', route['query'])
                self.assertIn("request_type NOT IN ('EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST')", route['query'])

    def test_an_imprest_against_a_journal_entry_goes_to_the_head_then_navdeep_not_the_director(self):
        route = BY_CODE['AP_IMPREST_JOURNAL']
        self.assertEqual(route['stages'], [(DEPARTMENT_HEAD_STAGE, hierarchy.HEAD), (hierarchy.SECOND_STAGE, 'navdeep')])
        self.assertIn("request_type = 'EMPLOYEE_IMPREST' AND payment_against = 'AGAINST_JOURNAL'", route['query'])
        self.assertNotIn('MART', route['query'])
        # …and the Staff Advance / Imprest route (head, then Director) no longer takes it.
        self.assertIn(f"NOT {hierarchy.IMPREST_JOURNAL}", BY_CODE['AP_STAFF_ADVANCE']['query'])

    def test_salary_splits_on_the_factory_budgets_without_overlap(self):
        salary = {r['code']: r['query'] for r in _routes_for('SALARY')}
        self.assertEqual(set(salary), {'AP_SALARY', 'AP_SALARY_FACTORY_OIL', 'AP_SALARY_FACTORY_BEV'})
        self.assertIn("budget_code NOT IN ('Factory', 'FACT_COM')", salary['AP_SALARY'])
        self.assertIn("company = 'OIL'", salary['AP_SALARY_FACTORY_OIL'])
        self.assertIn("budget_code IN ('Factory', 'FACT_COM')", salary['AP_SALARY_FACTORY_BEV'])

    def test_a_customer_refund_goes_to_its_companys_sales_owner(self):
        refunds = {r['company']: r['stages'] for r in _routes_for('CUSTOMER_REFUND')}
        self.assertEqual(refunds, {'OIL': [(hierarchy.OWNER_STAGE, 'sales_oil')],
                                   'BEVERAGES': [(hierarchy.OWNER_STAGE, 'sales_bev')]})


class WhoApproves(SimpleTestCase):
    def first(self, purpose):
        # A bill due within 2 days has its own route; this is the usual one.
        [route] = [r for r in _routes_for(purpose) if not r['code'].endswith(hierarchy.DUE_SOON_SUFFIX)]
        return route['stages']

    def test_by_department_purposes_go_to_the_picked_department_head(self):
        for code in HEAD_PURPOSES:
            with self.subTest(code):
                self.assertEqual(self.first(code)[0], (DEPARTMENT_HEAD_STAGE, hierarchy.HEAD))
        self.assertEqual(BY_CODE['AP_STAFF_ADVANCE']['stages'],
                         [(DEPARTMENT_HEAD_STAGE, hierarchy.HEAD), (hierarchy.DIRECTOR_STAGE, 'director')])

    def test_every_freight_goes_to_navdeep_first_and_no_one_else(self):
        for code in ('FREIGHT_IN', 'FREIGHT_OUT', 'FREIGHT_IMPORT'):
            with self.subTest(code):
                self.assertEqual(self.first(code), [(hierarchy.FREIGHT_STAGE, 'navdeep')])
                self.assertEqual(BY_CODE[f'AP_{code}']['company'], 'ALL')
        self.assertEqual([r['code'] for r in ROUTES if any(role == 'himanshu' for _n, role in r['stages'])], [])

    def test_oil_purchase_goes_to_shunty_then_ziyaul(self):
        self.assertEqual(BY_CODE['AP_OIL_PURCHASE']['stages'],
                         [(hierarchy.OWNER_STAGE, 'shunty'), (hierarchy.SECOND_STAGE, 'ziyaul')])

    def test_incentive_and_ta_da_go_to_the_picked_head_then_navdeep(self):
        for code in ('AP_INCENTIVE', 'AP_TA_DA'):
            with self.subTest(code):
                self.assertEqual(BY_CODE[code]['stages'],
                                 [(DEPARTMENT_HEAD_STAGE, hierarchy.HEAD), (hierarchy.SECOND_STAGE, 'navdeep')])
                # Navdeep approves once, at CA (2026-10-20).
                self.assertEqual([name for name, _r in hierarchy.full_stages(BY_CODE[code])],
                                 ['Payment Approval', 'Department Head Approval', 'CA Approval',
                                  'Audit Approval', 'Final Approval'])

    def test_the_director_follows_where_the_hierarchy_says(self):
        with_director = sorted(r['code'] for r in ROUTES if (hierarchy.DIRECTOR_STAGE, 'director') in r['stages'])
        self.assertEqual(with_director, sorted([
            'AP_STAFF_ADVANCE', 'AP_STAFF_ADVANCE_FACTORY_HIGH', 'AP_FA_CIVIL',
            'AP_FA_PM_OIL', 'AP_FA_PM_BEV', 'AP_FA_CONSUMABLES_OIL', 'AP_FA_CONSUMABLES_BEV',
            'AP_FA_OTHERS_OIL', 'AP_FA_OTHERS_BEV', 'AP_CAPITAL', 'AP_UTILITIES', 'AP_INSURANCE',
            'AP_EMP_ADVANCE', 'AP_EMP_IMPREST', 'AP_EXPENSE_CLAIM']))

    def test_the_plants_fixed_assets_go_to_its_approver_civil_to_bhupinder(self):
        # Plant & machinery, spares, others: Oil -> Gagan, Beverages -> Arvinder, then the Director.
        for code in ('FA_PM', 'FA_CONSUMABLES', 'FA_OTHERS'):
            with self.subTest(code):
                oil, bev = BY_CODE[f'AP_{code}_OIL'], BY_CODE[f'AP_{code}_BEV']
                self.assertIn("company = 'OIL'", oil['query'])
                self.assertIn("company = 'BEVERAGES'", bev['query'])
                self.assertEqual(oil['stages'], [(hierarchy.OWNER_STAGE, 'factory_oil'),
                                                 (hierarchy.DIRECTOR_STAGE, 'director')])
                self.assertEqual(bev['stages'], [(hierarchy.OWNER_STAGE, 'factory_bev'),
                                                 (hierarchy.DIRECTOR_STAGE, 'director')])
                self.assertNotIn(f'AP_{code}', BY_CODE)
        self.assertEqual(self.first('FA_CIVIL'), [(hierarchy.OWNER_STAGE, 'bhupinder'),
                                                  (hierarchy.DIRECTOR_STAGE, 'director')])
        self.assertFalse(needs_department_head('OIL', 'VENDOR', 'FA_OTHERS'))
        self.assertEqual(set(hierarchy.RETIRED_ROUTES), {'AP_FA_PM', 'AP_FA_CONSUMABLES', 'AP_FA_OTHERS'})

    def test_ghee_and_other_raw_material_go_to_bhupinder(self):
        self.assertEqual(self.first('RAW_MATERIAL'), [(hierarchy.OWNER_STAGE, 'bhupinder')])

    def test_every_route_starts_at_payment_and_ends_ca_audit_final(self):
        for route in ROUTES:
            with self.subTest(route['code']):
                stages = hierarchy.full_stages(route)
                self.assertEqual(stages[0], ('Payment Approval', 'payment'))
                self.assertEqual([r for _n, r in stages[-3:]], ['ca', 'audit', 'final'])
                named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in
                                            enumerate(stages, start=1)])
                roles = [r for _s, r in named]
                self.assertEqual(roles[0], StageRole.PAYMENT)
                self.assertEqual(roles[-3:], [StageRole.CA, StageRole.AUDIT, StageRole.FINAL])
                self.assertTrue(all(r == StageRole.APPROVAL for r in roles[1:-3]))


def _po_request(**overrides):
    """A vendor advance against a PO: still routed by its purpose."""
    return vendor_bill_request(payment_against='AGAINST_PO', documents=[
        {'kind': 'PO', 'sap_doc_entry': 14008, 'open_amount': '350000', 'amount': '1000'}], **overrides)


class TheFormNamesTheDepartmentHead(SimpleTestCase):
    def test_who_needs_one(self):
        self.assertTrue(needs_department_head('OIL', 'VENDOR', 'RENT'))
        self.assertTrue(needs_department_head('BEVERAGES', 'EMPLOYEE_IMPREST', 'OIL_PURCHASE'))
        self.assertFalse(needs_department_head('OIL', 'VENDOR', 'OIL_PURCHASE'))
        self.assertFalse(needs_department_head('MART', 'EMPLOYEE_ADVANCE', 'RENT'))  # Mart: one approver

    def test_a_by_department_purpose_names_an_hod_with_a_login(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Choose the Department Head'):
            _clean(_po_request(purpose_code='RENT'))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not an active HOD'):
            _clean(_po_request(purpose_code='RENT', department_head_code='JWPL9999'))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Arshdeep Singh .JWPL3005. has no OMS login'):
            _clean(_po_request(purpose_code='RENT', department_head_code='JWPL3005'))
        cleaned = _clean(_po_request(purpose_code='RENT', department_head_code='temp0001'))
        self.assertEqual(cleaned.fields['department_head_employee'].employee_code, 'TEMP0001')
        self.assertEqual(cleaned.fields['department_head'].username, 'nirmal')  # who approves

    def test_a_fixed_owner_purpose_drops_a_head_sent_anyway(self):
        cleaned = _clean(vendor_bill_request(purpose_code='OIL_PURCHASE', department_head_code='TEMP0001'))
        self.assertIsNone(cleaned.fields['department_head'])
        self.assertIsNone(cleaned.fields['department_head_employee'])

    def test_a_retired_purpose_can_no_longer_be_chosen(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not a Payment Purpose'):
            _clean(vendor_bill_request(purpose_code='FIXED_ASSETS'))


def head_stage(pk=1, sequence=1):
    return SimpleNamespace(id=pk, pk=pk, name=' department  head APPROVAL ', sequence=sequence, user_id=900)


class TheDepartmentHeadStage(SimpleTestCase):
    def test_is_named_exactly(self):
        self.assertTrue(is_department_head_stage(DEPARTMENT_HEAD_STAGE))
        self.assertFalse(is_department_head_stage('HOD Approval'))

    def test_goes_to_the_picked_head_or_their_stand_in(self):
        advance = SimpleNamespace(department_head_id=7)
        with mock.patch.object(flow_service.replacements, 'effective_user_ids', return_value={7: 8}), \
                mock.patch.object(flow_service, 'effective_user_id') as configured:
            self.assertEqual(flow_service.stage_user_id(advance, head_stage()), 8)
        configured.assert_not_called()  # the placeholder user is never asked

    def test_other_stages_go_to_their_configured_user(self):
        advance = SimpleNamespace(department_head_id=7)
        with mock.patch.object(flow_service, 'effective_user_id', return_value=42):
            self.assertEqual(flow_service.stage_user_id(
                advance, SimpleNamespace(id=5, name='Director Approval')), 42)

    def test_a_route_with_the_stage_needs_a_picked_head(self):
        advance = SimpleNamespace(pk=1, company='OIL', department_head_id=None)
        stages = [head_stage(1, 1), SimpleNamespace(id=2, name='Payment Approval', sequence=2),
                  SimpleNamespace(id=3, name='Audit Approval', sequence=3),
                  SimpleNamespace(id=4, name='Final Approval', sequence=4)]
        with mock.patch.object(flow_service.selection, 'select_for_module',
                               return_value=SimpleNamespace(stages=stages)):
            with self.assertRaisesRegex(flow_service.FlowError, 'choose the Department Head'):
                flow_service._select(advance)


class TheDirectorPickedAsHeadApprovesOnce(SimpleTestCase):
    def setUp(self):
        self.director = SimpleNamespace(id=2, pk=2, name='Director Approval', sequence=2, user_id=50)
        self.payment = SimpleNamespace(id=3, pk=3, name='Payment Approval', sequence=3, user_id=60)
        self.flow = SimpleNamespace(request=SimpleNamespace(department_head_id=50), current_stage_id=1,
                                    current_role='APPROVAL', current_user_id=50)

    def test_the_next_stage_of_the_same_person_is_recorded_and_passed(self):
        with mock.patch.object(flow_service, 'stage_user_id', return_value=50), \
                mock.patch.object(flow_service, 'log') as log, \
                mock.patch.object(flow_service, '_next', return_value=(self.payment, StageRole.PAYMENT)):
            following = flow_service._approve_once(self.flow, (self.director, StageRole.APPROVAL),
                                                   user=SimpleNamespace(pk=50))
        self.assertEqual(following, (self.payment, StageRole.PAYMENT))
        self.assertEqual(log.call_args.kwargs['data'], {'same_as_department_head': True})
        self.assertEqual(self.flow.current_stage_id, 2)

    def test_someone_else_next_waits_for_them(self):
        with mock.patch.object(flow_service, 'stage_user_id', return_value=51), \
                mock.patch.object(flow_service, 'log') as log:
            following = flow_service._approve_once(self.flow, (self.director, StageRole.APPROVAL),
                                                   user=SimpleNamespace(pk=50))
        self.assertEqual(following, (self.director, StageRole.APPROVAL))
        log.assert_not_called()

    def test_payment_is_never_skipped(self):
        with mock.patch.object(flow_service, 'stage_user_id', return_value=50), \
                mock.patch.object(flow_service, 'log') as log:
            following = flow_service._approve_once(self.flow, (self.payment, StageRole.PAYMENT),
                                                   user=SimpleNamespace(pk=50))
        self.assertEqual(following, (self.payment, StageRole.PAYMENT))
        log.assert_not_called()


def user(pk, username, name, email=''):
    return SimpleNamespace(pk=pk, username=username, name=name, email=email)


def hod(name, email=''):
    return SimpleNamespace(employee_code='X', employee_name=name, email=email)


#: The test server's logins, as the HODs of the employee master meet them.
USERS = [
    user(92, 'gagan', 'Gagan'), user(143, 'gagan1', 'gagan_P&D'), user(147, 'gagan2', 'gagan_bev'),
    user(26795, 'Raju Vg', 'Jasvir Singh (Raju)'), user(26802, 'prabhjot', 'Prabhjot Singh'),
    user(26797, 'bhupinder', 'Bhupinder Singh', 'bhupinder@oms.com'), user(26794, 'nirmal', 'Nirmal Didi Ji'),
    user(26667, 'gurvinder', 'Gurvinder'), user(26668, 'Gurpreet Vg', 'Gurpreet Singh'),
]


class AnHodIsMatchedToTheirLogin(SimpleTestCase):
    def match(self, employee):
        found = heads.match_user(employee, USERS)
        return found.username if found else None

    def test_by_email_then_name_then_distinctive_words(self):
        self.assertEqual(self.match(hod('Someone Else', 'BHUPINDER@oms.com')), 'bhupinder')
        self.assertEqual(self.match(hod('Bhupinder Singh')), 'bhupinder')
        self.assertEqual(self.match(hod('Nirmal Didi')), 'nirmal')
        self.assertEqual(self.match(hod('Jasbir Singh Raju')), 'Raju Vg')
        self.assertEqual(self.match(hod('Prabhu')), 'prabhjot')            # a shared start: Prabh-
        self.assertEqual(self.match(hod('Gurvinderjeet Singh')), 'gurvinder')

    def test_a_username_outranks_the_same_word_in_a_name(self):
        self.assertEqual(self.match(hod('Gagan Vg')), 'gagan')            # not gagan1 "gagan_P&D"

    def test_courtesy_words_alone_match_no_one(self):
        self.assertIsNone(self.match(hod('Veerji')))
        self.assertIsNone(self.match(hod('Singh Vg')))                    # not Gurpreet Vg / Raju Vg

    def test_a_known_login_wins_by_employee_code(self):
        veerji = SimpleNamespace(employee_code='TEMP0002', employee_name='Veerji', email='')
        self.assertEqual(self.match(veerji), 'Gurpreet Vg')               # Veerji is Gurpreet Ji
        shunty = SimpleNamespace(employee_code='JWPL0035', employee_name='Shunty Veerji Accounts', email='')
        self.assertIsNone(self.match(shunty))                             # the word alone is not him

    def test_gagan_vg_is_the_approver_login_not_gagan_adv(self):
        gagan_vg = SimpleNamespace(employee_code='JWPL0018', employee_name='Gagan Vg', email='')
        live = [user(45, 'gagan', 'Gagan'), user(132, 'gagan adv', 'Gagan')]  # a tie by name on live
        self.assertEqual(heads.match_user(gagan_vg, live).username, 'gagan')

    def test_a_tie_is_no_match(self):
        twins = [user(1, 'amit.k', 'Amit Kumar'), user(2, 'amit.s', 'Amit Sharma')]
        self.assertIsNone(heads.match_user(hod('Amit'), twins))


class ExpenseRoutes(SimpleTestCase):
    """Expense requests (2026-10-09): the budget head's owner, then the Director,
    then Payment -> Expense Audit (Bhavani, not the payments auditor), which posts."""

    def test_one_route_per_head_and_one_for_mart(self):
        heads = sum(len(h) for h in hierarchy.EXPENSE_OWNERS.values())
        self.assertEqual(len(EXPENSE_ROUTES), heads + 1)
        codes = [r['code'] for r in ALL_ROUTES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(len(c) <= 60 for c in codes))
        # The old per-electricity workflows are retired, none of them still a route.
        retired = hierarchy.retired_expense_routes()
        self.assertEqual(len(retired), heads)
        self.assertFalse(set(retired) & set(codes))
        self.assertIn('AP_EXP_OIL_FACTORY_ELEC', retired)

    def test_every_expense_route_ends_payment_then_its_own_audit_which_posts(self):
        for route in EXPENSE_ROUTES:
            with self.subTest(route['code']):
                stages = hierarchy.full_stages(route)
                self.assertEqual(stages[0], ('Payment Approval', 'payment'))
                self.assertEqual(stages[-2:], [(hierarchy.CA_STAGE, 'ca'), (EXPENSE_AUDIT_STAGE, 'expense_audit')])
                named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in
                                            enumerate(stages, start=1)])
                self.assertEqual(named[-1][1], StageRole.AUDIT)
                self.assertEqual(flow_service.posting_role(named), StageRole.AUDIT)
        self.assertEqual(hierarchy.PEOPLE['expense_audit'], 'bhavani')
        self.assertNotEqual(hierarchy.PEOPLE['expense_audit'], hierarchy.PEOPLE['audit'])

    def test_who_approves_an_expense(self):
        director = (hierarchy.DIRECTOR_STAGE, 'director')
        self.assertEqual(BY_CODE['AP_EXP_OIL_FACTORY']['stages'], [(hierarchy.OWNER_STAGE, 'factory_oil'), director])
        self.assertEqual(BY_CODE['AP_EXP_BEV_FACTORY']['stages'], [(hierarchy.OWNER_STAGE, 'factory_bev'), director])
        self.assertEqual(BY_CODE['AP_EXP_OIL_DEL_BKHP']['stages'], [(hierarchy.OWNER_STAGE, 'bhupinder'), director])
        self.assertEqual(BY_CODE['AP_EXP_OIL_NPD1']['stages'], [(hierarchy.OWNER_STAGE, 'avtar'), director])
        # R & D / OTE have no owner: the Director, once.
        self.assertEqual(BY_CODE['AP_EXP_OIL_R_D']['stages'], [director])
        # Mart: Prabhjot.
        self.assertEqual(BY_CODE['AP_EXP_MART']['stages'], [(hierarchy.OWNER_STAGE, 'mart')])

    def test_an_expense_matches_only_its_own_route(self):
        q = BY_CODE['AP_EXP_OIL_SALES_RE']['query']
        for part in ("request_type = 'EXPENSE'", "company = 'OIL'", "budget_code = 'Sales RE'"):
            self.assertIn(part, q)
        self.assertNotIn('is_electricity', q)  # electricity no longer changes the route
        # Payment routes need a purpose an Expense never has, and Mart's excludes it.
        for route in ROUTES:
            if route['code'] in ('AP_MART', 'AP_MART_IMPREST_SMALL', 'AP_IMPREST_JOURNAL', 'AP_ACCOUNT',
                                 'AP_AGAINST_BILL') or route['code'].startswith('AP_STAFF_ADVANCE'):
                continue
            self.assertIn('purpose_code = ', route['query'])

    def test_the_expense_auditors_stage_is_an_audit_stage_by_its_own_name(self):
        self.assertEqual(StageRole.of('Expense Audit Approval'), StageRole.AUDIT)
        self.assertEqual(StageRole.of('expense  audit approval'), StageRole.AUDIT)
        self.assertEqual(StageRole.of('Audit Approval'), StageRole.AUDIT)

    def test_a_payment_route_without_final_is_refused_but_payment_audit_is_an_expense_tail(self):
        stage = lambda n, i: SimpleNamespace(name=n, sequence=i)  # noqa: E731
        with self.assertRaises(flow_service.FlowError):
            flow_service.roles([stage('Budget Owner Approval', 1), stage('Payment Approval', 2)])
        named = flow_service.roles([stage('Budget Owner Approval', 1), stage('Payment Approval', 2),
                                    stage('Audit Approval', 3)])
        self.assertEqual(flow_service.posting_role(named), StageRole.AUDIT)
        named = flow_service.roles([stage('Payment Approval', 1), stage('Audit Approval', 2),
                                    stage('Final Approval', 3)])
        self.assertEqual(flow_service.posting_role(named), StageRole.FINAL)


class StaffSalaryAdvance(SimpleTestCase):
    """The approval matrix of 2026-10-09: a plant's staff salary advance goes to the
    plant's approver up to 20,000, the Director above; elsewhere, and Imprest, as before."""

    def test_the_plants_approver_up_to_the_limit_the_director_above(self):
        self.assertEqual(BY_CODE['AP_STAFF_ADVANCE_FACTORY_OIL']['stages'], [(hierarchy.OWNER_STAGE, 'shunty')])
        self.assertEqual(BY_CODE['AP_STAFF_ADVANCE_FACTORY_BEV']['stages'], [(hierarchy.OWNER_STAGE, 'factory_bev')])
        self.assertEqual(hierarchy.PEOPLE['factory_bev'], 'arvinder')
        self.assertEqual(BY_CODE['AP_STAFF_ADVANCE_FACTORY_HIGH']['stages'],
                         [(hierarchy.DIRECTOR_STAGE, 'director')])
        self.assertIn('amount <= 20000', BY_CODE['AP_STAFF_ADVANCE_FACTORY_OIL']['query'])
        self.assertIn('amount > 20000', BY_CODE['AP_STAFF_ADVANCE_FACTORY_HIGH']['query'])
        for code in ('AP_STAFF_ADVANCE_FACTORY_OIL', 'AP_STAFF_ADVANCE_FACTORY_BEV', 'AP_STAFF_ADVANCE_FACTORY_HIGH'):
            self.assertIn("request_type = 'EMPLOYEE_ADVANCE'", BY_CODE[code]['query'])
            self.assertIn("budget_code IN ('Factory', 'FACT_COM')", BY_CODE[code]['query'])

    def test_imprest_and_advances_outside_the_plant_stay_with_the_department_head(self):
        q = BY_CODE['AP_STAFF_ADVANCE']['query']
        self.assertIn("(request_type = 'EMPLOYEE_IMPREST' OR budget_code NOT IN ('Factory', 'FACT_COM'))", q)
        self.assertEqual(BY_CODE['AP_STAFF_ADVANCE']['stages'],
                         [(DEPARTMENT_HEAD_STAGE, hierarchy.HEAD), (hierarchy.DIRECTOR_STAGE, 'director')])

    def test_no_department_head_is_asked_for_a_plants_salary_advance(self):
        self.assertFalse(needs_department_head('OIL', 'EMPLOYEE_ADVANCE', 'EMP_ADVANCE', 'Factory'))
        self.assertFalse(needs_department_head('BEVERAGES', 'EMPLOYEE_ADVANCE', 'EMP_ADVANCE', 'FACT_COM'))
        self.assertTrue(needs_department_head('OIL', 'EMPLOYEE_ADVANCE', 'EMP_ADVANCE', 'BackOff'))
        self.assertTrue(needs_department_head('OIL', 'EMPLOYEE_IMPREST', 'EMP_IMPREST', 'Factory'))

    def test_beverages_sales_is_rajus(self):
        self.assertEqual(hierarchy.PEOPLE['sales_bev'], 'Raju Vg')
        self.assertEqual(BY_CODE['AP_EXP_BEV_SALES']['stages'],
                         [(hierarchy.OWNER_STAGE, 'sales_bev'), (hierarchy.DIRECTOR_STAGE, 'director')])


class HeadsTwoLoginsShare(SimpleTestCase):
    """Where two logins share an HOD's name, the matcher will not guess; the code is pinned."""

    def login(self, pk, username, name):
        return SimpleNamespace(pk=pk, username=username, name=name, email=f'{pk}@x.in')

    def test_each_pinned_hod_goes_to_its_own_login_not_the_namesake(self):
        users = [
            self.login(138, 'Shunty Vg', 'Shunty Veerji'), self.login(144, 'Shunty Ary', 'Shunty Ary'),
            self.login(123, 'bhupinder', 'Bhupinder Singh'), self.login(143, 'bhupinder payment', 'Bhupinder Singh'),
            self.login(104, 'prabhjot', 'Prabhjot Singh'), self.login(43, 'raunak', 'Prabhjot singh raunak'),
        ]
        for code, name, pk in (('JWPL0035', 'Shunty Veerji Accounts', 138),
                               ('JWPL1556', 'Bhupinder Singh', 123),
                               ('JWPL2217', 'Prabhu', 104)):
            employee = SimpleNamespace(employee_code=code, employee_name=name, email=None)
            with self.subTest(code=code):
                self.assertEqual(heads.match_user(employee, users).pk, pk)

    def test_unpinned_namesakes_are_still_no_match(self):
        users = [self.login(1, 'asha a', 'Asha Rani'), self.login(2, 'asha b', 'Asha Devi')]
        employee = SimpleNamespace(employee_code='JWPL9999', employee_name='Asha', email=None)
        self.assertIsNone(heads.match_user(employee, users))


class TheTimelineWhenTheHeadIsTheDirector(SimpleTestCase):
    """The Director picked as Department Head approves once: the timeline must not show a second approval to come."""

    def plan(self, director_id, state='UPCOMING'):
        return [
            {'name': DEPARTMENT_HEAD_STAGE, 'role': 'APPROVAL', 'user_id': 137, 'state': 'CURRENT'},
            {'name': 'Director Approval', 'role': 'APPROVAL', 'user_id': director_id, 'state': state},
            {'name': 'Payment Approval', 'role': 'PAYMENT', 'user_id': 134, 'state': 'UPCOMING'},
        ]

    def test_the_same_persons_next_stage_goes_with_the_heads(self):
        plan = flow_service.mark_with_head(self.plan(137))
        self.assertEqual([s['state'] for s in plan], ['CURRENT', 'WITH_HEAD', 'UPCOMING'])

    def test_someone_elses_next_stage_is_still_to_come(self):
        plan = flow_service.mark_with_head(self.plan(126))
        self.assertEqual(plan[1]['state'], 'UPCOMING')

    def test_a_stage_already_decided_keeps_its_state(self):
        plan = flow_service.mark_with_head(self.plan(137, state='APPROVED'))
        self.assertEqual(plan[1]['state'], 'APPROVED')


class BillsDueSoon(SimpleTestCase):
    """Utilities and insurance due within 2 days go straight to Payment (user, 2026-10-17).

    Due 20 Oct: raised on the 18th or before, Department Head then Director;
    on the 19th or later (or overdue), straight to Payment Approval. The date
    is the Bill Due Date the requester types.
    """

    def test_each_purpose_has_the_usual_route_and_one_straight_to_payment(self):
        for code in ('UTILITIES', 'INSURANCE'):
            with self.subTest(code):
                routes = {r['code']: r for r in _routes_for(code)}
                self.assertEqual(set(routes), {f'AP_{code}', f'AP_{code}_DUE_SOON'})
                self.assertEqual(routes[f'AP_{code}']['stages'],
                                 [(DEPARTMENT_HEAD_STAGE, hierarchy.HEAD), (hierarchy.DIRECTOR_STAGE, 'director')])
                self.assertEqual(routes[f'AP_{code}_DUE_SOON']['stages'], [])
                self.assertEqual([n for n, _r in hierarchy.full_stages(routes[f'AP_{code}_DUE_SOON'])],
                                 ['Payment Approval', 'CA Approval', 'Audit Approval', 'Final Approval'])

    def test_the_two_routes_never_both_match(self):
        for code in ('UTILITIES', 'INSURANCE'):
            with self.subTest(code):
                usual, soon = BY_CODE[f'AP_{code}']['query'], BY_CODE[f'AP_{code}_DUE_SOON']['query']
                self.assertTrue(usual.endswith(f'AND NOT {hierarchy.BILL_DUE_SOON}'))
                self.assertTrue(soon.endswith(f'AND {hierarchy.BILL_DUE_SOON}'))
                self.assertEqual(usual.replace(f'NOT {hierarchy.BILL_DUE_SOON}', ''),
                                 soon.replace(hierarchy.BILL_DUE_SOON, ''))

    def test_due_soon_is_the_typed_date_before_today_plus_two_in_india(self):
        self.assertEqual(hierarchy.BILL_DUE_SOON,
                         "(bill_due_date IS NOT NULL "
                         "AND bill_due_date <= (now() AT TIME ZONE 'Asia/Kolkata')::date + 2)")
        self.assertEqual(purposes.DUE_SOON_DAYS, 2)

    def test_only_these_routes_look_at_the_due_date(self):
        with_rule = sorted(r['code'] for r in ALL_ROUTES if 'due_date' in r['query'])
        self.assertEqual(with_rule, ['AP_INSURANCE', 'AP_INSURANCE_DUE_SOON',
                                     'AP_UTILITIES', 'AP_UTILITIES_DUE_SOON'])

    def test_every_due_soon_purpose_is_a_department_head_one(self):
        self.assertTrue(purposes.DUE_SOON_PURPOSES <= HEAD_PURPOSES)
        self.assertEqual(purposes.DUE_SOON_PURPOSES, {'UTILITIES', 'INSURANCE'})

    def test_the_route_starts_at_payment(self):
        stages = hierarchy.full_stages(BY_CODE['AP_INSURANCE_DUE_SOON'])
        named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in
                                    enumerate(stages, start=1)])
        self.assertEqual(named[0][1], StageRole.PAYMENT)
        self.assertFalse(any(is_department_head_stage(s.name) for s, _r in named))

    def test_the_list_says_which_purposes_take_a_due_date(self):
        flagged = sorted(p['code'] for p in purposes.purposes() if p['due_date'])
        self.assertEqual(flagged, ['INSURANCE', 'UTILITIES'])
        self.assertEqual(purposes.purpose_label('INSURANCE'), 'Insurance Premium')


def _due_request(purpose='UTILITIES', **overrides):
    """A vendor paid on account for `purpose`, with its Bill Due Date."""
    data = vendor_bill_request(purpose_code=purpose, department_head_code='TEMP0001',
                               payment_against='ON_ACCOUNT', documents=[], amount='45886',
                               bill_due_date='2026-10-20')
    data.update(overrides)
    return data


def _problems(data):
    try:
        _clean(data)
    except request_service.RequestInvalid as exc:
        return exc.problems
    return []


class TheBillDueDate(SimpleTestCase):
    """The requester types it; the bill or policy showing it is attached."""

    def setUp(self):
        # Five days before the sample due date (20 Oct): the full route, so a head is asked.
        import datetime
        from advance_payment.services import clock
        patcher = mock.patch.object(clock, 'today', return_value=datetime.date(2026, 10, 15))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_utilities_and_insurance_on_account_carry_it(self):
        for purpose in ('UTILITIES', 'INSURANCE'):
            with self.subTest(purpose):
                cleaned = _clean(_due_request(purpose))
                self.assertEqual(str(cleaned.fields['bill_due_date']), '2026-10-20')
                self.assertEqual(cleaned.fields['payment_against'], 'ON_ACCOUNT')
                self.assertEqual(cleaned.documents, [])

    def test_it_is_required_and_may_be_past(self):
        self.assertIn("Enter the Bill Due Date: the due date on the bill, or the policy's renewal date.",
                      _problems(_due_request(bill_due_date='')))
        self.assertEqual(_problems(_due_request(bill_due_date='2026-09-01')), [])  # overdue: allowed

    def test_against_a_bill_it_is_not_asked(self):
        # Every bill goes straight to Payment (2026-10-19): its due date decides nothing.
        cleaned = _clean(vendor_bill_request(purpose_code='UTILITIES', bill_due_date='2026-10-25'))
        self.assertIsNone(cleaned.fields['bill_due_date'])

    def test_other_purposes_are_not_asked_and_keep_none(self):
        cleaned = _clean(vendor_bill_request(bill_due_date='2026-10-20'))
        self.assertIsNone(cleaned.fields['bill_due_date'])

    def test_who_is_asked(self):
        ask = purposes.needs_bill_due_date
        self.assertTrue(ask('OIL', 'VENDOR', 'INSURANCE'))
        self.assertTrue(ask('BEVERAGES', 'VENDOR', 'UTILITIES'))
        self.assertFalse(ask('MART', 'VENDOR', 'UTILITIES'))          # Prabhjot, whatever it is for
        self.assertFalse(ask('OIL', 'EMPLOYEE_IMPREST', 'UTILITIES'))  # the Employee / Imprest route
        self.assertFalse(ask('OIL', 'VENDOR', 'RENT'))

    def test_on_account_is_only_for_utilities_and_insurance(self):
        problems = _problems(_due_request('RENT', bill_due_date=''))
        self.assertIn('A vendor is paid On Account only for Electricity, Water & Utilities or an '
                      'Insurance Premium. Pay against its bill or its PO.', problems)

    def test_raising_one_needs_the_bill_or_policy_attached(self):
        cleaned = _clean(_due_request())
        with mock.patch.object(request_service, 'check_files'):
            with self.assertRaises(request_service.RequestInvalid) as caught:
                request_service.create(cleaned, user=SimpleNamespace(pk=1), files=[])
        self.assertEqual(caught.exception.problems, [request_service.DUE_DATE_PROOF])

    def test_an_edit_may_not_remove_the_last_attachment(self):
        cleaned = _clean(_due_request())
        advance = mock.MagicMock()
        advance.files.filter.return_value.exclude.return_value.exists.return_value = False
        with mock.patch.object(request_service, 'check_files'), \
                mock.patch.object(request_service, '_reserve') as reserve:
            with self.assertRaises(request_service.RequestInvalid) as caught:
                request_service.update(advance, cleaned, user=SimpleNamespace(pk=1), files=[],
                                       remove_file_ids=[9])
        self.assertEqual(caught.exception.problems, [request_service.DUE_DATE_PROOF])
        reserve.assert_not_called()  # refused before anything is written
        advance.files.filter.return_value.exclude.assert_called_once_with(pk__in=[9])


class APremiumPaidOnAccount(SimpleTestCase):
    """Posted as SAP pays insurers and electricity boards: to the vendor, on account."""

    def test_posts_to_the_vendor_with_no_bill_and_a_settlement_date(self):
        from advance_payment.services import voucher as voucher_service
        from advance_payment.tests_requests import DAY, advance_of, line, payout

        advance = advance_of('VENDOR', amount='873200', partner='VENDA000356')
        body = voucher_service.build_payload(advance, payout(line(1, 'RTGS', '873200')), posting_date=DAY,
                                             series=2601, bpl_id=1, memo='OMS AP-2026-0201/1')
        self.assertEqual((body['DocType'], body['CardCode']), ('rSupplier', 'VENDA000356'))
        self.assertNotIn('PaymentInvoices', body)
        self.assertEqual(body['U_Type_of_Advance'], 'One Time Settlement')
        self.assertEqual(body['U_Adv_Settl_Dt'], '2026-10-25')  # 30 days on, as SAP's own (10 to 25 days)


class DueSoonSkipsTheHead(SimpleTestCase):
    """Due within 2 days, or overdue: no Department Head, paid the day it is raised (user, 2026-10-19).
    Due later: the Department Head the requester picks, then the Director."""

    def clean_on(self, today, **overrides):
        import datetime
        from advance_payment.services import clock
        with mock.patch.object(clock, 'today', return_value=datetime.date.fromisoformat(today)):
            return _clean(_due_request(**overrides))

    def test_due_within_two_days_drops_the_head_and_pays_today(self):
        for today in ('2026-10-18', '2026-10-19', '2026-10-20', '2026-10-25'):  # 2 days, 1 day, the day, overdue
            with self.subTest(today):
                cleaned = self.clean_on(today, payment_date='2026-12-31', department_head_code='')
                self.assertIsNone(cleaned.fields['department_head'])
                self.assertIsNone(cleaned.fields['department_head_employee'])
                self.assertEqual(str(cleaned.fields['payment_date']), today)

    def test_a_head_sent_anyway_is_dropped(self):
        cleaned = self.clean_on('2026-10-19', department_head_code='TEMP0001')
        self.assertIsNone(cleaned.fields['department_head'])

    def test_more_than_two_days_ahead_needs_the_head_and_still_pays_today_on_account(self):
        cleaned = self.clean_on('2026-10-17', payment_date='2026-10-19')  # due in 3 days
        self.assertIsNotNone(cleaned.fields['department_head_employee'])
        # Vendor On Account: never asked, always the day it is raised.
        self.assertEqual(str(cleaned.fields['payment_date']), '2026-10-17')
        with self.assertRaises(request_service.RequestInvalid) as caught:
            self.clean_on('2026-10-17', department_head_code='')
        self.assertIn('Choose the Department Head who approves this request.', caught.exception.problems)

    def test_the_rule(self):
        import datetime
        due, day = datetime.date(2026, 10, 20), datetime.date.fromisoformat
        self.assertFalse(purposes.is_due_soon(due, day('2026-10-16')))
        self.assertFalse(purposes.is_due_soon(due, day('2026-10-17')))
        self.assertTrue(purposes.is_due_soon(due, day('2026-10-18')))
        self.assertTrue(purposes.is_due_soon(due, day('2026-10-19')))
        self.assertTrue(purposes.is_due_soon(due, day('2026-10-21')))
        self.assertFalse(purposes.is_due_soon(None, day('2026-10-19')))



class AgainstABillThePaymentDateIsStillAsked(SimpleTestCase):
    """Only On Account (and a bill due soon) is paid the day it is raised."""

    def test_a_utility_bill_keeps_its_typed_payment_date_and_names_no_head(self):
        import datetime
        from advance_payment.services import clock
        with mock.patch.object(clock, 'today', return_value=datetime.date(2026, 10, 15)):
            cleaned = _clean(vendor_bill_request(purpose_code='UTILITIES', department_head_code='TEMP0001',
                                                 bill_due_date='2026-10-25', payment_date='2026-10-22'))
        self.assertEqual(str(cleaned.fields['payment_date']), '2026-10-22')
        self.assertIsNone(cleaned.fields['department_head_employee'])
