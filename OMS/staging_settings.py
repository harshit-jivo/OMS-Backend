"""Staging — Phase 0.4.

Production settings with the database name swapped, so a migration can be
rehearsed against real data and real row counts before it reaches `.117`.

    python manage.py migrate --settings=OMS.staging_settings --plan

Everything else is inherited on purpose. The point of a rehearsal is that the
only difference from production is the one you chose; a staging config that
also changes DEBUG, the middleware or the logging is rehearsing something else.

Two things ARE forced, because neither is about the database:

* Outbound side effects are off. Staging holds a copy of production's rows,
  including real party contacts, so a management command run here must not be
  able to email or push-notify anyone. This is the one difference worth having.
* `SENTRY_DSN` is cleared, so a rehearsal failure does not raise a production
  alert.
"""
from .settings import *  # noqa: F401,F403
from .settings import DATABASES as _PRODUCTION_DATABASES
from decouple import config

STAGING_DB_NAME = config(
    'STAGING_DB_NAME', default=f'{_PRODUCTION_DATABASES["default"]["NAME"]}_staging')

# A guard, not a formality. Importing this module must never be able to point
# Django at production — a rehearsal that silently ran against `.117` is the
# exact failure this phase exists to prevent, and it would look like success.
if STAGING_DB_NAME == _PRODUCTION_DATABASES['default']['NAME']:
    raise RuntimeError(
        f'STAGING_DB_NAME is the production database ({STAGING_DB_NAME!r}). '
        f'Refusing to load staging settings.')
if not STAGING_DB_NAME.endswith('_staging'):
    raise RuntimeError(
        f'STAGING_DB_NAME ({STAGING_DB_NAME!r}) must end in "_staging".')

DATABASES = {**_PRODUCTION_DATABASES}
DATABASES['default'] = {**_PRODUCTION_DATABASES['default'], 'NAME': STAGING_DB_NAME}

# Nothing leaves the building. `locmem` collects mail in memory and drops it.
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Error tracking off — a rehearsal failure is expected output, not an incident.
SENTRY_DSN = ''

# Visible in every log line, so a rehearsal cannot be mistaken for production
# when reading `logs/oms.log` afterwards.
import logging as _logging  # noqa: E402

_logging.getLogger('core').info('running against STAGING: %s', STAGING_DB_NAME)
