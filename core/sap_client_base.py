"""Shared low-level helpers for the project's SAP Service Layer clients.

Three modules talk to the SAP Business One Service Layer directly —
`serviceLayer/service.py`, `einvoice/sap.py` and `payments/sap_client.py` —
and had independently converged on the same handful of tiny helpers
(`_base()` / `_verify()` / `_timeout()`) without ever sharing one copy. See
`serviceLayer/service.py`'s own module docstring, which names this exact
gap. Phase 3.6 moves the parts that really were byte-for-byte identical into
this one module; each client now imports from here instead of keeping its
own copy.

This is a PURE deduplication of low-level transport helpers only:

* `base_url()` — identical between `einvoice/sap.py` and
  `payments/sap_client.py` (both `settings.HANA_SERVICE_LAYER_URL.rstrip('/')`
  ). `serviceLayer/service.py` never had a `_base()` — it builds its (single)
  login URL directly from `settings.HANA_SERVICE_LAYER_URL` with no rstrip —
  and is left exactly as it was rather than switched onto this helper, so as
  not to change its behaviour on the (currently untested) chance that setting
  ever gains a trailing slash.
* `verify_setting()` — identical between `serviceLayer/service.py` and
  `payments/sap_client.py`. NOT wired into `einvoice/sap.py`: that module's
  own `_verify()` is a narrower, older version (`getattr(settings,
  "HANA_SSL_VERIFY", False)`) that never consults `HANA_SSL_CA_BUNDLE` — a
  real behavioural difference, currently inert only because this project's
  `.env` leaves `HANA_SSL_CA_BUNDLE` unset. Left alone rather than folded in;
  see this task's report for the discrepancy.
* `timeout_setting()` — identical between `serviceLayer/service.py` and
  `payments/sap_client.py`. NOT wired into `einvoice/sap.py`: that module's
  own `_timeout()` (`getattr(settings, "HANA_CONNECT_TIMEOUT", 15)`, no `or`
  fallback) diverges from this one only if the setting is ever explicitly
  falsy (e.g. `0`), which is not how it is configured today, but is still a
  real difference in the code as written, not just in comments.

Deliberately NOT moved here: each client's own typed exception
(`einvoice.sap.SapFetchError`, `payments.sap_client.SapError`) and the bare
`Exception` still raised by `serviceLayer/service.py`. Those differ in shape
— `SapError` alone carries `status_code` / `sap_code` / `payload` — and are
business logic the three clients built independently, not the shared
low-level helpers this item scoped in.
"""
from django.conf import settings


def base_url():
    """Service Layer base URL with no trailing slash."""
    return settings.HANA_SERVICE_LAYER_URL.rstrip('/')


def verify_setting():
    """Honour the configured TLS setting.

    A configured CA bundle path wins (passed straight to `requests` as
    `verify=<path>`, which is how a self-signed SAP cert is trusted without
    turning verification off wholesale); otherwise the plain
    `HANA_SSL_VERIFY` boolean.
    """
    bundle = getattr(settings, 'HANA_SSL_CA_BUNDLE', '') or ''
    if bundle:
        return bundle
    return getattr(settings, 'HANA_SSL_VERIFY', True)


def timeout_setting():
    """(connect, read) timeout tuple from settings, never unbounded."""
    return (
        getattr(settings, 'HANA_CONNECT_TIMEOUT', None) or 15,
        getattr(settings, 'HANA_READ_TIMEOUT', None) or 120,
    )
