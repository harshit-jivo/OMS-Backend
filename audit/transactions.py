"""Transactions the audit buffer can be trusted across.

`AuditMiddleware` flushes the per-request buffer from its own `finally`, after
the view has returned — deliberately outside any transaction the view opened.
That is right for the ordinary case (a handful of `.save()` calls, no explicit
transaction) and wrong for a bulk write: a view that wraps four hundred saves in
`transaction.atomic()` and then raises rolls every row back, and the buffer it
filled on the way is still flushed afterwards. The log would then record four
hundred changes the database never kept — an audit trail wrong in the one
direction that matters, claiming a price change that never happened.

`audited_atomic()` is `transaction.atomic()` that also discards the entries a
rollback orphaned.
"""
from contextlib import contextmanager

from django.db import transaction

from . import context


@contextmanager
def audited_atomic(using=None):
    """`transaction.atomic()`, with the audit buffer rolled back alongside it.

    Only what this block added is discarded. Entries already in the buffer
    belong to an earlier write in the same request and are left alone.

    The one case this does not unpick: if an earlier write already buffered a
    record and this block merged FURTHER changes into that same entry, the entry
    survives with the merged values, because the buffer keeps one entry per
    record rather than a list. No caller does that today — each bulk view is the
    only writer in its request — and untangling it would mean teaching the audit
    buffer to version itself for a case that does not arise.
    """
    buffer = context.buffer()
    preexisting = set(buffer) if buffer is not None else set()
    try:
        with transaction.atomic(using=using):
            yield
    except Exception:
        current = context.buffer()
        if current is not None:
            for key in set(current) - preexisting:
                current.pop(key, None)
        raise
