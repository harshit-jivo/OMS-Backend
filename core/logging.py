"""Logging filters and formatters — Phase 5.2.

Two pieces:

* `RequestContextFilter` puts the request ID (and user, path, method) onto
  every record, so existing `logger.info(...)` calls gain correlation without
  a single call site changing.
* `JsonFormatter` renders records as one JSON object per line, for when the
  logs are shipped somewhere that parses them.

The text formatter stays the default. JSON is opt-in via `LOG_FORMAT=json`
because these logs are read directly today — by a person, with a text editor,
on the Windows box that runs them — and JSON is worse for that. "Structured"
is about the record carrying its fields; the rendering is a separate choice
and the deployment gets to make it.
"""
import json
import logging

from core import request_context

#: Attributes `logging.LogRecord` sets itself. Everything on a record that is
#: NOT in here came from `extra=` at the call site, which is what makes the
#: JSON formatter able to include custom fields without being told about them.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord('', 0, '', 0, '', (), None).__dict__
) | {'asctime', 'message', 'taskName'}


class RequestContextFilter(logging.Filter):
    """Attach request context to every record.

    A `Filter` rather than a `LoggerAdapter` or a custom `Logger` class: those
    require every call site to opt in, and there are several hundred existing
    `logger.*` calls across the project. A filter on the HANDLER applies to
    all of them, including third-party libraries and Django's own loggers.

    Every field is always set, even outside a request. A formatter referencing
    `%(request_id)s` raises `ValueError` on a record that lacks the attribute,
    and that error surfaces at the point of logging — so a management command
    would crash while writing a log line. Empty strings, never absent.
    """

    def filter(self, record):
        context = request_context.get()
        for key, value in context.items():
            # `setdefault` semantics: an explicit `extra={'user_id': ...}` at
            # the call site wins over the ambient context.
            if not hasattr(record, key):
                setattr(record, key, value)
        # A single pre-rendered token, because the text formatter needs one
        # field and every line should look the same whether or not there is a
        # request in flight.
        if not hasattr(record, 'context_tag'):
            request_id = getattr(record, 'request_id', '')
            record.context_tag = f'[{request_id[:8]}]' if request_id else '[-]'
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    `default=str` on the dump is load-bearing: `extra=` values are arbitrary
    objects (a Decimal, a model instance, a datetime), and an unserialisable
    one would raise inside the logging call — turning a log line into an
    application error. Logging must never be able to do that.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    #: Handled explicitly in `format`, and skipped by the catch-all loop so an
    #: empty one does not reappear as `"user_id": ""`.
    _CONTEXT_KEYS = ('request_id', 'user_id', 'method', 'path')

    def format(self, record):
        payload = {
            'time': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        for key in self._CONTEXT_KEYS:
            value = getattr(record, key, '')
            if value:
                payload[key] = value

        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)

        # Whatever the call site passed via `extra=`, without having to
        # enumerate it here.
        skip = _STANDARD_ATTRS | set(self._CONTEXT_KEYS) | {'context_tag'}
        for key, value in record.__dict__.items():
            if key not in skip and key not in payload:
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)
