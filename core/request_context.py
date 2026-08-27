"""Per-request identity for logs — Phase 5.2.

One request produces log lines from `orders`, `serviceLayer`, `hana`,
`einvoice` and `audit`, interleaved with every other concurrent request's
lines. Without a correlation key, reconstructing what happened during one
order submission means reading timestamps and guessing.

This module holds that key, plus the small amount of context worth carrying
with it, and `core.logging.RequestContextFilter` attaches it to every record
automatically — so no call site has to remember to pass it.

Why `contextvars` and not a thread-local
----------------------------------------
`audit/context.py` uses `threading.local()`, which is correct for what it does
today. This module does not follow it, for two reasons:

1. A `ContextVar` is the only one of the two that survives `async def` views
   and `sync_to_async`. Django supports both, and a thread-local silently
   returns the WRONG request's value when a coroutine resumes on a different
   thread — a failure that produces plausible, wrong log correlation rather
   than an error.
2. `ContextVar.set` returns a token that restores the previous value exactly.
   A thread-local in a pooled worker keeps the last request's value until it
   is overwritten, so a log line emitted between requests is attributed to
   whoever was served last.

Nothing here is a security boundary. `user_id` is copied for logging only;
never read it back to make an authorization decision — the request's own
`request.user` is the only thing that may do that.
"""
import contextvars
import re
import uuid

_request_id = contextvars.ContextVar('request_id', default='')
_user_id = contextvars.ContextVar('user_id', default='')
_path = contextvars.ContextVar('path', default='')
_method = contextvars.ContextVar('method', default='')

_VARS = (_request_id, _user_id, _path, _method)

#: What an inbound `X-Request-ID` is allowed to look like.
#:
#: This is a security control, not tidiness. The header is caller-supplied and
#: its value is written verbatim into every log line the request produces, so
#: an unvalidated value is a log-injection primitive: a newline plus a
#: convincing prefix forges entries that look like they came from the server.
#: The character class excludes CR, LF and everything else that could break a
#: line, and the length cap stops a caller writing a megabyte per line.
_SAFE_REQUEST_ID = re.compile(r'\A[A-Za-z0-9._-]{1,64}\Z')


def new_request_id():
    """A fresh ID. `uuid4().hex` rather than `str(uuid4())` — no dashes means
    the value is one token to a log grep and to a shell."""
    return uuid.uuid4().hex


def sanitize_request_id(value):
    """Return `value` if it is safe to log, otherwise a fresh ID.

    Deliberately does NOT strip or escape a bad value into shape. A caller
    that sent something unusable gets a server-generated ID instead, because a
    silently-rewritten ID would still correlate to whatever the caller thinks
    it sent — which is worse than not honouring it at all.
    """
    if value and _SAFE_REQUEST_ID.match(value):
        return value
    return new_request_id()


def bind(request_id='', user_id='', path='', method=''):
    """Bind context for the current request. Returns tokens for `unbind`."""
    return [
        _request_id.set(request_id),
        _user_id.set(str(user_id or '')),
        _path.set(path),
        _method.set(method),
    ]


def unbind(tokens):
    """Restore whatever was bound before.

    Tolerant of a token that cannot be used — one created in another context,
    which happens when a response is produced on a different thread than the
    one that bound it, or a malformed token list. Failing here would replace a
    real error with a confusing one from the logging layer.

    Both `ValueError` AND `TypeError` are caught. `ContextVar.reset` raises
    `ValueError` for a token from another context but `TypeError` for
    something that is not a token at all, and catching only the first left the
    loop to abort part-way — so the remaining variables kept the finished
    request's values and every later log line was attributed to it. That is
    exactly the leak this module exists to prevent, and it is why the reset
    happens per-variable rather than under one `try`.
    """
    for index, var in enumerate(_VARS):
        try:
            var.reset(tokens[index])
        except (ValueError, TypeError, IndexError, LookupError):
            var.set('')


def get():
    return {
        'request_id': _request_id.get(),
        'user_id': _user_id.get(),
        'path': _path.get(),
        'method': _method.get(),
    }


def get_request_id():
    return _request_id.get()
