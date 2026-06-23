"""Per-request context shared between the middleware and the model signals.

The middleware marks a request active and records who/which page. During the
request the signal handlers accumulate their changes into a per-record buffer
(keyed by model + pk) instead of writing immediately, so all changes to one
record - scalar fields, many-to-many relations, etc. - end up in a single
audit row. The middleware flushes that buffer at the end of the request.

Outside a request (shell, scheduled jobs) there is no active buffer, so the
handlers write their rows immediately.

A thread-local keeps concurrent requests isolated.
"""
import threading

_state = threading.local()


def begin(user=None, page=''):
    _state.user = user
    _state.page = page
    _state.active = True
    _state.buffer = {}


def is_active():
    return getattr(_state, 'active', False)


def get():
    return {
        'user': getattr(_state, 'user', None),
        'page': getattr(_state, 'page', ''),
    }


def buffer():
    return getattr(_state, 'buffer', None)


def clear():
    _state.__dict__.clear()
