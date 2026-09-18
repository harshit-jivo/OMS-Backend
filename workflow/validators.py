"""Validation for admin-authored condition SQL — plan §14, Layer 2.

What this is, and what it is not
--------------------------------
The approved plan specifies parsing with `sqlglot` (or `pglast`). **Neither is
a dependency of this project**, and adding one is a dependency decision that
was not part of the approved scope, so this module does NOT pretend to be an
AST validator. It implements two real checks instead:

1. A conservative, comment- and string-aware lexical validator (below). It
   strips literals so a keyword inside a string cannot trip it, then enforces
   single-statement, SELECT-only, allow-listed relations, and a function
   deny-list.
2. `EXPLAIN`-based verification using PostgreSQL's OWN parser and planner
   (`workflow.services.conditions.explain`), which catches anything the
   lexical pass cannot — real syntax errors, unresolvable relations, and
   unknown functions — without executing the query.

Neither is a proof. That is why the read-only execution path (plan §5.2,
Layer 1) is the actual guarantee: even a total validation bypass yields only
`SELECT` on permitted relations, in a transaction the server has been told is
read-only. This module reduces how often that last line gets tested.

If an AST parser is later approved as a dependency, `validate_query_text`
is the single place to add it.
"""
import re

#: Statement kinds that must never appear, anywhere in the text — including
#: inside a CTE or subquery, which is why this is a whole-word scan over the
#: literal-stripped body rather than a check on the leading keyword only.
FORBIDDEN_KEYWORDS = frozenset({
    'insert', 'update', 'delete', 'merge', 'truncate', 'drop', 'alter',
    'create', 'grant', 'revoke', 'copy', 'call', 'do', 'vacuum', 'analyze',
    'reindex', 'cluster', 'comment', 'security', 'listen', 'notify',
    'prepare', 'execute', 'deallocate', 'discard', 'lock', 'set', 'reset',
    'begin', 'commit', 'rollback', 'savepoint', 'refresh', 'import',
})

#: Functions and constructs that read the filesystem, sleep, or reach another
#: server. A read-only role stops the writes; these are the read-side abuses.
FORBIDDEN_FUNCTIONS = frozenset({
    'pg_sleep', 'pg_read_file', 'pg_read_binary_file', 'pg_ls_dir',
    'pg_stat_file', 'lo_import', 'lo_export', 'dblink', 'dblink_exec',
    'pg_logical_emit_message', 'query_to_xml', 'pg_terminate_backend',
    'pg_cancel_backend', 'pg_reload_conf', 'pg_rotate_logfile',
    'set_config', 'current_setting',
})

#: Schemas a configured query may never touch, regardless of the allow-list.
FORBIDDEN_SCHEMAS = frozenset({
    'pg_catalog', 'information_schema', 'pg_temp', 'pg_toast',
})

