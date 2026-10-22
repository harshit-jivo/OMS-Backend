"""An Item bill's GRPO user fields — bilty, transporter, vehicle, litres… — for
the Accounts desk (user, 2026-10-22): every user field the GRPO carries is
shown; the ones a bill may differ in are editable, the rest read only.

Which is which comes from how the books use them (live, bills copied from a
GRPO since April 2026): the transport details on the header (bilty number and
date, transporter, vehicle, LR / bill of entry, gate pass, AR number) are
re-keyed or corrected on the bill often enough — Beverages' bills mostly
differ — and a line's litres, bilty and remarks now and then; customer and
sub-account codes, scheme fields, the add-on's counters and Mart's document
references are carried as the GRPO has them, never changed.

The A/P invoice is sent every one of them that the bill also has — the GRPO's
value, or the desk's where it changed an editable one — so the bill carries
what the GRPO did. Read only; nothing here writes to SAP.
"""
import datetime
from decimal import Decimal, InvalidOperation

from advance_payment.services.sap import _date, _run, _s, _schema

#: Editable on the header / a line: `{field: kind}` (text, date or number).
HEADER_EDITABLE = {
    'U_BilltyNumber': 'text', 'U_BiltyDate': 'date', 'U_TransporterName': 'text', 'U_VehicleNoM': 'text',
    'U_LRNUmber': 'text', 'U_TransporterInvoice': 'text', 'U_ARNO': 'text', 'U_AR_NO': 'text',
    'U_TotalAmt': 'number', 'U_BOEDate': 'date', 'U_InvRevEntry': 'text',
}
LINE_EDITABLE = {
    'U_UNE_LTS': 'number', 'U_BilltyNumber': 'text', 'U_BiltyDate': 'date', 'U_ARNO': 'text', 'U_Remarks': 'text',
}
#: Typed values are checked to this length; SAP's own sizes are narrower for some.
MAX_TEXT = 100

_COLUMNS_SQL = '''
    SELECT TABLE_NAME AS "table", COLUMN_NAME AS "name", DATA_TYPE_NAME AS "type"
    FROM SYS.TABLE_COLUMNS
    WHERE SCHEMA_NAME = ? AND TABLE_NAME IN ('OPDN', 'PDN1', 'OPCH', 'PCH1') AND LEFT(COLUMN_NAME, 2) = 'U_'
'''
_LABELS_SQL = '''
    SELECT "TableID" AS "table", "AliasID" AS "alias", "Descr" AS "label", "TypeID" AS "type"
    FROM "{schema}"."CUFD" WHERE "TableID" IN ('OPDN', 'PDN1')
'''


def _kind(sap_type, data_type):
    """text, date or number, from SAP's field type (CUFD.TypeID) and the column's."""
    if sap_type == 'D' or data_type in ('TIMESTAMP', 'DATE', 'SECONDDATE'):
        return 'date'
    if sap_type in ('N', 'B') or data_type in ('DECIMAL', 'INTEGER', 'SMALLINT', 'BIGINT', 'DOUBLE'):
        return 'number'
    return 'text'


def _select(name, data_type):
    if data_type in ('NCLOB', 'CLOB', 'NVARCHAR_LOB'):
        return f'TO_NVARCHAR(SUBSTR(X."{name}", 1, 2000)) AS "{name}"'
    return f'X."{name}" AS "{name}"'


def _shown(value, kind):
    """A field's value as text for the desk: '' when empty."""
    if value is None:
        return ''
    if kind == 'date':
        return _date(value) or ''
    if kind == 'number':
        number = Decimal(str(value))
        return '' if number == 0 else f'{number.normalize():f}'
    return _s(value).strip()


