"""Point SAP's budget gate at OMS's own approval table ("option B").

SAP refuses to post a draft whose lines have no approval row: its
SBO_SP_TRANSACTIONNOTIFICATION reads the custom table "tbl_Draft_Approvals"
(JSAP's) by name — 33 places in Oil, 39 in Beverages. Rather than edit those,
the company schema is switched over like this:

    OMS_BUDGET_APPROVALS          OMS's own table: one row per draft line
                                  (A approved, V moved to the next stage,
                                  R rejected, NULL pending). At the switch,
                                  JSAP's rows are copied in once (DecidedBy
                                  'JSAP'), so a draft JSAP already approved
                                  stays postable.
    tbl_Draft_Approvals_JSAP      JSAP's table, renamed and kept as history;
                                  nothing reads it afterwards
    tbl_Draft_Approvals  (VIEW)   what the gate reads: OMS's table only

The view has every column JSAP's table had, with the same names and types, so
no procedure that reads it changes. OMS fills the ones the gate checks:
DocEntry, ObjType, LineNum, VisOrder, AcctCode, CardCode, DocDate,
ApprovedStatus and VerifiedStatus (OMS sets 'V' when a line is fully
approved: the Beverages A/R gate checks it). The rest are NULL.

SAP's procedures belong to SYSTEM and run with its rights, so the view is
granted to the owners of the procedures that read it. The view reads only
OMS's table, which is why OMS (the view's owner) is able to grant it.

    python manage.py budget_gate --schema TEST_JIVO_OIL_HANADB              # status only
    python manage.py budget_gate --schema TEST_JIVO_OIL_HANADB --apply
    python manage.py budget_gate --schema TEST_JIVO_OIL_HANADB --rollback   # back to JSAP's table

Only TEST_ schemas unless --allow-live: the live companies are still gated by
JSAP. Each step is checked, procedures that read the table are recompiled and
must come back valid, and a failure after the rename puts JSAP's table back.
"""
from django.core.management.base import BaseCommand, CommandError

from hana.services.connection import HANAConnection

GATE = 'tbl_Draft_Approvals'
JSAP = 'tbl_Draft_Approvals_JSAP'
OMS = 'OMS_BUDGET_APPROVALS'

#: OMS's table. Keyed like the gate looks lines up: object type, draft, line.
OMS_DDL = '''CREATE COLUMN TABLE "{schema}"."OMS_BUDGET_APPROVALS" (
    "ObjType"        INTEGER       NOT NULL,
    "DocEntry"       INTEGER       NOT NULL,
    "LineNum"        INTEGER       NOT NULL,
    "VisOrder"       INTEGER,
    "AcctCode"       NVARCHAR(50),
    "CardCode"       NVARCHAR(50),
    "DocDate"        TIMESTAMP,
    "BUDGET"         NVARCHAR(50),
    "SUB_BUDGET"     NVARCHAR(50),
    "AMOUNT"         DECIMAL(19, 2),
    "ApprovedStatus" NVARCHAR(1),
    "VerifiedStatus" NVARCHAR(1),
    "Remarks"        NVARCHAR(5000),
    "OmsRequestId"   INTEGER,
    "DecidedBy"      NVARCHAR(150),
    "DecidedAt"      TIMESTAMP,
    "CreatedAt"      TIMESTAMP     DEFAULT CURRENT_UTCTIMESTAMP,
    "UpdatedAt"      TIMESTAMP     DEFAULT CURRENT_UTCTIMESTAMP,
    PRIMARY KEY ("ObjType", "DocEntry", "LineNum")
)'''

#: JSAP column -> the OMS expression that fills it on an OMS row.
FROM_OMS = {
    'DocEntry': 'o."DocEntry"', 'ObjType': 'o."ObjType"', 'LineNum': 'o."LineNum"',
    'VisOrder': 'o."VisOrder"', 'AcctCode': 'o."AcctCode"', 'CardCode': 'o."CardCode"',
    'DocDate': 'o."DocDate"', 'BUDGET': 'o."BUDGET"', 'SUB_BUDGET': 'o."SUB_BUDGET"', 'AMOUNT': 'o."AMOUNT"',
    'ApprovedStatus': 'o."ApprovedStatus"', 'VerifiedStatus': 'o."VerifiedStatus"',
    'ACOMMENT': 'o."Remarks"', 'UpdateDate': 'o."UpdatedAt"', 'CreatedDate': 'o."CreatedAt"',
}

