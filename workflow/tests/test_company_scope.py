"""Company applicability: `ALL` vs one company, and the absence of precedence.

Covers T1-T6 from the approved plan §18. The load-bearing assertion is T4:
a company-specific match and an `ALL` match are TWO matches, so the engine
fails loud rather than preferring the specific one. `approvals.resolve_workflow`
does prefer the specific one; that engine is deliberately untouched.

`company` is ONE column holding `ALL`, `OIL`, `BEVERAGES` or `MART`. The old
`company_scope` + nullable `company` pair is gone, so the contradictory states
these tests used to assert against are now unrepresentable rather than merely
forbidden — which is why the constraint class below is much shorter.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.companies import BEVERAGES, MART, OIL
from workflow.exceptions import (
    AmbiguousWorkflowSelection,
    WorkflowNotConfigured,
)
from workflow.models import COMPANY_ALL, Workflow, WorkflowQuery
from workflow.services import selection
from workflow.tests.factories import (
    make_document,
    make_module,
    make_query,
    make_stage,
    make_user,
    make_workflow,
)


class CompanyApplicabilitySelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.user = make_user('scope-approver')

    def _select(self, company):
        doc = make_document()
        return selection.select_workflow(self.module, doc.pk, company)

    # --- T1 ---------------------------------------------------------------
    def test_specific_company_matches_its_own_company(self):
        wf = make_workflow(self.module, code='WF_OIL', company=OIL)
        make_query(wf, company=OIL)
        make_stage(wf, 1, self.user)

        workflow, _query = self._select(OIL)
        self.assertEqual(workflow.pk, wf.pk)

    # --- T2 ---------------------------------------------------------------
    def test_all_scope_matches_a_specific_company_document(self):
        wf = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(wf, company=None)
        make_stage(wf, 1, self.user)

        workflow, _query = self._select(OIL)
        self.assertEqual(workflow.pk, wf.pk)
        self.assertEqual(wf.company, COMPANY_ALL)

    # --- T3 ---------------------------------------------------------------
    def test_one_all_configuration_serves_every_company(self):
        wf = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(wf, company=None)
        make_stage(wf, 1, self.user)

        for company in (OIL, BEVERAGES, MART):
            with self.subTest(company=company):
                workflow, _query = self._select(company)
                self.assertEqual(workflow.pk, wf.pk)

    # --- T4 — the important one -------------------------------------------
    def test_specific_and_all_both_matching_is_ambiguous_not_preferred(self):
        specific = make_workflow(self.module, code='WF_OIL', company=OIL)
        make_query(specific, name='oil', company=OIL)
        make_stage(specific, 1, self.user)

        catch_all = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(catch_all, name='all', company=None)
        make_stage(catch_all, 1, self.user)

        with self.assertRaises(AmbiguousWorkflowSelection) as caught:
            self._select(OIL)

        # Both must be named, so an operator can see which two collided.
        self.assertIn('WF_ALL', caught.exception.message)
        self.assertIn('WF_OIL', caught.exception.message)

    def test_ambiguity_is_not_resolved_by_row_order(self):
        """Creating the ALL row FIRST must not change the outcome."""
        catch_all = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(catch_all, name='all', company=None)
        make_stage(catch_all, 1, self.user)

        specific = make_workflow(self.module, code='WF_OIL', company=OIL)
        make_query(specific, name='oil', company=OIL)
        make_stage(specific, 1, self.user)

        with self.assertRaises(AmbiguousWorkflowSelection):
            self._select(OIL)

    # --- T5 ---------------------------------------------------------------
    def test_specific_company_does_not_match_another_company(self):
        wf = make_workflow(self.module, code='WF_OIL', company=OIL)
        make_query(wf, company=OIL)
        make_stage(wf, 1, self.user)

        with self.assertRaises(WorkflowNotConfigured):
            self._select(BEVERAGES)

    def test_all_selected_alone_when_specific_is_for_another_company(self):
        specific = make_workflow(self.module, code='WF_OIL', company=OIL)
        make_query(specific, name='oil', company=OIL)
        make_stage(specific, 1, self.user)

        catch_all = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(catch_all, name='all', company=None)
        make_stage(catch_all, 1, self.user)

        workflow, _query = self._select(MART)
        self.assertEqual(workflow.pk, catch_all.pk)

    # --- T6 ---------------------------------------------------------------
    def test_all_is_stored_as_exactly_one_row(self):
        """The point of ALL: no per-company duplication."""
        wf = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(wf, company=None)
        make_stage(wf, 1, self.user)

        for company in (OIL, BEVERAGES, MART):
            selection.select_workflow(
                self.module, make_document().pk, company)

        self.assertEqual(Workflow.objects.count(), 1)
        self.assertEqual(WorkflowQuery.objects.count(), 1)

    # --- query-level narrowing -------------------------------------------
    def test_query_scope_narrows_within_an_all_workflow(self):
        """One workflow, a different condition per company — still ONE match."""
        wf = make_workflow(self.module, code='WF_ALL', company=None)
        make_query(wf, name='oil-only', where="company = 'OIL'", company=OIL)
        make_query(wf, name='bev-only', where="company = 'BEVERAGES'",
                   company=BEVERAGES)
        make_stage(wf, 1, self.user)

        for company in (OIL, BEVERAGES):
            with self.subTest(company=company):
                workflow, query = self._select(company)
                self.assertEqual(workflow.pk, wf.pk)
                # Only the company's own query is even evaluated.
                self.assertEqual(query.company, company)

        # MART matches neither query, so the workflow does not apply.
        with self.assertRaises(WorkflowNotConfigured):
            self._select(MART)

    def test_document_without_company_matches_only_all_scope(self):
        specific = make_workflow(self.module, code='WF_OIL', company=OIL)
        make_query(specific, name='oil', company=OIL)
        make_stage(specific, 1, self.user)

        with self.assertRaises(WorkflowNotConfigured):
            self._select('')


class CompanyConstraintTests(TestCase):
    """The database must refuse anything that is not a company or `ALL`.

    There is exactly ONE rule left. The three old CHECKs existed to bar the
    contradictory `(scope, company)` pairs; with one column those states
    cannot be written down, so there is nothing to forbid.
    """

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()

    def test_unknown_company_is_rejected_on_a_workflow(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Workflow.objects.create(
                    module=self.module, code='BAD1', name='bad',
                    company='ATLANTIS',
                )

    def test_empty_company_is_rejected(self):
        """`''` is the sentinel this design deliberately does not use."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Workflow.objects.create(
                    module=self.module, code='BAD2', name='bad', company='',
                )

    def test_every_valid_company_is_accepted(self):
        for i, company in enumerate([COMPANY_ALL, OIL, BEVERAGES, MART]):
            with self.subTest(company=company):
                wf = Workflow.objects.create(
                    module=self.module, code=f'OK{i}', name='ok',
                    company=company,
                )
                self.assertEqual(wf.company, company)

    def test_query_level_constraint_applies_too(self):
        wf = make_workflow(self.module, code='WF', company=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkflowQuery.objects.create(
                    workflow=wf, name='bad',
                    query_text='SELECT 1 AS id', company='PUNJAB',
                )


class CompanyMatchingTests(TestCase):
    """`applies_to_company` — the pure-Python mirror of the selection filter.

    Spelled out per pair rather than derived, because the whole point is that
    `ALL` matches everything and a specific company matches only itself. A
    loop computing the expectation from the same rule would pass even if the
    rule were wrong.
    """

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()

    def test_all_matches_every_company(self):
        wf = make_workflow(self.module, code='A', company=None)
        for company in (OIL, BEVERAGES, MART):
            with self.subTest(document=company):
                self.assertTrue(wf.applies_to_company(company))

    def test_specific_matches_only_itself(self):
        cases = [
            (OIL, OIL, True), (OIL, BEVERAGES, False), (OIL, MART, False),
            (BEVERAGES, BEVERAGES, True), (BEVERAGES, OIL, False),
            (BEVERAGES, MART, False),
            (MART, MART, True), (MART, OIL, False), (MART, BEVERAGES, False),
        ]
        for i, (configured, document, expected) in enumerate(cases):
            with self.subTest(configured=configured, document=document):
                wf = make_workflow(self.module, code=f'S{i}',
                                   company=configured)
                self.assertIs(wf.applies_to_company(document), expected)

    def test_a_document_with_no_company_matches_only_all(self):
        allc = make_workflow(self.module, code='N1', company=None)
        oil = make_workflow(self.module, code='N2', company=OIL)
        self.assertTrue(allc.applies_to_company(''))
        self.assertFalse(oil.applies_to_company(''))


class QueryScopeNarrowingTests(TestCase):
    """A query may narrow its workflow's scope, never contradict it."""

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()

    def test_contradicting_scope_is_reported(self):
        wf = make_workflow(self.module, code='WF_OIL', company=OIL)
        query = make_query(wf, name='bev', company=BEVERAGES, validate=False)
        conflict = query.scope_conflict()
        self.assertIsNotNone(conflict)
        self.assertIn('BEVERAGES', conflict)
        self.assertIn('OIL', conflict)

    def test_matching_and_all_scopes_are_accepted(self):
        wf = make_workflow(self.module, code='WF_OIL', company=OIL)
        same = make_query(wf, name='oil', company=OIL, validate=False)
        broader = make_query(wf, name='all', company=None, validate=False)
        self.assertIsNone(same.scope_conflict())
        self.assertIsNone(broader.scope_conflict())

    def test_all_workflow_accepts_any_query_scope(self):
        wf = make_workflow(self.module, code='WF_ALL', company=None)
        for company in (None, OIL, BEVERAGES, MART):
            with self.subTest(company=company):
                query = make_query(wf, name=f'q-{company}', company=company,
                                   validate=False)
                self.assertIsNone(query.scope_conflict())
