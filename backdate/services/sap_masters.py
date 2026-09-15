"""SAP master data the BackDate form needs: users and document types.

Both come from zero-parameter HANA procedures — `GETUSERDETAILS` (from `OUSR`)
and `GETMOBJDETAILS` (from `MOBJ`), verified against the live catalogue.

WHY THESE ARE CACHED
--------------------
JSAP resolved a numeric object type into a display label by calling
`GETMOBJDETAILS` inside the transform every list endpoint ran — so opening the
pending list, the approved list or any document detail opened a live HANA
connection, per request. `HANAConnection` has no pooling (a known gap in
`docs/CODEBASE_AND_REFACTOR_PLAN.md`), so that is one connect/disconnect per
page view for data that changes very rarely.

A request stores the object NAME, and `object_type_for` turns that back into
the number `OPEN_BKDT` needs at the moment of the call. The mapping is
one-to-one — 75 objects, 75 distinct non-blank names, identical in all three
company schemas, verified against live HANA — so the round trip is exact.
"""
import logging

from django.core.cache import cache

from hana.services.connection import HANAConnection, HanaSchemaError, Queries

logger = logging.getLogger(__name__)

#: Master data changes on the order of never; an hour is short enough that a
#: genuine change is picked up the same working session.
CACHE_SECONDS = 3600


class MasterDataError(Exception):
    """SAP master data could not be read. The message is operator-safe."""


def _fetch(company, sql_name, row_builder, cache_key):
    company = (company or '').strip().upper()
    try:
        schema = Queries._schema_for_branch(company)
    except HanaSchemaError as exc:
        raise MasterDataError(
            f'"{company or "(none)"}" is not a company this module can read '
            f'SAP data for.') from exc

    key = f'backdate:{cache_key}:{schema}'
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        with HANAConnection() as conn:
            rows = conn.execute(f'CALL "{schema}"."{sql_name}"()')
    except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
        # The database message carries the schema and statement; log it, and
        # tell the caller something they can act on.
        logger.warning('BKDT %s failed for %s: %s', sql_name, schema, exc,
                       exc_info=True)
        raise MasterDataError(
            'SAP master data is unavailable right now. Try again shortly.'
        ) from exc

    built = [row_builder(row) for row in (rows or [])]
    built = [row for row in built if row is not None]
    cache.set(key, built, CACHE_SECONDS)
    return built


def _pick(row, *names):
    """First present key, case-insensitively.

    `HANAConnection.execute` returns dicts keyed by column name, and SAP B1
    column case is not consistent between tables, so the lookup is tolerant
    rather than assuming one spelling.
    """
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _user_row(row):
    """`GETUSERDETAILS` selects `USERID, USER_CODE, *` from `OUSR`."""
    code = _pick(row, 'USER_CODE')
    if code is None:
        return None
    return {'user_id': _pick(row, 'USERID'), 'user_code': str(code).strip()}


def _object_row(row):
    """`GETMOBJDETAILS` selects `*` from `MOBJ` — `Code, ObjType, ObjName`."""
    try:
        obj_type = int(_pick(row, 'ObjType'))
    except (TypeError, ValueError):
        return None
    name = _pick(row, 'ObjName') or ''
    return {'object_type': obj_type, 'name': str(name).strip()}


def sap_users(company):
    return _fetch(company, 'GETUSERDETAILS', _user_row, 'ousr')


def document_types(company):
    return _fetch(company, 'GETMOBJDETAILS', _object_row, 'mobj')


def object_type_for(company, name):
    """The numeric `ObjType` for an object NAME, or None.

    The reverse of `document_type_labels`, and the reason a request can store
    only the name: `OPEN_BKDT`'s `TRANSTYPE` is an INTEGER, so the number is
    needed at the moment of the call and nowhere else.

    Matched case-insensitively on the trimmed name, because the name travelled
    through a form and back. Returns None when SAP does not have it — the
    caller decides how loudly to say so, and for the SAP write that is loudly:
    guessing a number would grant rights over the wrong object.
    """
    wanted = (name or '').strip().casefold()
    if not wanted:
        return None
    for row in document_types(company):
        if row['name'].strip().casefold() == wanted:
            return row['object_type']
    return None


def document_type_names(company):
    """The object names a request may name, for validating a submission."""
    return [row['name'] for row in document_types(company) if row['name']]


def document_type_labels(company):
    """`{ObjType: name}` for rendering a stored numeric type.

    Returns an empty dict rather than raising: a list of requests must still
    render when SAP is unreachable, showing the number instead of the label.
    """
    try:
        return {row['object_type']: row['name']
                for row in document_types(company)}
    except MasterDataError:
        return {}
