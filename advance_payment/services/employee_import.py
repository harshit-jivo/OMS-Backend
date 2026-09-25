"""Read JSAP's employee export into `Employee` field values.

Shared by migration 0002 and `manage.py load_jsap_employees`, so the file is
read the same way by both. It returns plain dicts of field values, never
model instances, so a migration can use it with its historical model.

The export is '|'-separated with a header line. It holds personal data and is
kept out of git (`advance_payment/data/*.psv` in .gitignore).
"""
import csv
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / 'data' / 'jsap_employees.psv'

COLUMNS = ('EmployeeId', 'EmployeeCode', 'EmployeeName', 'Email', 'Phone', 'Designation',
           'RoleTypeId', 'Gender', 'IsActive')


class ImportFileError(ValueError):
    """The file is missing a column, or a row cannot be read."""


def _value(raw):
    raw = (raw or '').strip()
    return None if raw in ('', 'NULL') else raw


def _row_fields(row, line):
    try:
        role = _value(row['RoleTypeId'])
        gender = _value(row['Gender'])
        return {
            'employee_id': int(row['EmployeeId']),
            'employee_code': row['EmployeeCode'].strip().upper(),
            'employee_name': ' '.join(row['EmployeeName'].split()),
            'email': _value(row['Email']),
            'phone': _value(row['Phone']),
            'designation': _value(row['Designation']),
            'role': int(role) if role else 3,
            'gender': gender if gender in ('M', 'F') else None,
            'is_active': (row['IsActive'] or '').strip() == '1',
            'is_deleted': False,
            # `created_on` is left to the model default: the day it is loaded.
        }
    except (KeyError, ValueError, AttributeError) as exc:
        raise ImportFileError(f'line {line}: {exc}') from exc


def read_rows(path):
    """Every row of the export as `Employee` field values."""
    path = Path(path)
    with path.open(encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle, delimiter='|')
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ImportFileError(f'{path.name} is missing column(s): {", ".join(missing)}')
        return [_row_fields(row, line) for line, row in enumerate(reader, start=2)]
