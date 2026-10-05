"""The SAP B1 company databases (HANA schemas) the Control Panel reads.

Taken from OMS's own settings — the same `.env` variables every other OMS
module uses — so each environment points the Control Panel at its own
companies (TEST_* on a test box, the live ones in production):

    OIL        HANA_DB_OIL_NAME        -> settings.HANA_OIL_COMPANY_DB
    BEVERAGES  HANA_DB_BEVERAGE_NAME   -> settings.HANA_BEVERAGE_COMPANY_DB
    MART       HANA_DB_MART_NAME       -> settings.HANA_MART_COMPANY_DB

All three are required: a blank one is a system-check error (check_companies,
registered in apps.py), so the server refuses to start rather than querying
the wrong company.
"""
from django.conf import settings
from django.core.checks import Error

OIL = getattr(settings, 'HANA_OIL_COMPANY_DB', '') or ''
BEVERAGES = getattr(settings, 'HANA_BEVERAGE_COMPANY_DB', '') or ''
MART = getattr(settings, 'HANA_MART_COMPANY_DB', '') or ''

#: (label, setting, .env variable) — what the system check verifies.
_REQUIRED = (
    ('OIL', 'HANA_OIL_COMPANY_DB', 'HANA_DB_OIL_NAME'),
    ('BEVERAGES', 'HANA_BEVERAGE_COMPANY_DB', 'HANA_DB_BEVERAGE_NAME'),
    ('MART', 'HANA_MART_COMPANY_DB', 'HANA_DB_MART_NAME'),
)


def check_companies(app_configs=None, **kwargs):
    return [
        Error(f'The Control Panel needs the {label} SAP company database name.',
              hint=f'Set {env} in the backend .env (e.g. the live or TEST_ company schema).',
              id='cpanel.E001')
        for label, setting, env in _REQUIRED if not getattr(settings, setting, '')
    ]
