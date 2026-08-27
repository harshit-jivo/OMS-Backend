"""Request correlation — Phase 5.2.

Binds a request ID for the life of each request, returns it on the response,
and writes one access line per request.

Placed FIRST in `MIDDLEWARE`, which matters: middleware wraps inward, so the
first entry is the outermost and therefore the only position from which the
context covers everything else — including `SecurityMiddleware`'s SSL redirect,
`CorsMiddleware`'s preflight replies and `AuditMiddleware`'s own logging. Any
later position leaves the requests that never reach the view uncorrelated,
and those are disproportionately the ones being investigated.
"""
import logging
import time

from django.conf import settings

from core import request_context

logger = logging.getLogger('core.request')

#: Read from the request, and echoed back on the response.
HEADER = 'X-Request-ID'
_META_KEY = 'HTTP_X_REQUEST_ID'

#: Paths that do not get an access line. A load balancer polls readiness every
#: few seconds, and left in, those lines would be the overwhelming majority of
#: the log — burying real traffic and rotating the interesting entries out of
#: the file. Failures still log, from the health check's own logger.
#
# Both prefixes, because Phase 6.2 mounts the API at `/api/` and `/api/v1/`
# and a load balancer configured against either one would otherwise flood the
# log through the prefix that was missed.
QUIET_PATHS = ('/api/health/', '/api/v1/health/')

#: A request slower than this is logged at WARNING rather than INFO, so
#: "what was slow yesterday" is a level filter instead of a parsing exercise.
SLOW_REQUEST_MS = 3000


class RequestContextMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request_context.sanitize_request_id(
            request.META.get(_META_KEY, ''))
        # Also on the request object, so a view or an exception handler can
        # put the ID in a response body without importing this module.
        request.request_id = request_id

        started = time.monotonic()
        # `request.user` is NOT read here. Authentication middleware runs
        # downstream of this one, so touching it now would either be empty or
        # force a database query on every request including anonymous ones.
        # The user is resolved after the response, when it is already known.
        tokens = request_context.bind(
            request_id=request_id, path=request.path, method=request.method)

        try:
            response = self.get_response(request)
        except Exception:
            # Logged here as well as by `django.request` because this is the
            # only layer that knows the request ID and the duration. The
            # exception is re-raised untouched — handling it is not this
            # middleware's job.
            self._log(request, None, started, level=logging.ERROR)
            raise
        finally:
            request_context.unbind(tokens)

        response[HEADER] = request_id
        self._log(request, response, started)
        return response

    def _log(self, request, response, started, level=None):
        if request.path.startswith(QUIET_PATHS):
            return

        duration_ms = round((time.monotonic() - started) * 1000, 1)
        status = getattr(response, 'status_code', 500)

        if level is None:
            if status >= 500:
                level = logging.ERROR
            elif status >= 400 or duration_ms >= SLOW_REQUEST_MS:
                level = logging.WARNING
            else:
                level = logging.INFO

        logger.log(
            level, '%s %s %s %sms', request.method, request.path, status,
            duration_ms,
            extra={
                # `request.path`, never `get_full_path()`. Query strings in
                # this API carry tokens, IRNs and party codes, and a log file
                # is a lower-trust store than the database they came from.
                'status': status,
                'duration_ms': duration_ms,
                'user_id': _user_id(request),
                'client_ip': _client_ip(request),
            },
        )


def _user_id(request):
    """Best-effort, and quiet about failure.

    Reading `request.user` after the response can raise if the session backend
    is unavailable or a custom authentication class misbehaves — and an
    exception raised while logging a request would replace the real outcome
    with a 500 from the logging layer.
    """
    try:
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            return user.pk
    except Exception:
        pass
    return ''


def _client_ip(request):
    """The caller's address.

    `X-Forwarded-For` is trusted ONLY when `USE_X_FORWARDED_FOR` says a proxy
    sets it, because the header is caller-supplied and trusting it by default
    lets anyone write any address into the logs. The LAST entry is taken, not
    the first: the first is whatever the client claimed, while the last was
    appended by the proxy nearest to us.
    """
    if getattr(settings, 'USE_X_FORWARDED_FOR', False):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded:
            return forwarded.split(',')[-1].strip()[:45]
    return request.META.get('REMOTE_ADDR', '')[:45]
