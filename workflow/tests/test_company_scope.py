"""Company scope: ALL vs SPECIFIC, and the absence of precedence.

Covers T1-T6 from the approved plan §18. The load-bearing assertion is T4:
a SPECIFIC match and an ALL match are TWO matches, so the engine fails loud
rather than preferring the specific one. `approvals.resolve_workflow` does
prefer the specific one; that engine is deliberately untouched.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.companies import BEVERAGES, MART, OIL
from workflow.exceptions import (
    AmbiguousWorkflowSelection,
    WorkflowNotConfigured,
)
from workflow.models import CompanyScope, Workflow, WorkflowQuery
from workflow.services import selection
from workflow.tests.factories import (
    make_document,
    make_module,
    make_query,
    make_stage,
    make_user,
    make_workflow,
)


class CompanyScopeSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.user = make_user('scope-approver')

    def _select(self, company):
        doc = make_document(company=company)
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
        self.assertEqual(wf.company_scope, CompanyScope.ALL)
        self.assertIsNone(wf.company)

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
                self.module, make_document(company=company).pk, company)

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


class CompanyScopeConstraintTests(TestCase):
    """The database must refuse every ambiguous representation."""

    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()

    def test_all_scope_with_a_company_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Workflow.objects.create(
                    module=self.module, code='BAD1', name='bad',
                    company_scope=CompanyScope.ALL, company=OIL,
                )

    def test_specific_scope_without_a_company_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Workflow.objects.create(
                    module=self.module, code='BAD2', name='bad',
                    company_scope=CompanyScope.SPECIFIC, company=None,
                )

    def test_unknown_company_code_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Workflow.objects.create(
                    module=self.module, code='BAD3', name='bad',
                    company_scope=CompanyScope.SPECIFIC, company='ATLANTIS',
                )

    def test_unknown_scope_value_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Workflow.objects.create(
                    module=self.module, code='BAD4', name='bad',
                    company_scope='SOMETIMES', company=None,
                )

    def test_query_level_constraints_apply_too(self):
        wf = make_workflow(self.module, code='WF', company=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkflowQuery.objects.create(
                    workflow=wf, name='bad',
                    query_text='SELECT 1 AS id',
                    company_scope=CompanyScope.ALL, company=OIL,
                )


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
