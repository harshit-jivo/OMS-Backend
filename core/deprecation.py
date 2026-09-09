"""Endpoint deprecation — Phase 6.4.

Retiring an endpoint on this system has always had the same missing step:
nobody can say who still calls it. `orders.Notification` is the standing
example — 366 rows, 125 references, a replacement that already exists in
`notifications`, and no way to tell whether the old routes are still in use by
the mobile client, the web client, both or neither.

This module supplies that: a decorator that marks a route deprecated, tells
the caller so in standard headers, and records each call so the question has
an answer before anything is deleted.

Headers
-------
`Deprecation` and `Sunset` are the real HTTP fields for this — `Sunset` is
RFC 8594, `Deprecation` its companion draft — rather than an invented
`X-` name, so an HTTP-aware client or gateway understands them without being
taught. `Link: <successor>; rel="successor-version"` names the replacement,
which is the part a person reading the response actually needs.

Response headers, not a body change. Adding a field to the body would alter
the response shape of the very endpoints being retired, which is the one thing
a deprecation is supposed to avoid.

What this deliberately does NOT do
----------------------------------
It never fails a request, and it sets no sunset date on its own. Turning an
endpoint off is a decision with a date attached, and the date belongs to
whoever owns the client migration. `devices.VersionPolicy` is the mechanism
that can actually block a stale mobile client (HTTP 426); this one only
informs and measures. Removing an endpoint is: mark it here → read the usage
log → set a sunset → let VersionPolicy enforce the client floor → delete.
"""
import functools
import logging

logger = logging.getLogger(__name__)

DEPRECATION_HEADER = 'Deprecation'
SUNSET_HEADER = 'Sunset'
LINK_HEADER = 'Link'

#: Registry of everything marked deprecated, for the tests and for
#: `/api/health/detail/`-style introspection. Keyed by view class name.
REGISTRY = {}


def _apply_headers(response, successor, sunset):
    # `Deprecation: true` is the draft's form for "deprecated, no date given".
    response[DEPRECATION_HEADER] = 'true'
    if sunset:
        response[SUNSET_HEADER] = sunset
    if successor:
        existing = response.get(LINK_HEADER)
        link = f'<{successor}>; rel="successor-version"'
        # Appended rather than overwritten: `Link` is a list-valued header and
        # something else may already have set one.
        response[LINK_HEADER] = f'{existing}, {link}' if existing else link
    return response


def deprecated(successor=None, sunset=None, note=''):
    """Mark an APIView (or a DRF `@api_view` function) as deprecated.

    `successor` is the path clients should move to. `sunset` is an HTTP-date
    string, and is left None until a real date exists — an invented one is
    worse than none, because clients plan against it.

    Applied to a class, it wraps `dispatch`, so it covers every method the view
    exposes and cannot be forgotten on one of them.
    """

    def decorate(view):
        successor_path = successor
        REGISTRY[getattr(view, '__name__', str(view))] = {
            'successor': successor_path,
            'sunset': sunset,
            'note': note,
        }

        if isinstance(view, type):
            original_dispatch = view.dispatch

            @functools.wraps(original_dispatch)
            def dispatch(self, request, *args, **kwargs):
                response = original_dispatch(self, request, *args, **kwargs)
                _log_call(request, view.__name__, successor_path)
                return _apply_headers(response, successor_path, sunset)

            view.dispatch = dispatch
            view.is_deprecated = True
            return view

        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            response = view(request, *args, **kwargs)
            _log_call(request, getattr(view, '__name__', ''), successor_path)
            return _apply_headers(response, successor_path, sunset)

        wrapper.is_deprecated = True
        return wrapper

    return decorate


def _log_call(request, view_name, successor):
    """Record the call so "is anything still using this?" has an answer.

    Structured `extra=` fields rather than an interpolated message, so the
    5.2 JSON formatter emits them as real keys and the usage can be counted
    without parsing prose. The request ID comes along automatically.

    The CLIENT is the field that matters. Knowing an endpoint is called 400
    times a day says nothing about whether it can be removed; knowing every
    call comes from ANDROID build 214 says exactly when it can.
    """
    try:
        logger.warning(
            'deprecated endpoint called: %s', view_name,
            extra={
                'deprecated_view': view_name,
                'successor': successor or '',
                'client_platform': request.META.get('HTTP_X_PLATFORM', ''),
                'client_version': request.META.get('HTTP_X_APP_VERSION', ''),
                'client_build': request.META.get('HTTP_X_BUILD_NUMBER', ''),
            },
        )
    except Exception:
        # Measurement must never be able to break the endpoint it measures.
        logger.debug('failed to log deprecated call', exc_info=True)
