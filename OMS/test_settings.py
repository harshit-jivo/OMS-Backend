"""Test settings: in-memory SQLite, schema built directly from the models.

Usage::

    python manage.py test orders --settings=OMS.test_settings

Why this file exists. Two separate obstacles stop `manage.py test` working
against the normal settings, and neither is caused by the tests:

1. The live Postgres role cannot CREATE DATABASE, so Django's test runner
   cannot build its `test_*` database at all.
2. The migration history cannot be replayed onto an empty database —
   `users/0004_userrole_user_role` fails with "table users_role already
   exists".

Pointing `default` at in-memory SQLite solves (1). Disabling the migration
executor solves (2) by having Django issue CREATE TABLE straight from the
current model definitions, which is what a test database needs anyway. The
consequence worth knowing: this suite verifies MODEL correctness, not migration
correctness — a broken migration would not be caught here.

Only the `default` alias is redirected. The `hana` alias must survive because
`hana/services/connection.py` reads `DATABASES['hana']['SCHEMA']` at import
time, and the root URLconf imports that module.

Nothing here touches the live database, and no test contacts Expo or a browser
push service — every outbound call in tests_notifications.py is mocked.
"""
from OMS.settings import *  # noqa: F401,F403
from OMS.settings import DATABASES as _REAL_DATABASES


class _SkipMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


DATABASES = dict(_REAL_DATABASES)
DATABASES['default'] = {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': ':memory:',
}

# --- HAIS schema qualifier flattened for SQLite ----------------------------
# The HAIS app owns a Postgres SCHEMA and declares its tables as
# `db_table = 'hais"."tbl_X'`, which Postgres reads as "hais"."tbl_X".
#
# SQLite has no schemas, so it reads `hais` as an attached-database name and
# EVERY app's tests fail with `unknown database "hais"` — including tests that
# never touch HAIS, because Django builds the whole schema up front.
#
# ATTACHing a database called `hais` fixes CREATE TABLE but not the foreign
# keys: SQLite cannot resolve a schema-qualified name in a REFERENCES clause,
# so tbl_AssetStorageType and tbl_AssetLog still fail. Dropping HAIS from
# INSTALLED_APPS is also not an option — OMS/urls.py:43 routes to HAIS.urls,
# so the app must stay importable.
#
# Rewriting `hais"."tbl_X` to `hais_tbl_X` for SQLite only keeps every table
# and FK inside one database, which is exactly what SQLite needs. Postgres is
# untouched: this file is only ever loaded by the test runner.
# Applied from HAIS's own AppConfig.ready() would be intrusive, so it is done
# here via class_prepared, which fires as each model class is built — early
# enough that the schema editor and the ORM only ever see the flat name.
from django.db.models.signals import class_prepared  # noqa: E402
from django.dispatch import receiver as _receiver  # noqa: E402


@_receiver(class_prepared)
def _flatten_schema_qualified_tables(sender, **kwargs):
    table = sender._meta.db_table
    if '"."' in table:
        sender._meta.db_table = table.replace('"."', '_')


# --- Postgres-only constraints dropped for SQLite -------------------------
# `workflow.StageReplacement` guards overlapping cover periods with an
# ExclusionConstraint — `EXCLUDE USING gist (old_user WITH =, daterange(...)
# WITH &&)`. It is the right tool on Postgres and has no SQLite equivalent, so
# the schema editor emits `EXCLUDE` verbatim and SQLite answers
#
#     OperationalError: near "EXCLUDE": syntax error
#
# during CREATE TABLE. That kills the WHOLE suite, not just the workflow
# tests: Django builds every app's schema up front, so no test in any app can
# run while one model carries a constraint SQLite cannot express.
#
# Dropped here, the same way and for the same reason the table names above are
# flattened. The consequence worth knowing: the database-level overlap guard
# is NOT exercised by this suite. The application-level check in
# `workflow/services` still is, and Postgres is untouched — this file is only
# ever loaded by the test runner.
from django.contrib.postgres.constraints import (  # noqa: E402
    ExclusionConstraint,
)