#: JSAP's rows, one per line (its duplicates never disagree on status), into OMS's table.
IMPORT_SQL = '''INSERT INTO "{schema}"."OMS_BUDGET_APPROVALS"
    ("ObjType", "DocEntry", "LineNum", "VisOrder", "AcctCode", "CardCode", "DocDate", "BUDGET", "SUB_BUDGET",
     "AMOUNT", "ApprovedStatus", "VerifiedStatus", "DecidedBy")
SELECT "ObjType", "DocEntry", "LineNum", MAX("VisOrder"), MAX("AcctCode"), MAX("CardCode"), MAX("DocDate"),
       MAX("BUDGET"), MAX("SUB_BUDGET"), MAX("AMOUNT"), MAX(LEFT("ApprovedStatus", 1)),
       MAX(LEFT("VerifiedStatus", 1)), 'JSAP'
FROM "{schema}"."{source}"
WHERE "ObjType" IS NOT NULL AND "DocEntry" IS NOT NULL AND "LineNum" IS NOT NULL
GROUP BY "ObjType", "DocEntry", "LineNum"'''

_LOBS = {'CLOB': 'TO_CLOB', 'NCLOB': 'TO_NCLOB', 'BLOB': 'TO_BLOB'}


def _typed(expr, col):
    """`expr` as the JSAP column's exact type, so both halves of the UNION agree."""
    kind = col['type']
    if kind in _LOBS:
        return f'{_LOBS[kind]}({expr})'
    if kind in ('NVARCHAR', 'VARCHAR', 'VARBINARY'):
        return f'CAST({expr} AS {kind}({col["length"]}))'
    if kind == 'DECIMAL':
        return f'CAST({expr} AS DECIMAL({col["length"]}, {col["scale"] or 0}))'
    return f'CAST({expr} AS {kind})'


def view_select(schema, columns):
    """The view's SELECT: OMS's table, shaped exactly like JSAP's (`columns`, its name and type)."""
    shaped = ', '.join(f'{_typed(FROM_OMS.get(c["name"], "NULL"), c)} AS "{c["name"]}"' for c in columns)
    return f'SELECT {shaped} FROM "{schema}"."{OMS}" o'


