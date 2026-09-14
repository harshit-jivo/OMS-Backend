"""The ONE generic query executor — plan §5.0.

Every module that needs the old JSAP `jsExecuteBudgetQueries` behaviour calls
in here. There is deliberately no second SQL execution path anywhere in the
engine, and none may be added to `budget`, `prdo`, `credit_limit`,
`backdate`, `bp_master`, `item_master` or any future module: JSAP's failure
mode was one `jsExecute*Queries` procedure per module, each re-implementing
dynamic SQL with its own bugs and its own injection surface.

The transformation this module performs
---------------------------------------
A configured query is a SET SELECTOR — it answers "which documents belong in
this workflow?", exactly as JSAP's did. JSAP therefore swept whole tables. We
keep JSAP's own subquery wrapper and add a BOUND predicate on the document
key, which preserves the set semantics exactly while turning the scan into an
index lookup:

    SELECT 1 FROM ( <configured query> ) AS q WHERE q.<key> = %s LIMIT 1

`%s` is a real bind parameter. The document id is never formatted into the
SQL string. That is the specific defect in `bud.jsExecuteBudgetQueries`,
which concatenated admin text into `sp_executesql`.

Isolation
---------
Two modes, and the difference is reported honestly rather than papered over:

* `workflow_ro` alias configured (recommended, plan §5.2) — execution runs on
  a SEPARATE connection, in its own transaction, which is set
  `READ ONLY` at the server before the query runs. A write is then impossible
  regardless of what validation missed, and a failure cannot poison the
  caller's transaction because it is a different connection.

* no such alias — execution falls back to the default connection inside a
  SAVEPOINT, with `statement_timeout` still applied. The savepoint is what
  keeps a SQL error from poisoning the surrounding business transaction.
  `SET TRANSACTION READ ONLY` is NOT available in this mode (PostgreSQL only
  accepts it as the first statement of a transaction, and the caller's
  transaction has already written the business row), so the read-only
  guarantee degrades to validation plus bound parameters. `isolation_mode()`
  reports which mode is live so a deployment can assert on it.
"""
import logging

from django.conf import settings
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

from workflow.exceptions import ConditionExecutionError
from workflow.models import DEFAULT_KEY_COLUMN
from workflow.validators import allowed_relations_for, validate_query_text

logger = logging.getLogger(__name__)

#: Database alias for condition execution. Provisioned as a read-only role.
RO_ALIAS = 'workflow_ro'

#: Per-statement ceiling. A condition is a single indexed existence check; if
#: one takes longer than this, the configuration is wrong.
STATEMENT_TIMEOUT_MS = getattr(settings, 'WORKFLOW_CONDITION_TIMEOUT_MS', 3000)


def isolation_mode():
    """`'read_only_role'` or `'savepoint_fallback'` — see the module docstring."""
    return ('read_only_role' if RO_ALIAS in settings.DATABASES
            else 'savepoint_fallback')


def _alias():
    return RO_ALIAS if RO_ALIAS in settings.DATABASES else 'default'