@_receiver(class_prepared)
def _drop_postgres_only_constraints(sender, **kwargs):
    constraints = getattr(sender._meta, 'constraints', None)
    if not constraints:
        return
    kept = [c for c in constraints if not isinstance(c, ExclusionConstraint)]
    if len(kept) != len(constraints):
        sender._meta.constraints = kept
        # `original_attrs` is what Django's schema editor reads when it builds
        # the table, so trimming only `constraints` leaves the EXCLUDE in
        # place and changes nothing.
        sender._meta.original_attrs['constraints'] = kept
MIGRATION_MODULES = _SkipMigrations()
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# --- Throttling off by default -------------------------------------------
# The throttle cache is process-global and does NOT reset between tests, so
# leaving the real rates on makes a suite's Nth anonymous request fail with 429
# for reasons that have nothing to do with the test — and the failure moves as
# tests are added or reordered.
#
# Tests that verify throttling re-enable it for themselves with
# @override_settings; see users.tests.LoginThrottleTests.
REST_FRAMEWORK = {**REST_FRAMEWORK, 'DEFAULT_THROTTLE_RATES': {
    'anon': None, 'user': None, 'login': None,
}}


# --- Transport security: off for the test client -----------------------------
# `SECURE_SSL_REDIRECT` defaults to true whenever DEBUG is false, which is
# correct for a deployed server and impossible for the test client: it issues
# plain HTTP and sets no X-Forwarded-Proto, so Django's SecurityMiddleware
# answers 301 before any view runs. Sixty-six tests then assert on a redirect
# instead of on the API — `301 != 200`, `301 != 403`, and
# `Content-Type header is "text/html", not "application/json"`.
#
# This was invisible until CI, because a developer .env sets DEBUG=true and the
# whole block above is skipped. Forcing it off here is what lets the suite run
# under the PRODUCTION configuration — permission classes, ALLOWED_HOSTS, CORS
# and the security headers are all still exercised with DEBUG false. Only the
# transport redirect, which no test can satisfy and no test asserts on, is
# disabled.

# --- Postgres regex escapes taught to SQLite ------------------------------
# `workflow.WorkflowQuery` guards its stored SQL with
#
#     CheckConstraint(condition=Q(query_text__iregex=r'^\s*(select|with)\y'))
#
# `\y` is Postgres's word boundary. Django implements `iregex` on SQLite by
# registering a Python `regexp` function and calling `re.search`, where `\y`
# is not an escape at all — so the UDF raises and SQLite reports only
#
#     OperationalError: user-defined function raised exception
#
# on every INSERT into that table. Nothing can be inserted, so `setUpTestData`
# dies and the ENTIRE backdate, production and workflow test surface errors out
# before a single assertion runs.
#
# Python spells the same boundary with a different letter, so the pattern
# is translated on its way into `re`. The constraint keeps doing its job —
# only the dialect changes — and Postgres never sees this: the file is
# loaded by the test runner alone.
from django.db.backends.signals import connection_created  # noqa: E402

#: Postgres's word boundary, and Python's. Written from `chr` so that no
#: tool rewriting this file can quietly eat a backslash.
SAP_WORD_BOUNDARY = chr(92) + 'y'
PY_WORD_BOUNDARY = chr(92) + 'b'


@_receiver(connection_created)
def _teach_sqlite_postgres_regex(sender, connection, **kwargs):
    if connection.vendor != 'sqlite':
        return
    import re

    def regexp(pattern, value):
        # Django's own `_sqlite_regexp`, with the escape translated. Same
        # None-handling and same str() coercion, so behaviour is unchanged for
        # every pattern that did not use `\y`.
        if pattern is None or value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        return bool(re.search(
            pattern.replace(SAP_WORD_BOUNDARY, PY_WORD_BOUNDARY), value))

    connection.connection.create_function('regexp', 2, regexp,
                                          deterministic=True)


SECURE_SSL_REDIRECT = False
