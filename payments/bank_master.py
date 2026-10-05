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


def _cash_keys(company):
    company = (company or '').upper()
    return f'cash_master:fresh:{company}', f'cash_master:stale:{company}'


def get_company_cash_accounts(company, *, force_refresh=False):
    """Return (accounts, meta) for `company` — the same contract as banks.

    `accounts` is a list of dicts: gl_account, account_name, `key` (the G/L,
    since a drawer has no bank code) and `label`.

    Separate cache keys from the bank list because the two come from different
    SAP tables and one being unreadable says nothing about the other.
    """
    fresh_key, stale_key = _cash_keys(company)

    if not force_refresh:
        cached = cache.get(fresh_key)
        if cached is not None:
            return cached['accounts'], {'source': 'cache', 'available': True,
                                        'stale': False,
                                        'synced_at': cached['synced_at']}

    try:
        accounts = hana_queries.fetch_company_cash_accounts(company=company)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning('cash master: could not read cash accounts for %s: %s',
                       company, str(exc)[:200])
        stale = cache.get(stale_key)
        if stale is not None:
            return stale['accounts'], {'source': 'stale', 'available': True,
                                       'stale': True,
                                       'synced_at': stale['synced_at']}
        return [], {'source': 'none', 'available': False, 'stale': False,
                    'synced_at': None}

    payload = {'accounts': accounts, 'synced_at': timezone.now().isoformat()}
    cache.set(fresh_key, payload, timeout=TTL)
    cache.set(stale_key, payload, timeout=_STALE_TTL)
    return accounts, {'source': 'sap', 'available': True, 'stale': False,
                      'synced_at': payload['synced_at']}


def find_cash_account(company, selector):
    """One cash account of this company, by G/L code.

    Matched only against the company's own list, which is what enforces
    company isolation: another company's drawer is simply not in it. A frozen
    or non-postable account is not in it either, because the query excludes
    them — so "no longer selectable" and "never existed" refuse identically.

    Raises BankMasterUnavailable when the list could not be established, and
    ValidationError when it could and the account is not in it.
    """
    wanted = (selector or '').strip()
    if not wanted:
        return None

    accounts, meta = get_company_cash_accounts(company)
    if not meta['available']:
        raise BankMasterUnavailable(
            'Unable to verify cash accounts because SAP is currently '
            'unavailable.')

    # Exact, like find_bank_exact: the key IS the G/L for a drawer, and no
    # looser match may stand in for the account the user picked.
    for account in accounts:
        if account['key'] == wanted:
            return account

    options = ', '.join(a['gl_account'] for a in accounts) or 'none'
    raise ValidationError(
        f"The selected cash account '{wanted}' is not a valid cash account "
        f'for {company}. Available: {options}.')


def find_bank_exact(company, account_key):
    """One bank account by its canonical `key` ("BANKCODE:GLACCOUNT"), exactly.

    The Incoming Payment path uses this, NOT `find_bank`. A user picks one
    receiving account, and the key is that account's identity — so nothing
    short of the complete key may select it:

        "INB:1104102" -> that account
        "INB"         -> refused (OIL holds three INB accounts)
        "1104102"     -> refused (a G/L alone is not the key)

    `find_bank`'s fallbacks (G/L, then bare bank code, first account wins)
    exist for older deposit values and stay there. Reusing them here let a
    bare "INB" silently choose one of three accounts — an automatic selection
    the user never made.

    Scoped to the company's own list: identical keys in two company databases
    (a physically shared bank account) are two separate, valid accounts, each
    resolved within its own company.

    Raises BankMasterUnavailable if the list could not be established, and
    ValidationError if it could and the key is not in it.
    """
    wanted = (account_key or '').strip()
    if not wanted:
        return None

    banks, meta = get_company_banks(company)
    if not meta['available']:
        raise BankMasterUnavailable(
            'Unable to verify bank information because SAP is currently '
            'unavailable.')

    for bank in banks:
        if bank['key'] == wanted:
            return bank

    raise ValidationError(
        f"'{wanted}' is not a bank account of {company}. Select the "
        f'receiving account from the list (a bank code or G/L on its own is '
        f'not enough to identify one account).')


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

# `PaymentAccountResolver` lived here, from line 240 to the end of this file.
#
# It answered "which SAP account does this payment method use for this
# company?" by reading `PaymentMethodMapping` — one admin-chosen account per
# method — and resolving it against SAP's house-bank list.
#
# Both halves are gone. The collector now picks the receiving account on the
# payment itself and it is frozen onto the line, so there is nothing to resolve
# at read time and no table to resolve it from.
#
# What remains in this module is the SAP side alone: `get_company_banks` and
# `get_company_cash_accounts` supply the lists the picker offers, and
# `find_bank_exact` / `find_cash_account` turn a chosen key back into an
# account.
