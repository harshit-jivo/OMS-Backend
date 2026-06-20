"""Per-request context shared between the middleware and the model signals.

The middleware records who is making the change and which page, at the start of
each request. The signal handlers read it so each change can be attributed to
the right user/page, and bump a counter so the middleware knows whether any
model change was already logged (and a fallback row is therefore not needed).

A thread-local keeps concurrent requests isolated.
"""
import threading

_state = threading.local()


def begin(user=None, page=''):
    _state.user = user
    _state.page = page
    _state.model_writes = 0


def get():
    return {
        'user': getattr(_state, 'user', None),
        'page': getattr(_state, 'page', ''),
    }


def mark_model_write():
    _state.model_writes = getattr(_state, 'model_writes', 0) + 1


def model_writes():
    return getattr(_state, 'model_writes', 0)


def clear():
    _state.__dict__.clear()
