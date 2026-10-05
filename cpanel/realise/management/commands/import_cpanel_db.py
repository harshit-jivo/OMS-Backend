"""Copy production C_Panel's saved data (its db.sqlite3) into OMS's Control Panel tables.

C_Panel kept what people typed — targets, person mapping, aging remarks, claims,
credit locks, rate lists — in its own SQLite file. The same models live in OMS
as the `cp_realise` app (tables `c_panel.realise_*`), with the same columns (both
at migration 0029), so this is a straight table-for-table copy:

* primary keys are kept, so links between rows (a credit lock's snapshots)
  stay intact; the Postgres id sequences are moved past them afterwards;
* `updated_at` / `created_at` are kept as they were (auto_now is suspended);
* columns pointing at a C_Panel user (`updated_by`, `created_by`) are matched
  to the OMS user with the same username, else left empty — C_Panel's own
  users, groups and sessions are not copied (OMS has its own).

Dry run by default: prints what it would copy and writes nothing. `--apply`
writes, all in one transaction. A table that already has rows in OMS is
refused unless `--replace` is given, which empties it first.

    python manage.py import_cpanel_db C:/path/to/db.sqlite3            # dry run
    python manage.py import_cpanel_db C:/path/to/db.sqlite3 --apply
"""
import sqlite3
from contextlib import contextmanager
from datetime import timezone as dt_timezone

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, models, transaction
from django.utils import timezone

#: C_Panel model names, parents before children (CreditLockSnapshot -> CreditLock).
#: C_Panel's older target tables (MainGroupMaster, StateMaster, TargetMaster,
#: SegmentTarget) are not copied: OMS has no such tables — their data, if any, is
#: superseded by TargetNode.
MODELS = (
    'MonthlyTarget', 'TargetNode', 'TerritoryMapping', 'CityOwner',
    'TerritoryProductTarget', 'TerritoryItemTarget',
    'ClosingRemark', 'CreditLock', 'CreditLockSnapshot',
    'AgingRemark', 'AgingRemarkLine', 'AgingDueConfig', 'Claim', 'RateList',
)


def _table(model):
    """`c_panel.realise_targetnode`, not Django's quoted `c_panel"."realise_targetnode`."""
    return model._meta.db_table.replace('"."', '.')


@contextmanager
def _keep_timestamps(model):
    """bulk_create runs pre_save, which would stamp auto_now fields with now."""
    fields = [f for f in model._meta.concrete_fields
              if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False)]
    saved = [(f, f.auto_now, f.auto_now_add) for f in fields]
    for f in fields:
        f.auto_now = f.auto_now_add = False
    try:
        yield
    finally:
        for f, now, now_add in saved:
            f.auto_now, f.auto_now_add = now, now_add


class Command(BaseCommand):
    help = "Copy production C_Panel's db.sqlite3 data into OMS's Control Panel tables."

    def add_arguments(self, parser):
        parser.add_argument('sqlite_path')
        parser.add_argument('--apply', action='store_true', help='Write (default: dry run).')
        parser.add_argument('--replace', action='store_true',
                            help='Empty an OMS table that already has rows before copying into it.')

    def handle(self, sqlite_path, apply, replace, **options):
        try:
            src = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
            src.execute('select 1 from realise_targetnode limit 1')
        except sqlite3.Error as exc:
            raise CommandError(f'Not a C_Panel database: {sqlite_path} ({exc})')
        src.row_factory = sqlite3.Row

        users = self._user_map(src)
        plan = []
        for name in MODELS:
            model = apps.get_model('cp_realise', name)
            table = 'realise_' + model._meta.model_name
            if not src.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                continue
            rows = src.execute(f'select * from "{table}"').fetchall()
            existing = model.objects.count()
            if existing and not replace and rows:
                raise CommandError(f'{_table(model)} already has {existing} rows in OMS; '
                                   'rerun with --replace to overwrite it.')
            plan.append((model, rows, existing))

        # Build every row now, in the dry run too, so a value that will not convert
        # fails here rather than half-way through the write.
        self.authorless = 0
        built = [(model, [self._instance(model, row, users) for row in rows], existing)
                 for model, rows, existing in plan]
        for model, objs, existing in built:
            note = f'  (replaces {existing})' if existing and objs else ''
            self.stdout.write(f'{len(objs):>6}  {_table(model)}{note}')
        if self.authorless:
            self.stdout.write(f'{self.authorless} row(s) name a C_Panel user with no OMS account '
                              '(same username); they are copied with no author.')

        if not apply:
            self.stdout.write(self.style.WARNING('Dry run: nothing written. Add --apply to copy.'))
            return

        with transaction.atomic():
            # Children first when emptying, parents first when filling.
            for model, objs, existing in reversed(built):
                if existing and objs:
                    model.objects.all().delete()
            for model, objs, _ in built:
                if objs:
                    with _keep_timestamps(model):
                        model.objects.bulk_create(objs, batch_size=500)
            with connection.cursor() as cursor:
                for sql in connection.ops.sequence_reset_sql(no_style(), [m for m, _, _ in built]):
                    cursor.execute(sql)
        self.stdout.write(self.style.SUCCESS(
            f'Copied {sum(len(o) for _, o, _ in built)} rows into {sum(1 for _, o, _ in built if o)} tables.'))

    def _user_map(self, src):
        """C_Panel auth_user id -> OMS user id (same username), or None."""
        oms = {u.lower(): pk for pk, u in get_user_model().objects.values_list('pk', 'username')}
        return {row[0]: oms.get(str(row[1]).lower())
                for row in src.execute('select id, username from auth_user')}

    def _instance(self, model, row, users):
        values = {}
        keys = row.keys()
        for field in model._meta.concrete_fields:
            if field.column not in keys:
                continue
            raw = row[field.column]
            if field.is_relation:
                if field.related_model is get_user_model() and raw is not None:
                    raw = users.get(raw)
                    self.authorless += raw is None
                values[field.attname] = raw
                continue
            value = field.to_python(raw) if raw is not None else None
            # Both projects run USE_TZ=True: SQLite holds UTC as naive text.
            if isinstance(field, models.DateTimeField) and value is not None and timezone.is_naive(value):
                value = timezone.make_aware(value, dt_timezone.utc)
            values[field.attname] = value
        return model(**values)