class Command(BaseCommand):
    help = "Switch a SAP company's budget gate to OMS's own approval table (or back)."

    def add_arguments(self, parser):
        parser.add_argument('--schema', required=True, help='The SAP company schema, e.g. TEST_JIVO_OIL_HANADB.')
        parser.add_argument('--apply', action='store_true', help='Switch the gate to OMS.')
        parser.add_argument('--rollback', action='store_true', help="Put JSAP's table back as the gate.")
        parser.add_argument('--allow-live', action='store_true', help='Permit a schema not starting with TEST_.')

    def handle(self, *args, **opts):
        schema = opts['schema'].strip()
        if not schema.upper().startswith('TEST_') and not opts['allow_live']:
            raise CommandError(f'{schema} is a live company. JSAP still gates it; pass --allow-live only at cut-over.')
        if opts['apply'] and opts['rollback']:
            raise CommandError('Choose --apply or --rollback, not both.')
        with HANAConnection() as hana:
            self.hana, self.schema = hana, schema
            if opts['apply']:
                self.apply()
            elif opts['rollback']:
                self.rollback()
            self.status()

    # -- reading --------------------------------------------------------------

    def q(self, sql):
        return self.hana.execute(sql)

    def kind(self, name):
        """'TABLE', 'VIEW' or None for an object of this schema."""
        if self.q(f"SELECT 1 FROM SYS.TABLES WHERE SCHEMA_NAME = '{self.schema}' AND TABLE_NAME = '{name}'"):
            return 'TABLE'
        if self.q(f"SELECT 1 FROM SYS.VIEWS WHERE SCHEMA_NAME = '{self.schema}' AND VIEW_NAME = '{name}'"):
            return 'VIEW'
        return None

    def columns(self, table):
        return [{'name': r['c'], 'type': r['t'], 'length': r['l'], 'scale': r['s']} for r in self.q(
            f'''SELECT COLUMN_NAME "c", DATA_TYPE_NAME "t", LENGTH "l", SCALE "s" FROM SYS.TABLE_COLUMNS
                WHERE SCHEMA_NAME = '{self.schema}' AND TABLE_NAME = '{table}' ORDER BY POSITION''')]

    def readers(self):
        return self.q(f'''SELECT PROCEDURE_NAME "p", IS_VALID "ok" FROM SYS.PROCEDURES
                          WHERE SCHEMA_NAME = '{self.schema}' AND UPPER(DEFINITION) LIKE '%TBL_DRAFT_APPROVALS%' ''')

    def count(self, name):
        return self.q(f'SELECT COUNT(*) "n" FROM "{self.schema}"."{name}"')[0]['n']

    def status(self):
        self.stdout.write(f'\n{self.schema}:')
        for name in (GATE, JSAP, OMS):
            kind = self.kind(name)
            self.stdout.write(f'  {name:<28} {kind or "-":<6} {self.count(name) if kind else ""}')
        for r in self.readers():
            self.stdout.write(f'  reader {r["p"]:<34} valid={r["ok"]}')

    # -- changing -------------------------------------------------------------

    def recompile(self):
        for r in self.readers():
            self.q(f'ALTER PROCEDURE "{self.schema}"."{r["p"]}" RECOMPILE')
        bad = [r['p'] for r in self.readers() if str(r['ok']).upper() != 'TRUE']
        if bad:
            raise CommandError(f'Procedures not valid after recompiling: {bad}')

    def apply(self):
        if self.kind(GATE) == 'VIEW' and self.kind(JSAP) == 'TABLE':
            self.stdout.write('Already switched to OMS; nothing to do.')
            return
        if self.kind(GATE) != 'TABLE':
            raise CommandError(f'{GATE} is not a table here; refusing to guess what this schema holds.')
        if self.kind(OMS) is None:
            self.q(OMS_DDL.format(schema=self.schema))
            self.stdout.write(f'created {OMS}')
        elif not any(c['name'] == 'VerifiedStatus' for c in self.columns(OMS)):
            self.q(f'ALTER TABLE "{self.schema}"."{OMS}" ADD ("VerifiedStatus" NVARCHAR(1))')
            self.stdout.write(f'added VerifiedStatus to {OMS}')
        if self.count(OMS) == 0:
            self.q(IMPORT_SQL.format(schema=self.schema, source=GATE))
            self.stdout.write(f"copied JSAP's {self.count(GATE)} rows into {OMS} as {self.count(OMS)} lines")
        columns = self.columns(GATE)
        owners = sorted({r['o'] for r in self.q(
            f'''SELECT OWNER_NAME "o" FROM SYS.PROCEDURES WHERE SCHEMA_NAME = '{self.schema}'
                AND UPPER(DEFINITION) LIKE '%TBL_DRAFT_APPROVALS%' ''')} - {self.me()})
        # Prove the view compiles before renaming anything.
        self.q(f'SELECT COUNT(*) "n" FROM ({view_select(self.schema, columns)})')
        self.q(f'RENAME TABLE "{self.schema}"."{GATE}" TO "{JSAP}"')
        self.stdout.write(f'renamed {GATE} -> {JSAP}')
        try:
            self.q(f'CREATE VIEW "{self.schema}"."{GATE}" AS {view_select(self.schema, columns)}')
            self.stdout.write(f'created view {GATE}')
            for owner in owners:
                self.q(f'GRANT SELECT ON "{self.schema}"."{GATE}" TO "{owner}"')
                self.stdout.write(f'granted SELECT on the view to {owner}')
            self.recompile()
            seen, expected = self.count(GATE), self.count(OMS)
            if seen != expected:
                raise CommandError(f'The view shows {seen} rows; {OMS} has {expected}.')
        except Exception:
            self.stdout.write(self.style.ERROR('Failed after the rename; putting JSAP back.'))
            self.rollback()
            raise
        self.stdout.write(self.style.SUCCESS(f'{self.schema}: the gate now reads OMS ({seen} lines).'))

    def me(self):
        return self.q('SELECT CURRENT_USER "u" FROM DUMMY')[0]['u']

    def rollback(self):
        if self.kind(GATE) == 'VIEW':
            self.q(f'DROP VIEW "{self.schema}"."{GATE}"')
            self.stdout.write(f'dropped view {GATE}')
        if self.kind(JSAP) == 'TABLE' and self.kind(GATE) is None:
            self.q(f'RENAME TABLE "{self.schema}"."{JSAP}" TO "{GATE}"')
            self.stdout.write(f'renamed {JSAP} -> {GATE}')
        self.recompile()
        self.stdout.write(f'{self.schema}: the gate reads JSAP\'s table again. ({OMS} is kept.)')
