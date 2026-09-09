"""Where a payment posts: SAP company database and default branch.

Both come from the ENVIRONMENT, never from a payments table. Moving TEST to
LIVE is then a deployment change — set `HANA_DB_OIL_NAME` and restart — rather
than an `UPDATE payments.payment_sap_company_map`, which is a database edit
nobody reviews and which silently differs between environments.

The company database is resolved by `SAPServiceLayerManager.schema_for()`, the
same function the Service Layer, tracker and e-invoice modules already use.
Payments deliberately does NOT get its own mapping: two resolvers reading two
sources is how a payment ends up posting to a different company than the
invoice it settles.
"""
import logging

from django.conf import settings
from django.core.exceptions import ValidationError

from serviceLayer.service import SAPServiceLayerManager

logger = logging.getLogger(__name__)

# Company -> the setting holding that company's default branch. The company
# keys are core.CATEGORY_CHOICES; the setting names follow the existing
# HANA_<COMPANY>_* convention in settings.py.
_DEFAULT_BPL_SETTING = {
    'OIL': 'HANA_OIL_DEFAULT_BPL_ID',
    'BEVERAGES': 'HANA_BEVERAGE_DEFAULT_BPL_ID',
    'MART': 'HANA_MART_DEFAULT_BPL_ID',
}


def resolve_company_db(company):
    """The SAP company database / HANA schema for a company key.

    One call, one source. `schema_for` treats an unknown branch as OIL, which
    is right for a sales document but wrong for money: posting a Mart payment
    into the Oil company because a typo slipped through is exactly the failure
    this module exists to prevent. So an unrecognised company is refused here
    before it reaches the resolver.
    """
    key = (company or '').strip().upper()
    if key not in _DEFAULT_BPL_SETTING:
        raise ValidationError(
            f'"{company}" is not a known company. Expected one of: '
            f'{", ".join(sorted(_DEFAULT_BPL_SETTING))}.')

    schema = (SAPServiceLayerManager.schema_for(key) or '').strip()
    if not schema:
        raise ValidationError(
            f'No SAP company database is configured for {key}. Set the '
            f'HANA company database environment variable for it.')
    return schema


def default_bpl_id(company):
    """The fallback branch for a company, or None when unset.

    Returns None rather than raising: the callers that use this are already
    handling "no branch to inherit", and each decides for itself whether a
    missing default is fatal.
    """
    key = (company or '').strip().upper()
    name = _DEFAULT_BPL_SETTING.get(key)
    if not name:
        return None
    value = getattr(settings, name, None)
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        logger.warning('%s is not a valid branch id: %r', name, value)
        return None
