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

from advance_payment import hierarchy
from advance_payment.models import DEPARTMENT_HEAD_STAGE, StageRole, is_department_head_stage
from advance_payment.purposes import HEAD_PURPOSES, PAYMENT_PURPOSES, needs_department_head
from advance_payment.services import flow as flow_service
from advance_payment.services import heads
from advance_payment.services import requests as request_service
from advance_payment.tests_requests import _clean, vendor_bill_request

ALL_ROUTES = hierarchy.routes()
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
        self.assertEqual(len(ROUTES), len(PAYMENT_PURPOSES) + 2 + 2 + 1)  # +Mart, Staff; Salary x3, Refund x2

    def test_codes_are_unique_and_fit_the_engine(self):
        codes = [r['code'] for r in ROUTES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(len(c) <= 60 for c in codes))

    def test_mart_and_staff_advance_are_kept_out_of_the_purpose_routes(self):
        self.assertEqual(BY_CODE['AP_MART']['query'],
                         "SELECT id FROM advance_payment_request WHERE company = 'MART' AND request_type <> 'EXPENSE'")
        self.assertIn("request_type IN ('EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST')",
                      BY_CODE['AP_STAFF_ADVANCE']['query'])
        for route in ROUTES:
            if route['code'] in ('AP_MART', 'AP_STAFF_ADVANCE'):
                continue
            with self.subTest(route['code']):
                self.assertNotIn('MART', route['query'])
                self.assertIn("request_type NOT IN ('EMPLOYEE_ADVANCE', 'EMPLOYEE_IMPREST')", route['query'])

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
        [route] = _routes_for(purpose)
        return route['stages']

    def test_by_department_purposes_go_to_the_picked_department_head(self):
        for code in HEAD_PURPOSES:
            with self.subTest(code):
                self.assertEqual(self.first(code)[0], (DEPARTMENT_HEAD_STAGE, hierarchy.HEAD))
        self.assertEqual(BY_CODE['AP_STAFF_ADVANCE']['stages'],
                         [(DEPARTMENT_HEAD_STAGE, hierarchy.HEAD), (hierarchy.DIRECTOR_STAGE, 'director')])

    def test_only_oil_and_import_expenses_go_to_himanshu(self):
        himanshu = sorted(r['code'] for r in ROUTES if ('Budget Owner Approval', 'himanshu') in r['stages'])
        self.assertEqual(himanshu, ['AP_FREIGHT_IMPORT', 'AP_OIL_PURCHASE'])

    def test_the_director_follows_where_the_hierarchy_says(self):
        with_director = sorted(r['code'] for r in ROUTES if (hierarchy.DIRECTOR_STAGE, 'director') in r['stages'])
        self.assertEqual(with_director, sorted([
            'AP_STAFF_ADVANCE', 'AP_OIL_PURCHASE', 'AP_FREIGHT_IMPORT', 'AP_FA_PM', 'AP_FA_CIVIL',
            'AP_FA_OTHERS', 'AP_FA_CONSUMABLES', 'AP_CAPITAL', 'AP_UTILITIES',
            'AP_EMP_ADVANCE', 'AP_EMP_IMPREST', 'AP_EXPENSE_CLAIM']))

    def test_ghee_and_other_raw_material_go_to_bhupinder(self):
        self.assertEqual(self.first('RAW_MATERIAL'), [(hierarchy.OWNER_STAGE, 'bhupinder')])

    def test_every_route_ends_payment_audit_final(self):
        for route in ROUTES:
            with self.subTest(route['code']):
                stages = hierarchy.full_stages(route)
                self.assertEqual([r for _n, r in stages[-3:]], ['payment', 'audit', 'final'])
                named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in
                                            enumerate(stages, start=1)])
                self.assertEqual([r for _s, r in named][-3:], [StageRole.PAYMENT, StageRole.AUDIT, StageRole.FINAL])


