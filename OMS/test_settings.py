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
SECURE_SSL_REDIRECT = False
