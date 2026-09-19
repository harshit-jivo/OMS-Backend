"""Run the test suite against the EXISTING TEST PostgreSQL database.

    python manage.py test payments --settings=OMS.test_settings_pg --keepdb

Why this exists. The engine's tests need real PostgreSQL — schemas, a GiST
exclusion constraint, `information_schema` reads — and `OMS/test_settings.py`
(in-memory SQLite) cannot provide any of it. The normal answer is to let Django
create `test_<name>`, but the TEST role has no CREATEDB. So this points the test
runner at the database that already exists instead of making a second one.

WHAT THIS IS SAFE TO DO, AND WHY
--------------------------------
Every test in this project is a `TestCase` or `SimpleTestCase` — there is no
`TransactionTestCase` anywhere — so each test runs inside a transaction that is
ROLLED BACK. Test rows never survive, and the existing data is not touched.

WHAT IT DOES CHANGE
-------------------
`--keepdb` still runs `migrate` against that database, so any unapplied
migration IS applied for real. That is a genuine schema change to the TEST
database and the reason this file is opt-in rather than the default.

WHY --keepdb IS ENFORCED
------------------------
Without it, Django DROPS AND RECREATES the database named here — which is the
TEST database itself. That would destroy every receipt, deposit and approval in
it. The guard below refuses to load rather than trust anyone to remember.
"""
import sys

from OMS.settings import *  # noqa: F401,F403
from OMS.settings import DATABASES

if 'test' in sys.argv and '--keepdb' not in sys.argv:
    raise SystemExit(
        'Refusing to run: these settings point the test runner at the EXISTING '
        'TEST database, and without --keepdb Django would drop and recreate '
        'it, destroying its data.\n\n'
        '    python manage.py test <labels> --settings=OMS.test_settings_pg '
        '--keepdb\n'
    )

# Reuse the configured database AS the test database. Django tries to create it
# first; with --keepdb the failure (no CREATEDB) is swallowed and the existing
# one is used.
DATABASES['default'].setdefault('TEST', {})
DATABASES['default']['TEST']['NAME'] = DATABASES['default']['NAME']

# Keep the real migrations: the point of running here is to exercise the
# PostgreSQL schema those migrations produce.
