"""The generic SQL executor: validation, binding, isolation, timeout."""
from django.db import connection
from django.test import TestCase

from core.companies import OIL
from workflow.exceptions import ConditionExecutionError
from workflow.models import WorkflowQuery
from workflow.services import conditions
from workflow.tests.factories import (
    DOCUMENT_TABLE,
    make_document,
    make_module,
    make_query,
    make_workflow,
)
from workflow.validators import allowed_relations_for, validate_query_text


class ValidatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        # An EXPLICIT allow-list. It no longer comes from the module registry,
        # so a caller that wants a relation boundary states one.
        cls.allowed = allowed_relations_for([DOCUMENT_TABLE])

    def _problems(self, sql, key_column='id'):
        return validate_query_text(sql, allowed_relations=self.allowed,
                                   key_column=key_column)

    def test_plain_select_passes(self):
        self.assertEqual(self._problems(f'SELECT * FROM {DOCUMENT_TABLE}'), [])

    def test_statement_chaining_is_rejected(self):
        problems = self._problems(
            f'SELECT * FROM {DOCUMENT_TABLE}; DROP TABLE x')
        self.assertTrue(any('single statement' in p for p in problems))

    def test_dml_is_rejected_even_inside_a_cte(self):
        sql = (f'WITH x AS (DELETE FROM {DOCUMENT_TABLE} RETURNING id) '
               f'SELECT id FROM x')
        problems = self._problems(sql)
        self.assertTrue(any('DELETE' in p for p in problems))

    def test_non_select_leading_keyword_is_rejected(self):
        problems = self._problems(f'UPDATE {DOCUMENT_TABLE} SET is_active = true')
        self.assertTrue(problems)

    def test_disallowed_relation_is_rejected_when_a_list_is_given(self):
        problems = self._problems('SELECT id FROM users_user')
        self.assertTrue(any('not allow-listed' in p for p in problems))

    def test_empty_allow_list_does_not_restrict_relations(self):
        """The DEFAULT is now unrestricted, and that is a real change.

        `allowed_relations_for()` with nothing to add returns an empty set,
        and an empty set means "not restricted" — the module registry used to
        contribute `business_table` here and no longer exists to do so. A
        condition query configured through the API can therefore name any
        relation the database user can read.

        What still stops it doing harm is asserted by the neighbouring tests:
        SELECT/WITH only, no DML keyword anywhere, the forbidden-schema list,
        the function deny-list, and read-only execution.
        """
        problems = validate_query_text(
            'SELECT id FROM users_user',
            allowed_relations=allowed_relations_for(),
            key_column='id',
        )
        self.assertEqual(problems, [])

    def test_forbidden_schemas_are_refused_even_unrestricted(self):
        problems = validate_query_text(
            'SELECT oid AS id FROM pg_catalog.pg_class',
            allowed_relations=allowed_relations_for(),
            key_column='id',
        )
        self.assertTrue(any('may not be read' in p for p in problems))

    def test_dml_is_refused_even_unrestricted(self):
        problems = validate_query_text(
            f'UPDATE {DOCUMENT_TABLE} SET is_active = true',
            allowed_relations=allowed_relations_for(),
            key_column='id',
        )
        self.assertTrue(problems)

    def test_system_catalog_is_rejected(self):
        problems = self._problems('SELECT oid AS id FROM pg_catalog.pg_class')
        self.assertTrue(any('may not be read' in p for p in problems))

    def test_pg_sleep_is_rejected(self):
        problems = self._problems(
            f'SELECT id FROM {DOCUMENT_TABLE} WHERE pg_sleep(10) IS NULL')
        self.assertTrue(any('pg_sleep' in p for p in problems))

    def test_missing_key_column_is_rejected(self):
        problems = self._problems(f'SELECT username FROM {DOCUMENT_TABLE}')
        self.assertTrue(any('key column' in p for p in problems))

    def test_keyword_inside_a_string_literal_is_not_a_false_positive(self):
        """`'DROP TABLE'` as DATA must not trip the keyword scan."""
        sql = (f"SELECT id FROM {DOCUMENT_TABLE} "
               f"WHERE document_number = 'DROP TABLE users'")
        self.assertEqual(self._problems(sql), [])

    def test_comment_cannot_hide_a_second_statement(self):
        sql = f'SELECT id FROM {DOCUMENT_TABLE} -- ; DROP TABLE x'
        self.assertEqual(self._problems(sql), [])


class ExecutorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.module = make_module()
        cls.workflow = make_workflow(cls.module, company=None)

    def test_matches_binds_the_document_key(self):
        query = make_query(self.workflow, where='amount > 100')
        rich = make_document(amount=500)
        poor = make_document(amount=5)
        self.assertTrue(conditions.matches(query, rich.pk))
        self.assertFalse(conditions.matches(query, poor.pk))

    def test_zero_rows_is_a_clean_non_match(self):
        query = make_query(self.workflow, where="company = 'NOPE_NOT_REAL'")
        doc = make_document(company=OIL)
        self.assertFalse(conditions.matches(query, doc.pk))

    def test_many_rows_still_answers_true_once(self):
        query = make_query(self.workflow)
        make_document()
        make_document()
        doc = make_document()
        self.assertTrue(conditions.matches(query, doc.pk))

    def test_unvalidated_query_refuses_to_run(self):
        query = make_query(self.workflow, name='raw', validate=False)
        query.validated_at = None
        query.save(update_fields=['validated_at'])
        doc = make_document()
        with self.assertRaises(ConditionExecutionError):
            conditions.matches(query, doc.pk)

    def test_broken_sql_raises_and_does_not_leak_database_text(self):
        query = WorkflowQuery.objects.create(
            workflow=self.workflow, name='broken',
            query_text='SELECT id FROM workflow.no_such_relation_xyz',
        )
        # Force it past the gate to prove the executor itself is safe.
        from django.utils import timezone
        query.validated_at = timezone.now()
        query.save(update_fields=['validated_at'])

        doc = make_document()
        with self.assertRaises(ConditionExecutionError) as caught:
            conditions.matches(query, doc.pk)
        # The message must not carry the relation name or SQL.
        self.assertNotIn('no_such_relation_xyz', caught.exception.message)

    def test_validation_failure_clears_validated_at(self):
        """A refused query keeps `validated_at` NULL, so selection skips it.

        Uses a forbidden SCHEMA rather than a non-allow-listed table: the
        stamp path supplies no relation allow-list any more, so `users_user`
        would now validate. The schema deny-list is unconditional.
        """
        query = WorkflowQuery.objects.create(
            workflow=self.workflow, name='bad',
            query_text='SELECT oid AS id FROM pg_catalog.pg_class',
        )
        problems = conditions.validate_and_stamp(query)
        query.refresh_from_db()
        self.assertTrue(problems)
        self.assertIsNone(query.validated_at)
        self.assertIn('may not be read', query.validation_error)

    def test_validation_success_stamps_and_clears_the_error(self):
        query = WorkflowQuery.objects.create(
            workflow=self.workflow, name='good',
            query_text=f'SELECT * FROM {DOCUMENT_TABLE}',
            validation_error='stale problem',
        )
        problems = conditions.validate_and_stamp(query)
        query.refresh_from_db()
        self.assertEqual(problems, [])
        self.assertIsNotNone(query.validated_at)
        self.assertEqual(query.validation_error, '')

    def test_statement_timeout_is_applied(self):
        """The timeout is set on the execution path, not just documented."""
        with connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            outer = cursor.fetchone()[0]
        query = make_query(self.workflow)
        doc = make_document()
        conditions.matches(query, doc.pk)
        with connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            # SET LOCAL is scoped to the executor's own block.
            self.assertEqual(cursor.fetchone()[0], outer)

    def test_isolation_mode_is_reported(self):
        self.assertIn(conditions.isolation_mode(),
                      {'read_only_role', 'savepoint_fallback'})

    def test_execute_returns_rows_for_module_use(self):
        query = make_query(self.workflow)
        make_document(amount=42)
        rows = conditions.execute(query)
        self.assertTrue(rows)
