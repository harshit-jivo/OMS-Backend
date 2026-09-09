"""Error tracking — Phase 5.4.

Sentry, and strictly opt-in: with no `SENTRY_DSN` set, nothing initialises and
nothing is sent anywhere. That is the default, and it is what CI and every
developer machine run under.

Why the guard is this careful
-----------------------------
An error tracker's whole job is to take the contents of a failing request —
locals, headers, request body, query string — and post it to a third party.
For most applications that is a fair trade. For this one the payloads include
SAP credentials in Service Layer calls, GSTINs and party master data, invoice
totals, and the NIC e-invoice tokens in `einvoice`. So the configuration here
is deliberately restrictive, and each restriction is a decision rather than a
default left in place:

* `send_default_pii=False` — do NOT attach cookies, the Authorization header,
  the request body or the user's IP. Sentry's own documentation encourages
  turning this on; it is exactly wrong here, because the Authorization header
  is a live JWT and the body of `/api/orders/submit/` is a customer's order.
* `_scrub` strips anything that looks like a credential from what survives.
  Sentry has its own server-side scrubber, but that runs AFTER the data has
  crossed the network and landed on someone else's disk. This one runs before
  the request leaves the process, which is the only place it can be relied on.
* `traces_sample_rate` defaults to 0. Performance tracing samples spans from
  every request including SQL text, and it is billed per event. It is
  available, off, and has to be asked for.

The request ID from 5.2 is attached as a tag, so an event in Sentry and the
lines in `logs/oms.log` are the same incident rather than two.
"""
import logging
import re

logger = logging.getLogger(__name__)

#: Header names never sent, whatever else is configured. Matched
#: case-insensitively against the exact name.
_DENY_HEADERS = frozenset({
    'authorization', 'cookie', 'set-cookie', 'x-csrftoken', 'x-api-key',
    'proxy-authorization', 'b1session', 'routeid',
})

#: Substrings that make a key's VALUE unsafe to send. Deliberately broad: a
#: false positive costs one redacted field in a bug report, a false negative
#: costs a credential on a third party's disk.
_SENSITIVE_KEY_PARTS = (
    'password', 'passwd', 'secret', 'token', 'auth', 'credential', 'apikey',
    'api_key', 'session', 'cookie', 'signature', 'private', 'dsn', 'otp',
    'aadhaar', 'pan', 'gstin', 'ifsc', 'account_number',
)

REDACTED = '[redacted]'

#: Values that look like credentials wherever they appear, including inside a
#: free-text exception message where no key name is available to match on.
_VALUE_PATTERNS = (
    # A JWT: three base64url segments. This is how a leaked Authorization
    # header usually escapes — quoted inside an exception message.
    re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
    # SAP Service Layer session cookie.
    re.compile(r'\bB1SESSION=[^;\s]+'),
    # `password=...` / `"password": "..."` in a serialised payload.
    #
    # The `["\']?` before the separator is not decoration. Without it the
    # pattern matched `password=x` but NOT `"password": "x"` — the closing
    # quote of the JSON key sits between the name and the colon — so a request
    # body quoted into an exception message, which is the commonest way a
    # payload reaches an error tracker, went through untouched.
    re.compile(
        r'(?i)\b(pass(?:word|wd)|secret|token|api_?key)\b["\']?\s*[=:]\s*'
        r'["\']?[^\s"\',}]+'),
)


def _scrub_text(value):
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _scrub(node, depth=0):
    """Walk an event and redact anything credential-shaped.

    Depth-limited because a Sentry event is arbitrarily nested and a cyclic or
    pathologically deep structure would otherwise recurse until the process
    dies — while handling an error, which is the worst possible moment.
    """
    if depth > 12:
        return node
    if isinstance(node, dict):
        cleaned = {}
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in _DENY_HEADERS or any(
                    part in lowered for part in _SENSITIVE_KEY_PARTS):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = _scrub(value, depth + 1)
        return cleaned
    if isinstance(node, (list, tuple)):
        return [_scrub(item, depth + 1) for item in node]
    if isinstance(node, str):
        return _scrub_text(node)
    return node


def before_send(event, hint):
    """Last gate before an event leaves the process.

    Also attaches the 5.2 request ID, so an event here and the lines in
    `logs/oms.log` are one incident rather than two.

    Any exception raised in here would be swallowed by the SDK and the event
    dropped silently — so failure is caught and reported explicitly rather
    than left to look like "Sentry is quiet today".
    """
    try:
        from core import request_context

        request_id = request_context.get_request_id()
        if request_id:
            event.setdefault('tags', {})['request_id'] = request_id

        # The query string is dropped rather than scrubbed. It carries IRNs,
        # party codes and occasionally a token, and unlike a body it is not
        # worth enough in a bug report to justify sending at all.
        request = event.get('request')
        if isinstance(request, dict):
            request.pop('query_string', None)
            request.pop('data', None)

        return _scrub(event)
    except Exception:
        logger.exception('Sentry before_send failed; dropping the event')
        return None


def init(settings):
    """Initialise Sentry if — and only if — a DSN is configured.

    Returns True when it initialised. Never raises: an error tracker that can
    stop the application from starting has inverted its own purpose.
    """
    dsn = getattr(settings, 'SENTRY_DSN', '') or ''
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        # In requirements.txt, but a deployment that has not reinstalled yet
        # must still boot. Loud, because a DSN was configured and the operator
        # is entitled to believe it took effect.
        logger.error(
            'SENTRY_DSN is set but sentry-sdk is not installed; '
            'error tracking is OFF. Run: pip install -r requirements.txt')
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=getattr(settings, 'SENTRY_ENVIRONMENT', 'production'),
            release=getattr(settings, 'SENTRY_RELEASE', '') or None,
            integrations=[
                DjangoIntegration(),
                LoggingIntegration(
                    # A breadcrumb for every INFO record, an event for every
                    # ERROR. `logger.exception` calls across the project become
                    # Sentry issues without any of them changing.
                    level=logging.INFO,
                    event_level=logging.ERROR,
                ),
            ],
            # See the module docstring. This is the single most consequential
            # setting on this call.
            send_default_pii=False,
            traces_sample_rate=getattr(settings, 'SENTRY_TRACES_SAMPLE_RATE', 0.0),
            before_send=before_send,
            # Bound so a failure loop cannot turn an outage into an outbound
            # traffic problem of its own.
            max_breadcrumbs=50,
        )
    except Exception:
        # This function runs at IMPORT TIME from settings.py, so anything that
        # escapes stops the application from starting. `sentry_sdk.init` raises
        # `BadDsn` on a malformed DSN — a typo in `.env` would take the whole
        # deployment down, which is a far worse outcome than having no error
        # tracking. Assumed otherwise at first; the test proved it wrong.
        logger.exception('Sentry failed to initialise; error tracking is OFF')
        return False

    logger.info('Sentry error tracking initialised (environment=%s)',
                getattr(settings, 'SENTRY_ENVIRONMENT', 'production'))
    return True