class TheFormNamesTheDepartmentHead(SimpleTestCase):
    def test_who_needs_one(self):
        self.assertTrue(needs_department_head('OIL', 'VENDOR', 'RENT'))
        self.assertTrue(needs_department_head('BEVERAGES', 'EMPLOYEE_IMPREST', 'OIL_PURCHASE'))
        self.assertFalse(needs_department_head('OIL', 'VENDOR', 'OIL_PURCHASE'))
        self.assertFalse(needs_department_head('MART', 'EMPLOYEE_ADVANCE', 'RENT'))  # Mart: one approver

    def test_a_by_department_purpose_names_an_hod_with_a_login(self):
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Choose the Department Head'):
            _clean(vendor_bill_request(purpose_code='RENT'))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'not an active HOD'):
            _clean(vendor_bill_request(purpose_code='RENT', department_head_code='JWPL9999'))
        with self.assertRaisesRegex(request_service.RequestInvalid, 'Arshdeep Singh .JWPL3005. has no OMS login'):
            _clean(vendor_bill_request(purpose_code='RENT', department_head_code='JWPL3005'))
        cleaned = _clean(vendor_bill_request(purpose_code='RENT', department_head_code='temp0001'))
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

    def test_a_tie_is_no_match(self):
        twins = [user(1, 'amit.k', 'Amit Kumar'), user(2, 'amit.s', 'Amit Sharma')]
        self.assertIsNone(heads.match_user(hod('Amit'), twins))


class ExpenseRoutes(SimpleTestCase):
    """Expense requests: by budget head's owner (the Budget hierarchy), then Payment -> Audit."""

    def test_one_plain_and_one_electricity_route_per_head_and_one_for_mart(self):
        heads = sum(len(h) for h in hierarchy.EXPENSE_OWNERS.values())
        self.assertEqual(len(EXPENSE_ROUTES), heads * 2 + 1)
        codes = [r['code'] for r in ALL_ROUTES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(len(c) <= 60 for c in codes))

    def test_every_expense_route_ends_payment_then_audit(self):
        for route in EXPENSE_ROUTES:
            with self.subTest(route['code']):
                stages = hierarchy.full_stages(route)
                self.assertEqual([r for _n, r in stages[-2:]], ['payment', 'audit'])
                named = flow_service.roles([SimpleNamespace(name=n, sequence=i) for i, (n, _r) in
                                            enumerate(stages, start=1)])
                self.assertEqual(flow_service.posting_role(named), StageRole.AUDIT)

    def test_who_approves_an_expense(self):
        self.assertEqual(BY_CODE['AP_EXP_OIL_FACTORY']['stages'], [(hierarchy.OWNER_STAGE, 'factory_oil')])
        self.assertEqual(BY_CODE['AP_EXP_BEV_FACTORY']['stages'], [(hierarchy.OWNER_STAGE, 'factory_bev')])
        # Electricity: the owner, then the Director.
        self.assertEqual(BY_CODE['AP_EXP_OIL_FACTORY_ELEC']['stages'],
                         [(hierarchy.OWNER_STAGE, 'factory_oil'), (hierarchy.DIRECTOR_STAGE, 'director')])
        # NPD: the Director always follows. R & D / OTE: the Director alone.
        self.assertEqual(BY_CODE['AP_EXP_OIL_NPD1']['stages'],
                         [(hierarchy.OWNER_STAGE, 'avtar'), (hierarchy.DIRECTOR_STAGE, 'director')])
        self.assertEqual(BY_CODE['AP_EXP_OIL_R_D']['stages'], [(hierarchy.DIRECTOR_STAGE, 'director')])
        self.assertEqual(BY_CODE['AP_EXP_MART']['stages'], [(hierarchy.OWNER_STAGE, 'mart')])

    def test_an_expense_matches_only_its_own_route(self):
        q = BY_CODE['AP_EXP_OIL_SALES_RE']['query']
        for part in ("request_type = 'EXPENSE'", "company = 'OIL'", "budget_code = 'Sales RE'",
                     'is_electricity = false'):
            self.assertIn(part, q)
        self.assertIn('is_electricity = true', BY_CODE['AP_EXP_OIL_SALES_RE_ELEC']['query'])
        # Payment routes need a purpose an Expense never has, and Mart's excludes it.
        for route in ROUTES:
            if route['code'] in ('AP_MART', 'AP_STAFF_ADVANCE'):
                continue
            self.assertIn('purpose_code = ', route['query'])

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
