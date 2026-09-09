"""Redact production data in staging — Phase 0.4.

A migration rehearsal needs the SHAPE of production: row counts, null
distributions, the values that violate a constraint you are about to add. It
does not need real GSTINs, party contacts, invoice values or password hashes,
and a staging database is by definition looser about who can reach it.

So this keeps every row and every relationship, and overwrites the fields whose
real values buy the rehearsal nothing. Row counts are unchanged, which is the
property that matters — a scrub that deleted rows would make the rehearsal a
lie about the thing it is rehearsing.

    python scripts/scrub_staging.py

Refuses to run against anything whose name does not end in `_staging`.
"""
import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OMS.staging_settings')

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402

#: `table: {column: sql_expression}`. The expression is evaluated per row, so
#: `id` is available to keep values distinct where a UNIQUE constraint needs
#: them to be.
#:
#: Deliberately NOT a blanket "null everything sensitive": a NULL where
#: production has a value changes the null distribution, and that is one of the
#: things a rehearsal is checking.
SCRUBS = {
    'users_user': {
        # A known, useless hash. Every staging account gets the same one, and
        # none of them can be logged into with a real password.
        'password': "'!scrubbed'",
        'email': "'user' || id || '@staging.invalid'",
        'first_name': "'First' || id",
        'last_name': "'Last' || id",
    },
    'sap_parties': {
        'party_name': "'Party ' || id",
        'gst_number': "'00SCRUBBED' || lpad(id::text, 5, '0')",
        'phone': "'0000000000'",
        'email': "'party' || id || '@staging.invalid'",
    },
    'sap_party_addresses': {
        'gst_number': "'00SCRUBBED' || lpad(id::text, 5, '0')",
        'address_name': "'Address ' || id",
        'street': "'Street ' || id",
    },
}


def columns_of(table):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_name = %s', [table])
        return {row[0] for row in cursor.fetchall()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', action='store_true',
                        help='Skip the confirmation prompt.')
    args = parser.parse_args()

    name = connection.settings_dict['NAME']
    if not name.endswith('_staging'):
        raise SystemExit(
            f'Refusing to scrub {name!r}: the name does not end in '
            f'"_staging". This script issues UPDATEs against every row it '
            f'touches and there is no undo.')

    print(f'scrubbing {name}')
    if not args.yes:
        if input('type the database name to confirm: ').strip() != name:
            raise SystemExit('aborted')

    total = 0
    with transaction.atomic():
        for table, fields in SCRUBS.items():
            present = columns_of(table)
            if not present:
                print(f'  {table}: not in this database, skipping')
                continue
            # Only the columns that actually exist. Schemas drift between
            # environments, and one renamed column must not abort a scrub
            # part-way — leaving half the database redacted and half not.
            usable = {c: e for c, e in fields.items() if c in present}
            missing = set(fields) - set(usable)
            if missing:
                print(f'  {table}: no such column(s) {sorted(missing)}, skipping those')
            if not usable:
                continue
            assignments = ', '.join(f'"{c}" = {e}' for c, e in usable.items())
            with connection.cursor() as cursor:
                cursor.execute(f'UPDATE "{table}" SET {assignments}')
                print(f'  {table}: {cursor.rowcount} rows, '
                      f'{len(usable)} column(s)')
                total += cursor.rowcount

    print(f'\n{total} rows scrubbed. Row counts and relationships are unchanged '
          f'— only values were overwritten.')


if __name__ == '__main__':
    main()
