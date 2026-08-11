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
MIGRATION_MODULES = _SkipMigrations()
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
