"""Bank master, read live from SAP House Bank Accounts (DSC1).

OMS keeps no bank table of its own: a second copy of this data can only drift
from SAP, and the payload has to agree with SAP or the posting fails. A bank
added or changed in SAP appears here on the next cache refresh, with no code
or database change.
"""
import logging

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import hana_queries

logger = logging.getLogger(__name__)

TTL = 600                       # 10 minutes, per spec
_STALE_TTL = 30 * 24 * 3600     # outage fallback only


class BankMasterUnavailable(Exception):
    """SAP unreachable and nothing cached — the bank list cannot be verified."""


def _keys(company):
    company = (company or '').upper()
    # Two entries, deliberately. FRESH expires after the TTL and is what makes
    # a newly-configured SAP bank appear. STALE long outlives it purely so an
    # outage serves the last known-good list instead of an empty one, which
    # would read as "no bank exists".
    return f'bank_master:fresh:{company}', f'bank_master:stale:{company}'


def get_company_banks(company, *, force_refresh=False):
    """Return (banks, meta) for `company`.

    `banks` is a list of dicts: bank_code, display_name, gl_account,
    account_number, branch, ifsc, plus `key` (unique per ACCOUNT) and `label`.

    `meta` says where the answer came from:
        {'source': 'cache'|'sap'|'stale'|'none', 'available': bool,
         'stale': bool, 'synced_at': iso8601|None}

    Callers must branch on `available`. It separates "SAP says there are no
    banks" (available=True, empty list) from "we could not ask SAP"
    (available=False) — conflating those either blocks every payment during an
    outage or waves through a bank SAP will reject.
    """
    fresh_key, stale_key = _keys(company)

    if not force_refresh:
        cached = cache.get(fresh_key)
        if cached is not None:
            return cached['banks'], {'source': 'cache', 'available': True,
                                     'stale': False,
                                     'synced_at': cached['synced_at']}

    try:
        banks = hana_queries.fetch_company_banks(company=company)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning('bank master: SAP unreachable for %s: %s',
                       company, str(exc)[:200])
        stale = cache.get(stale_key)
        if stale is not None:
            return stale['banks'], {'source': 'stale', 'available': True,
                                    'stale': True,
                                    'synced_at': stale['synced_at']}
        return [], {'source': 'none', 'available': False, 'stale': False,
                    'synced_at': None}

    payload = {'banks': banks, 'synced_at': timezone.now().isoformat()}
    cache.set(fresh_key, payload, timeout=TTL)
    cache.set(stale_key, payload, timeout=_STALE_TTL)
    return banks, {'source': 'sap', 'available': True, 'stale': False,
                   'synced_at': payload['synced_at']}


def find_bank(company, selector):
    """One bank account, by `key` ("CODE:GL"), GL account, or bank code.

    Accepting all three keeps older stored values working: a receipt saved
    before this change holds a bare bank code, and must still resolve. A bare
    code matches the bank's FIRST account, which is only ambiguous for a bank
    with several — hence `key` being what the UI actually sends.

    Raises BankMasterUnavailable if the list could not be established, and
    ValidationError if it could and the bank is not in it.
    """
    wanted = (selector or '').strip()
    if not wanted:
        return None

    banks, meta = get_company_banks(company)
    if not meta['available']:
        raise BankMasterUnavailable(
            'Unable to verify bank information because SAP is currently '
            'unavailable.')

    upper = wanted.upper()
    for bank in banks:
        if bank['key'].upper() == upper:
            return bank
    for bank in banks:
        if bank['gl_account'].upper() == upper:
            return bank
    for bank in banks:
        if bank['bank_code'].upper() == upper:
            return bank

    options = ', '.join(sorted({b['bank_code'] for b in banks})) or 'none'
    raise ValidationError(
        f"The selected bank '{wanted}' no longer exists in SAP for {company}. "
        f'Please refresh and select another bank. Available: {options}.')


# ---------------------------------------------------------------------------
# Payment method -> SAP account resolution
# ---------------------------------------------------------------------------

class PaymentAccountResolver:
    """Resolve the SAP account a payment method is deposited into.

    Two inputs, deliberately separated:
      * SAP owns the ACCOUNTS (bank_master, cached from DSC1)
      * OMS owns the CHOICE of which account each method uses
        (PaymentMethodMapping)

    Note this is always OUR account. On a cheque it is where we bank the money;
    the bank the customer's cheque is drawn on is a different thing entirely,
    typed by the collector and sent to SAP as BankCode.
    """

    def __init__(self, company):
        self.company = (company or '').upper()
        self._banks = None
        self._meta = None
        self._mappings = None

    def _load(self):
        if self._banks is None:
            self._banks, self._meta = get_company_banks(self.company)
        return self._banks, self._meta

    def _mapping_for(self, method):
        if self._mappings is None:
            from .models import PaymentMethodMapping
            self._mappings = {
                m.payment_method: m.bank_key
                for m in PaymentMethodMapping.objects.filter(
                    company=self.company, is_active=True)
            }
        return self._mappings.get(method)

    def cash_gl(self):
        """The company's configured cash G/L. Never from DSC1."""
        from .models import SapCompanyMap
        row = SapCompanyMap.objects.filter(company=self.company).first()
        return ((getattr(row, 'cash_gl_account', '') or '').strip()
                if row else '')

    def resolve(self, payment_method, *, bank_key=None):
        """Our deposit account for one method, or None when nothing is mapped.

        Raises BankMasterUnavailable if SAP could not be reached and nothing
        was cached; ValidationError if the mapped account is gone from SAP.
        """
        banks, meta = self._load()
        if not meta['available']:
            raise BankMasterUnavailable(
                'Unable to verify bank information because SAP is currently '
                'unavailable.')

        selector = bank_key or self._mapping_for(payment_method)
        if not selector:
            return None

        found = find_bank(self.company, selector)
        if found is None:
            return None
        return {
            'bank_code': found['bank_code'],
            'bank_name': found['display_name'],
            'gl_account': found['gl_account'],
            'account_number': found['account_number'],
            'branch': found['branch'],
            'bank_key': found['key'],
        }