_STRING_RE = re.compile(r"'(?:''|[^'])*'", re.DOTALL)
_DQUOTED_RE = re.compile(r'"(?:""|[^"])*"', re.DOTALL)
_DOLLAR_RE = re.compile(r'\$(\w*)\$.*?\$\1\$', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r'--[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
#: Relation references: the identifier following FROM / JOIN / INTO / UPDATE.
_RELATION_RE = re.compile(
    r'\b(?:from|join)\s+((?:[a-zA-Z_][\w$]*|"[^"]+")'
    r'(?:\s*\.\s*(?:[a-zA-Z_][\w$]*|"[^"]+"))?)',
    re.IGNORECASE,
)
_FUNCTION_RE = re.compile(r'\b([a-zA-Z_][\w$]*)\s*\(')
_WORD_RE = re.compile(r'\b([a-zA-Z_][\w$]*)\b')


def _strip_literals_and_comments(sql):
    """Blank out comments and string bodies, preserving nothing quotable.

    Done first so that `'-- not a comment'` or `'DROP TABLE x'` inside a
    string literal cannot trigger a keyword or comment rule. Replaced with a
    space rather than removed so adjacent tokens do not fuse.
    """
    out = _BLOCK_COMMENT_RE.sub(' ', sql)
    out = _LINE_COMMENT_RE.sub(' ', out)
    out = _DOLLAR_RE.sub(" '' ", out)
    out = _STRING_RE.sub(" '' ", out)
    return out


def _normalise_relation(raw):
    """`  Public . "My Tbl" ` -> `public.my tbl` (quotes keep their case)."""
    parts = [p.strip() for p in raw.split('.')]
    cleaned = []
    for part in parts:
        if part.startswith('"') and part.endswith('"'):
            cleaned.append(part[1:-1].replace('""', '"'))
        else:
            cleaned.append(part.lower())
    return '.'.join(cleaned)


def allowed_relations_for(extra=()):
    """Normalise a caller-supplied relation allow-list.

    ONLY the caller's list. The module registry used to contribute
    `business_table` here, but `workflow_modules` is identity now — the engine
    is generic and has no way to know which tables a module owns unless the
    module says so at the call site.

    An EMPTY allow-list means relations are not restricted, and that is the
    default for a query configured through the API. Read that plainly: the
    per-module relation boundary is gone, and a condition query may name any
    relation the database user can read. What still applies to every query, and
    is not weakened:

    * SELECT/WITH only, single statement, no DML or DDL keyword anywhere —
      enforced lexically here AND by the `workflow_query_select_only` CHECK;
    * the `FORBIDDEN_SCHEMAS` deny-list (pg_catalog, information_schema, ...);
    * the function deny-list;
    * execution on a read-only path (`conditions.isolation_mode`).

    So a query cannot WRITE. It can read more widely than before, which is why
    `workflow.config.manage` is an administrator permission. A caller that
    wants the old narrow boundary passes `extra` explicitly.
    """
    allowed = set()
    for raw in extra:
        if not raw:
            continue
        # A caller may pass Django's `schema"."table` quoting form; compare
        # on the raw-SQL `schema.table` either way.
        allowed.add(_normalise_relation(raw.replace('"."', '.')))
    # A bare table name is accepted for the same relation, since a query may
    # legitimately rely on search_path.
    for rel in list(allowed):
        if '.' in rel:
            allowed.add(rel.split('.')[-1])
    return allowed


def validate_query_text(query_text, *, allowed_relations, key_column):
    """Return a list of human-readable problems; empty means it passed.

    Errors are returned rather than raised so the admin/serializer layer can
    show every problem at once instead of one per save.
    """
    problems = []
    if not query_text or not query_text.strip():
        return ['Query text is empty.']

    body = _strip_literals_and_comments(query_text)
    stripped = body.strip().rstrip(';').strip()

    # --- single statement -------------------------------------------------
    # A trailing semicolon is fine; an interior one means a second statement.
    if ';' in stripped:
        problems.append(
            'Only a single statement is allowed — remove the ";" and any '
            'statement after it.'
        )

    # --- SELECT / WITH only ----------------------------------------------
    if not re.match(r'^\s*(select|with)\b', stripped, re.IGNORECASE):
        problems.append('The query must start with SELECT or WITH.')

    # --- no DML/DDL anywhere, including inside CTEs and subqueries --------
    words = {w.lower() for w in _WORD_RE.findall(stripped)}
    banned = sorted(words & FORBIDDEN_KEYWORDS)
    if banned:
        problems.append(
            'These statements are not allowed anywhere in a condition query: '
            + ', '.join(w.upper() for w in banned)
        )

    # --- function deny-list ----------------------------------------------
    used = {f.lower() for f in _FUNCTION_RE.findall(stripped)}
    bad_fns = sorted(used & FORBIDDEN_FUNCTIONS)
    if bad_fns:
        problems.append('These functions are not allowed: ' + ', '.join(bad_fns))

    # --- relation allow-list ---------------------------------------------
    referenced = {_normalise_relation(r) for r in _RELATION_RE.findall(stripped)}
    # A CTE name is a relation reference to itself, not an external table.
    cte_names = {
        m.lower() for m in re.findall(
            r'\b([a-zA-Z_][\w$]*)\s+as\s*\(', stripped, re.IGNORECASE)
    }
    for rel in sorted(referenced):
        if rel in cte_names:
            continue
        schema = rel.split('.')[0] if '.' in rel else ''
        if schema in FORBIDDEN_SCHEMAS:
            problems.append(f'Schema "{schema}" may not be read ({rel}).')
            continue
        if allowed_relations and rel not in allowed_relations:
            problems.append(
                f'Relation "{rel}" is not allow-listed for this module. '
                f'Allowed: {", ".join(sorted(allowed_relations))}'
            )

    # --- the key column must be selectable -------------------------------
    # Without it the §5.1 wrapper cannot bind the document id, so the query
    # is unusable by construction. `SELECT *` is accepted because the column
    # is then present by definition.
    if key_column:
        projects_star = re.search(r'select\s+(distinct\s+)?\*', stripped,
                                  re.IGNORECASE)
        if not projects_star and not re.search(
                r'\b' + re.escape(key_column) + r'\b', stripped, re.IGNORECASE):
            problems.append(
                f'The query must expose the key column "{key_column}" '
                f'(or SELECT *), otherwise the document id cannot be bound.'
            )

    return problems