def _quote_ident(name):
    """Quote an identifier for safe interpolation.

    Used ONLY for the key column, which comes from configuration rather than
    from a request, and which cannot be bound as a parameter because it is an
    identifier rather than a value. Double-quote doubling is the PostgreSQL
    escape, so a crafted column name cannot break out of the quotes.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _run(sql, params, *, alias):
    """Execute `sql` under a statement timeout, isolated from the caller.

    Returns the fetched rows. Raises `ConditionExecutionError` on any database
    error, with the original logged rather than returned.
    """
    connection = connections[alias]
    dedicated = alias != 'default'
    try:
        # A dedicated connection gets its own transaction, so READ ONLY can be
        # set as the first statement. The default connection gets a savepoint,
        # which is what lets the caller's transaction survive a failure here.
        with transaction.atomic(using=alias, savepoint=True):
            with connection.cursor() as cursor:
                if dedicated:
                    cursor.execute('SET TRANSACTION READ ONLY')
                cursor.execute(
                    'SET LOCAL statement_timeout = %s', [STATEMENT_TIMEOUT_MS])
                cursor.execute(sql, params)
                return cursor.fetchall()
    except DatabaseError as exc:
        # The text of a database error routinely contains the query and a row
        # of data. Log it; return a generic message.
        logger.warning(
            'workflow condition execution failed (alias=%s): %s', alias, exc,
            exc_info=True,
        )
        raise ConditionExecutionError() from exc


def matches(query, document_key, *, key_column=None):
    """Is the document with `document_key` inside this query's set?

    The single call selection uses (plan §6). Returns a bool.

    `key_column` is RUNTIME CONTEXT, supplied by the business module through
    `engine.start()` — not read from the query row. The module owns its own
    table and therefore knows which column identifies one of its documents;
    asking an administrator to restate that in the query configuration made
    the engine hold a second copy of a fact it could not verify. Defaults to
    `id`, which is what every table in this codebase actually uses.

    It is quoted as an IDENTIFIER and never interpolated as data; the document
    key itself is always a bound parameter.

    Raises `ConditionExecutionError` if the query errors or times out — the
    caller must fail the start rather than treat the error as "no match",
    because a skipped broken condition silently mis-routes the document.
    """
    if query.validated_at is None:
        # A query that never passed validation is a configuration error, not
        # a non-match. Refusing here is what makes `validated_at` a real gate.
        raise ConditionExecutionError(
            f'Query "{query.name}" has not passed validation and cannot be '
            f'evaluated.'
        )

    quoted = _quote_ident(key_column or DEFAULT_KEY_COLUMN)
    sql = (
        f'SELECT 1 FROM ({query.query_text}) AS wf_q '
        f'WHERE wf_q.{quoted} = %s LIMIT 1'
    )
    rows = _run(sql, [document_key], alias=_alias())
    return bool(rows)


def execute(query, params=None, *, limit=1000):
    """Run a configured query and return its rows.

    Exists so that a module needing ROWS rather than a boolean still has no
    reason to open its own cursor. Same validation gate, same binding, same
    isolation and timeout as `matches()`.
    """
    if query.validated_at is None:
        raise ConditionExecutionError(
            f'Query "{query.name}" has not passed validation.')
    sql = f'SELECT * FROM ({query.query_text}) AS wf_q LIMIT {int(limit)}'
    return _run(sql, list(params or []), alias=_alias())


def explain(query_text, *, key_column=None):
    """Let PostgreSQL parse and plan the query without executing it.

    This is the part of validation that uses the real grammar rather than a
    lexical approximation: `EXPLAIN` resolves relations and functions and
    rejects genuine syntax errors, but runs nothing.

    Returns a list of problems (empty means it planned cleanly), so it
    composes with `validators.validate_query_text`.
    """
    wrapped = f'SELECT 1 FROM ({query_text}) AS wf_q'
    if key_column:
        wrapped += f' WHERE wf_q.{_quote_ident(key_column)} = %s LIMIT 1'
        params = [None]
    else:
        wrapped += ' LIMIT 1'
        params = []
    try:
        _run('EXPLAIN (FORMAT JSON) ' + wrapped, params, alias=_alias())
    except ConditionExecutionError:
        # The specific database message was already logged by `_run`. Returning
        # a generic problem keeps admin-visible text free of internals.
        return ['PostgreSQL could not plan this query — check the SQL, the '
                'table names, and that the key column exists.']
    return []


def check_query(workflow, query_text, *, extra_relations=(),
                run_explain=True):
    """Validate query text WITHOUT saving anything. Returns problems.

    Exists so the serializer can reject bad SQL *before* an INSERT is
    attempted. Without this, a DML query reached the database and tripped the
    `workflow_query_select_only` CHECK, surfacing as an unhandled
    IntegrityError and an HTTP 500 instead of a 400.

    Deliberately the SAME code path `validate_and_stamp` uses — there is one
    validator, and this must not become a second one.

    `workflow` is still taken so the signature reads as "validate this query
    for this workflow" and so callers need no change, but nothing about the
    module or the query row is consulted.
    """
    # Configuration-time validation cannot know the runtime key column — the
    # module supplies that when it submits a document, which has not happened
    # yet. It checks against the default, `id`, which is what the wrapper will
    # bind unless a caller overrides it. A query that projects something else
    # satisfies this with `SELECT *`, exactly as before.
    effective_key = DEFAULT_KEY_COLUMN
    problems = validate_query_text(
        query_text,
        allowed_relations=allowed_relations_for(extra_relations),
        key_column=effective_key,
    )
    if not problems and run_explain:
        problems = explain(query_text, key_column=effective_key)
    return problems


def validate_and_stamp(query, *, extra_relations=(), run_explain=True):
    """Validate a configured query and set or clear `validated_at`.

    The single place `validated_at` is written. A query that fails keeps
    `validated_at = NULL`, so `matches()` refuses it and selection skips it —
    a broken configuration cannot silently take part in routing. Used by the
    revalidate endpoint and the admin, where the row already exists.

    Returns the list of problems (empty means the query is now usable).
    """
    problems = check_query(
        query.workflow, query.query_text,
        extra_relations=extra_relations, run_explain=run_explain,
    )

    if problems:
        query.validated_at = None
        query.validation_error = '\n'.join(problems)
    else:
        query.validated_at = timezone.now()
        query.validation_error = ''
    query.save(update_fields=['validated_at', 'validation_error', 'updated_at'])
    return problems
