"""Build a staging database from production — Phase 0.4.

Until now every migration has been applied straight to the live database on
`.117`, and there has been nowhere to find out what one does first. This makes
that place.

    python scripts/make_staging.py            # dump + restore
    python scripts/make_staging.py --scrub    # ...then redact the PII

The whole database is 61 MB, so this takes seconds and is meant to be re-run
before every rehearsal rather than kept alive and allowed to drift. A staging
database that has been up for three months is not a rehearsal environment; it
is a third environment nobody trusts.

Credentials come from Django settings, so they come from `.env` and are not
duplicated here. Nothing in this file is a secret.

Where the target lives
----------------------
By default the target is created on the SAME server as the source, which needs
CREATEDB on the production role. A well-configured deployment does not grant
that — `.117` does not — so the normal invocation names a different server:

    python scripts/make_staging.py         --target-host 127.0.0.1 --target-user postgres

That is the better arrangement anyway. A staging database on its own host
cannot be reached by a mistake aimed at production, and the production role
keeps the privileges it should have.

Safety
------
The one catastrophic mistake available to this script is restoring over
production, so that is guarded three ways: the target must differ from the
source, it must end in `_staging`, and `--force` is required to drop a target
that already exists. The guards are checked before `pg_dump` runs, not after.

The dump file is deleted when the script finishes unless `--keep-dump` is
given. It is a complete copy of production — party master data, GSTINs,
invoice values — and leaving it on a developer's disk is the quiet way that
data escapes.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OMS.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

#: Any target must end with this. The check is a guard against a typo in
#: `--target` silently naming the production database.
REQUIRED_SUFFIX = '_staging'

#: Windows installs the client tools outside PATH more often than not.
LIKELY_BIN_DIRS = [
    Path(r'C:\Program Files\PostgreSQL'),
    Path('/usr/bin'),
    Path('/usr/local/bin'),
]


def find_tool(name):
    """Locate a Postgres client binary.

    The client may be NEWER than the server (pg_dump 18 against server 16 is
    supported); the reverse is not, and pg_dump says so loudly, so no version
    check is duplicated here.
    """
    from shutil import which

    found = which(name)
    if found:
        return found
    # Exact name, plus the Windows `.exe` suffix — NOT a `{name}*` glob. That
    # glob matched `pg_dumpall.exe` when asked for `pg_dump`, and reverse
    # sorting (to prefer the newest major version directory) put it first. The
    # failure was loud, but a prefix match on tool names is wrong regardless.
    wanted = {name, f'{name}.exe'}
    for root in LIKELY_BIN_DIRS:
        if not root.exists():
            continue
        # Reverse so the highest major version wins: .../18/bin before .../16/bin.
        for version_dir in sorted(root.glob('*/bin'), reverse=True):
            for candidate in version_dir.iterdir():
                if candidate.name in wanted:
                    return str(candidate)
        for candidate in wanted:
            direct = root / candidate
            if direct.exists():
                return str(direct)
    raise SystemExit(
        f'Could not find `{name}`. Install the PostgreSQL client tools or put '
        f'them on PATH.')


def db_env(password):
    """PGPASSWORD via the environment, never on the command line — an argv is
    readable by every other process on the machine."""
    env = os.environ.copy()
    env['PGPASSWORD'] = password
    return env


def run(cmd, env, what):
    print(f'  {what}...')
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f'{what} failed (exit {result.returncode})')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default=None,
                        help=f'Staging database name (must end in '
                             f'{REQUIRED_SUFFIX!r}).')
    parser.add_argument('--target-host', default=None,
                        help='Restore to a DIFFERENT server than the source. '
                             'Preferred: the production role deliberately has '
                             'no CREATEDB, and a staging database on its own '
                             'host cannot be reached by a mistake aimed at it.')
    parser.add_argument('--target-port', default=None)
    parser.add_argument('--target-user', default=None)
    parser.add_argument('--target-password', default=None,
                        help='Or set PGPASSWORD_TARGET in the environment, '
                             'which keeps it out of your shell history and '
                             'out of argv.')
    parser.add_argument('--dump-file', default=None,
                        help='Where to write the dump (default: a temp file '
                             'beside this script).')
    parser.add_argument('--force', action='store_true',
                        help='Drop the target if it already exists.')
    parser.add_argument('--scrub', action='store_true',
                        help='Run scripts/scrub_staging.py afterwards.')
    parser.add_argument('--keep-dump', action='store_true',
                        help='Do not delete the dump file when finished. It '
                             'contains production data — see the warning '
                             'printed at the end.')
    args = parser.parse_args()

    source = settings.DATABASES['default']
    source_name = source['NAME']
    target_name = args.target or f'{source_name}{REQUIRED_SUFFIX}'

    # --- Guards, before anything runs -------------------------------------
    if target_name == source_name:
        raise SystemExit(
            f'Refusing to run: target and source are the same database '
            f'({source_name!r}).')
    if not target_name.endswith(REQUIRED_SUFFIX):
        raise SystemExit(
            f'Refusing to run: target {target_name!r} does not end in '
            f'{REQUIRED_SUFFIX!r}. This guard exists so a typo cannot name '
            f'the production database.')

    pg_dump = find_tool('pg_dump')
    pg_restore = find_tool('pg_restore')
    psql = find_tool('psql')

    src_env = db_env(source['PASSWORD'])
    src_conn = ['-h', source['HOST'], '-p', str(source['PORT']),
                '-U', source['USER']]

    # The target defaults to the same server, which is the simple case but
    # needs CREATEDB on the production role — something a well-configured
    # deployment does not grant. `--target-host` is the way out, and is the
    # better arrangement regardless.
    tgt_host = args.target_host or source['HOST']
    tgt_port = args.target_port or source['PORT']
    tgt_user = args.target_user or source['USER']
    tgt_password = (args.target_password or os.environ.get('PGPASSWORD_TARGET')
                    or (source['PASSWORD'] if args.target_host is None else None))
    if tgt_password is None:
        raise SystemExit(
            '--target-host was given but no target password. Pass '
            '--target-password or set PGPASSWORD_TARGET.')
    tgt_env = db_env(tgt_password)
    tgt_conn = ['-h', tgt_host, '-p', str(tgt_port), '-U', tgt_user]

    dump_file = Path(args.dump_file or (BASE_DIR / 'scripts' / '_staging.dump'))

    print(f'source : {source_name} @ {source["HOST"]}')
    print(f'target : {target_name} @ {tgt_host}')

    # --- Does the target already exist? -----------------------------------
    exists = run(
        [psql, *tgt_conn, '-d', 'postgres', '-tAc',
         f"SELECT 1 FROM pg_database WHERE datname = '{target_name}'"],
        tgt_env, 'checking for an existing target').stdout.strip() == '1'

    if exists and not args.force:
        raise SystemExit(
            f'{target_name!r} already exists. Re-run with --force to drop and '
            f'rebuild it. (Rebuilding from scratch is the intended workflow — '
            f'a staging database that has drifted is worse than none.)')

    # --- Dump --------------------------------------------------------------
    run([pg_dump, *src_conn, '--format=custom', '--no-owner', '--no-privileges',
         '--file', str(dump_file), source_name],
        src_env, f'dumping {source_name}')
    size_mb = dump_file.stat().st_size / (1024 * 1024)
    print(f'  dump is {size_mb:.1f} MB')

    # --- Recreate the target ----------------------------------------------
    if exists:
        # Terminate first: DROP DATABASE fails while anything is connected,
        # and a forgotten psql session is the usual culprit.
        run([psql, *tgt_conn, '-d', 'postgres', '-c',
             f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             f"WHERE datname = '{target_name}' AND pid <> pg_backend_pid()"],
            tgt_env, 'disconnecting existing sessions')
        run([psql, *tgt_conn, '-d', 'postgres', '-c',
             f'DROP DATABASE "{target_name}"'], tgt_env,
            'dropping the old target')

    run([psql, *tgt_conn, '-d', 'postgres', '-c',
         f'CREATE DATABASE "{target_name}"'], tgt_env, 'creating the target')

    # --- Restore -----------------------------------------------------------
    # `--no-owner` because the staging database is restored by whoever runs
    # this, who is not necessarily the production owner role. Exit code is
    # tolerated: pg_restore reports a non-zero status for benign notices such
    # as an extension that already exists, and `--exit-on-error` is
    # deliberately NOT passed so one such notice does not discard the restore.
    print('  restoring...')
    result = subprocess.run(
        [pg_restore, *tgt_conn, '--dbname', target_name, '--no-owner',
         '--no-privileges', str(dump_file)],
        env=tgt_env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'  pg_restore exited {result.returncode} (usually benign); '
              f'verifying...')

    # --- Verify, rather than trust the exit code ---------------------------
    tables = run(
        [psql, *tgt_conn, '-d', target_name, '-tAc',
         "SELECT count(*) FROM information_schema.tables "
         "WHERE table_schema NOT IN ('pg_catalog','information_schema')"],
        tgt_env, 'counting restored tables').stdout.strip()
    if int(tables or 0) == 0:
        raise SystemExit('Restore produced no tables. Check the output above.')
    print(f'  {tables} tables restored')

    if not args.keep_dump:
        dump_file.unlink(missing_ok=True)
    else:
        print(f'\n  !! {dump_file} holds PRODUCTION data — party master data, '
              f'GSTINs and invoice values. Delete it when finished.')

    if args.scrub:
        print('\nscrubbing...')
        subprocess.run([sys.executable, str(BASE_DIR / 'scripts' / 'scrub_staging.py'),
                        '--yes'], check=True)

    print(f"""
Done. Point Django at it with:

    python manage.py migrate --settings=OMS.staging_settings --plan
    python manage.py migrate --settings=OMS.staging_settings
    python manage.py test    --settings=OMS.test_settings

Rebuild it from scratch before the next rehearsal rather than reusing it.""")


if __name__ == '__main__':
    main()
