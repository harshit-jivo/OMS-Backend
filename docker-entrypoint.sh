#!/bin/sh
# ===========================================================================
#  Fix up mount ownership, then drop privileges and exec the real command.
#
#  WHY THIS EXISTS
#  ---------------
#  Two of the container's directories are mounts, and a mount arrives with
#  the HOST's ownership, overriding whatever the image set:
#
#    /app/logs     bind mount — owned by whoever created it on the host
#    /app/media    named volume — owned by root the first time Docker makes it
#
#  Django does not tolerate either being unwritable. settings.py builds a
#  RotatingFileHandler for logs/oms.log at IMPORT time, and a logging handler
#  opens its file eagerly — so an unwritable logs/ is not a degraded log, it
#  is a PermissionError before Django has finished starting, on every command
#  including `migrate`. The container then crash-loops with a stack trace
#  that points at logging and not at the mount, which is the actual cause.
#
#  The alternative — telling the operator to `chown -R 10001:10001` on the
#  host — works right up until someone forgets, and then produces exactly
#  that confusing failure. Doing it here means the stack comes up correctly
#  on a fresh host with no manual step.
#
#  The container still RUNS as an unprivileged user: root exists only for the
#  two chowns below, and `gosu` hands the process to `oms` before the
#  application starts. `exec` keeps it as PID 1, so Docker's stop signal
#  reaches gunicorn directly and shutdown stays graceful instead of waiting
#  out the 10-second kill timer.
# ===========================================================================
set -e

APP_USER=oms

if [ "$(id -u)" = '0' ]; then
    # Only the mounts. NOT `chown -R /app`: the source tree is already owned
    # correctly by the image, and recursing it on every start would add
    # seconds to boot and rewrite metadata for no reason.
    #
    # `|| true` throughout because a read-only or root-squashed mount (an NFS
    # share, say) will refuse — and if the directory is nevertheless writable
    # the app is fine. Failing the boot over a chown that may not have been
    # needed trades a working container for a tidy one.

    # logs/ is chowned RECURSIVELY, and that -R is load-bearing. Fixing only
    # the directory is not enough: a bind-mounted logs/ usually already
    # contains oms.log from a previous non-Docker run, owned by the host
    # user. The directory would then be writable while the FILE inside it was
    # not, and RotatingFileHandler opens that existing file for append at
    # startup — so the boot still dies with PermissionError on oms.log, which
    # looks identical to having done nothing at all.
    #
    # Cheap to recurse: RotatingFileHandler caps this directory at ~11 files
    # (10MB x 6 plus 5MB x 6), so there is no directory-walk cost to worry
    # about.
    [ -d /app/logs ] || mkdir -p /app/logs
    chown -R "${APP_USER}:${APP_USER}" /app/logs 2>/dev/null || true

    # media/ and staticfiles/ get the directory ONLY, deliberately. media/ is
    # a growing pile of user uploads and recursing it would put an unbounded
    # directory walk in front of every container start. It is also
    # unnecessary: a fresh named volume inherits the image's oms ownership,
    # and every file written after that is written BY oms. Only the directory
    # bit matters, and that is what lets new files be created.
    for d in /app/media /app/staticfiles; do
        [ -d "$d" ] || mkdir -p "$d"
        chown "${APP_USER}:${APP_USER}" "$d" 2>/dev/null || true
    done

    exec gosu "$APP_USER" "$@"
fi

# Already unprivileged — someone passed `user:` in compose or `--user` on the
# CLI. Respect it and skip straight to the command; the chowns above would
# only fail anyway.
exec "$@"