def read(company, doc_entry):
    """The GRPO's user fields: `{header: [field], lines: [{line_num, item_code,
    description, fields: [field]}]}`, a field being `{field, label, kind, value,
    editable}`. Shown: every field with a value, and the editable ones even
    when empty (to fill in). Only fields the bill has too; only open lines
    (the ones the bill copies). None when SAP has no such GRPO."""
    schema = _schema(company)
    columns = {}
    for r in _run(company, _COLUMNS_SQL, [schema], 'GRPO and bill user fields'):
        columns.setdefault(r['table'], {})[r['name']] = r['type']
    labels = {(r['table'], f"U_{r['alias']}"): (_s(r.get('label')), _s(r.get('type')))
              for r in _run(company, _LABELS_SQL.format(schema=schema), [], 'user field labels')}

    def fields_of(table, bill_table, row, editable):
        out = []
        for name, data_type in sorted(columns.get(table, {}).items()):
            if name not in columns.get(bill_table, {}):
                continue  # the bill has no such field: nothing to carry
            label, sap_type = labels.get((table, name), ('', ''))
            kind = editable.get(name) or _kind(sap_type, data_type)
            value = _shown(row.get(name), kind)
            if not value and name not in editable:
                continue
            out.append({'field': name, 'label': label or name[2:], 'kind': kind, 'value': value,
                        'editable': name in editable})
        return out

    head_cols = columns.get('OPDN', {})
    sql = (f'SELECT {", ".join(_select(n, t) for n, t in head_cols.items()) or "1 AS x"} '
           f'FROM "{schema}"."OPDN" X WHERE X."DocEntry" = ?')
    heads = _run(company, sql, [int(doc_entry)], 'GRPO user fields')
    if not heads:
        return None
    line_cols = columns.get('PDN1', {})
    sql = (f'SELECT X."LineNum" AS "line_num", X."ItemCode" AS "item_code", X."Dscription" AS "description"'
           f'{"".join(", " + _select(n, t) for n, t in line_cols.items())} '
           f'FROM "{schema}"."PDN1" X WHERE X."DocEntry" = ? AND X."LineStatus" = \'O\' ORDER BY X."LineNum"')
    lines = _run(company, sql, [int(doc_entry)], 'GRPO line user fields')
    return {
        'header': fields_of('OPDN', 'OPCH', heads[0], HEADER_EDITABLE),
        'lines': [{'line_num': int(r['line_num']), 'item_code': _s(r.get('item_code')),
                   'description': _s(r.get('description')),
                   'fields': fields_of('PDN1', 'PCH1', r, LINE_EDITABLE)} for r in lines],
    }


def _check(value, kind, where, problems):
    """One typed value, as stored ('' to clear), or None when it is wrong."""
    value = _s(value).strip()
    if not value:
        return ''
    if kind == 'date':
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            problems.append(f'{where}: {value!r} is not a date.')
            return None
    if kind == 'number':
        try:
            number = Decimal(value)
        except InvalidOperation:
            problems.append(f'{where}: {value!r} is not a number.')
            return None
        if number < 0:
            problems.append(f'{where} cannot be below zero.')
            return None
        return f'{number.normalize():f}'
    if len(value) > MAX_TEXT:
        problems.append(f'{where} is longer than {MAX_TEXT} characters.')
        return None
    return value


def clean(raw, problems):
    """The desk's changes to the GRPO's editable fields, as stored:
    `{header: {field: value}, lines: {"<line_num>": {field: value}}}`, or None
    for none. A field that is not editable is refused."""
    if raw in (None, '', {}):
        return None
    if not isinstance(raw, dict):
        problems.append('The GRPO fields are not in the expected shape.')
        return None
    header, lines = {}, {}
    for name, value in (raw.get('header') or {}).items():
        if name not in HEADER_EDITABLE:
            problems.append(f'The GRPO field {name} is not edited at Accounts.')
            continue
        checked = _check(value, HEADER_EDITABLE[name], f'GRPO {name[2:]}', problems)
        if checked is not None:
            header[name] = checked
    for line, values in (raw.get('lines') or {}).items():
        try:
            key = str(int(line))
        except (TypeError, ValueError):
            problems.append('A GRPO line is not a line number.')
            continue
        for name, value in (values or {}).items():
            if name not in LINE_EDITABLE:
                problems.append(f'The GRPO line field {name} is not edited at Accounts.')
                continue
            checked = _check(value, LINE_EDITABLE[name], f'GRPO line {key} {name[2:]}', problems)
            if checked is not None:
                lines.setdefault(key, {})[name] = checked
    return {'header': header, 'lines': lines} if header or lines else None


def _sap_value(value, kind):
    if kind == 'number':
        return float(Decimal(value))
    return value


def for_invoice(fields, changes):
    """`(header, {line_num: line})`: the user fields the A/P invoice is sent —
    the GRPO's values (`read`), the desk's `changes` laid over its editable
    ones. A field left empty is not sent."""
    changes = changes or {}
    head_changes, line_changes = changes.get('header') or {}, changes.get('lines') or {}

    def merged(found, overrides):
        out = {}
        for f in found:
            value = overrides.get(f['field'], f['value']) if f['editable'] else f['value']
            if value not in ('', None):
                out[f['field']] = _sap_value(value, f['kind'])
        return out

    header = merged(fields['header'], head_changes)
    lines = {ln['line_num']: merged(ln['fields'], line_changes.get(str(ln['line_num'])) or {})
             for ln in fields['lines']}
    return header, lines
