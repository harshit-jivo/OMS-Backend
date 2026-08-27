"""One error shape for the whole API — added to, never replacing (plan 3.8).

Error responses vary across this project. The frontend proves it: components
read `error.response.data.message` in 15 places, `.detail` in 13 and `.error`
in 11, because different endpoints answer with different keys. A caller cannot
write one error handler.

So this handler is ADDITIVE. It keeps every key the original response carried
and fills in the three the clients read, using `setdefault` so an existing
value is never overwritten. Nothing that works today stops working; new code
can rely on `message`, and the other two can be retired once the clients stop
reading them.

It also closes a gap that matters more than consistency. An unhandled
exception previously left DRF with nothing to render, so the API answered with
Django's HTML 500 page — HTML, to a JSON client, with a stack trace when DEBUG
is on. Those now log with a traceback and return JSON.

What it does NOT touch: a view that explicitly returns
`Response({'success': False, ...}, status=400)`. DRF's exception hook only
fires for RAISED exceptions. Converting those is a per-view change with a
client-visible blast radius, and is not what this is for.
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

#: Shown instead of the exception text for an unhandled error. The text of an
#: unexpected exception routinely carries a query, a file path or a row of
#: data, none of which belongs in an API response.
UNHANDLED_MESSAGE = 'An unexpected error occurred. The incident has been logged.'


def _first_message(data, fallback):
    """A human-readable sentence from a DRF error body.

    DRF answers with a string, a list, `{'detail': ...}`, or a dict of
    field -> [messages] — and for a serializer error the useful part is the
    field name plus its first message, not the raw structure.
    """
    if isinstance(data, str):
        return data
    if isinstance(data, (list, tuple)) and data:
        return _first_message(data[0], fallback)
    if isinstance(data, dict):
        if 'detail' in data:
            return _first_message(data['detail'], fallback)
        for field, value in data.items():
            message = _first_message(value, fallback)
            # `non_field_errors` names no field the user recognises.
            if field in ('non_field_errors', 'detail'):
                return message
            return f'{field}: {message}'
    return fallback


def api_exception_handler(exc, context):
    """DRF `EXCEPTION_HANDLER`."""
    response = drf_exception_handler(exc, context)

    view = context.get('view')
    request = context.get('request')

    if response is None:
        # A genuine bug. Django's own Http404 and PermissionDenied do NOT
        # arrive here — DRF's default handler already converts both, so
        # branches for them would be dead code; a test asserting otherwise is
        # what proved it.
        #
        # `exc_info=exc`, not `logger.exception`: the latter reads the
        # ACTIVE exception, so it logs "NoneType: None" whenever the handler is
        # called outside an `except` block. DRF calls it inside one, but a
        # traceback that depends on the caller's stack position is a trap.
        logger.error(
            'Unhandled exception in %s (%s %s)',
            getattr(view, '__class__', type(view)).__name__,
            getattr(request, 'method', '?'),
            getattr(request, 'path', '?'),
            exc_info=exc,
        )
        response = Response({'detail': UNHANDLED_MESSAGE},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    original = response.data
    body = dict(original) if isinstance(original, dict) else {'errors': original}

    message = _first_message(original, UNHANDLED_MESSAGE)
    # setdefault, never assignment: an endpoint that already answers with its
    # own `message` keeps it, so no existing client changes behaviour.
    body.setdefault('detail', message)
    body.setdefault('message', message)
    body.setdefault('error', message)
    body.setdefault('success', False)

    response.data = body
    return response
