"""Deactivate browser Web Push subscriptions that are no longer valid.

Why this exists
---------------
A browser ``PushSubscription`` dies silently: the user clears site data, the
endpoint expires, the browser garbage-collects it, or the machine is gone. The
push service then answers ``404``/``410 Gone`` (and ``401``/``403`` for a
subscription bound to an old VAPID key). ``deliver_web_push`` already retires
such rows the instant a real notification hits them, so during normal traffic
the table self-heals. This command is the **safety net** for subscriptions that
never receive a notification (they would otherwise linger active forever).

Validation is a real Web Push round-trip -- there is no "receipt" API as there
is for Expo. We send a tiny data-only *keepalive* payload the service worker
handles silently (see ``public/service-worker.js``), so **nothing is ever shown
to a user**; we only care about the HTTP status the push service returns.

Designed to run unattended from cron / Windows Task Scheduler every night -- see
``docs/web-push-cleanup.md``. It:

* appends a timestamped record of every run (success or failure) to
  ``settings.WEB_PUSH_CLEANUP_LOG``;
* takes an atomic lock (``settings.WEB_PUSH_CLEANUP_LOCK``) so a slow run can
  never overlap the next night's run;
* never raises past ``handle()`` -- failures are logged in full and the process
  exits non-zero, so tomorrow's run is unaffected;
* processes every subscription independently -- one failure never stops the rest.

Usage::

    python manage.py prune_web_push_subscriptions             # validate + prune
    python manage.py prune_web_push_subscriptions --dry-run   # report only
"""

import os
import time
import traceback
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from orders.models import WebPushSubscription
from orders.webpush import (
    DEACTIVATED,
    DELIVERED,
    KEEPALIVE_PAYLOAD,
    deliver_web_push,
)

# A lock older than this is assumed to be from a crashed run and is broken, so a
# hard kill can never disable the cleanup permanently.
_LOCK_STALE_AFTER_SECONDS = 60 * 60


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_log(text):
    """Append to the run log. Logging must never break the cleanup itself."""
    try:
        path = Path(settings.WEB_PUSH_CLEANUP_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:  # noqa: BLE001 - a bad log path must not stop the sweep
        pass


def _acquire_lock():
    """Atomically take the lock. Returns the path, or None if already running."""
    path = Path(settings.WEB_PUSH_CLEANUP_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > _LOCK_STALE_AFTER_SECONDS:
            path.unlink(missing_ok=True)  # stale: previous run died

    try:
        # O_EXCL is atomic on Windows and POSIX: whoever creates the file wins,
        # so two runs can never both proceed.
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    try:
        os.write(fd, f"{os.getpid()} {_now()}".encode())
    finally:
        os.close(fd)
    return path


class Command(BaseCommand):
    help = "Validate active Web Push subscriptions and deactivate dead ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deactivated without changing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        lock = _acquire_lock()
        if lock is None:
            message = (
                f"[{_now()}]\nSkipped: a web push cleanup is already running.\n\n"
            )
            _append_log(message)
            self.stdout.write(self.style.WARNING(message.strip()))
            return

        started = _now()
        try:
            subscriptions = list(
                WebPushSubscription.objects.filter(is_active=True)
            )
            checked = len(subscriptions)
            valid = 0
            removed = 0
            errored = 0

            for subscription in subscriptions:
                # deliver_web_push already deactivates dead rows (404/410/401/403)
                # and swallows transient errors -- one bad endpoint never stops
                # the loop. In --dry-run we still probe (to classify each one) but
                # revert any deactivation it performs.
                was_active = subscription.is_active
                outcome = deliver_web_push(subscription, KEEPALIVE_PAYLOAD)
                if outcome == DELIVERED:
                    valid += 1
                elif outcome == DEACTIVATED:
                    removed += 1
                    if dry_run and was_active:
                        WebPushSubscription.objects.filter(
                            pk=subscription.pk
                        ).update(is_active=True)
                else:  # transient error -- left active, retried next run
                    errored += 1

            suffix = " (dry-run: nothing changed)" if dry_run else ""
            report = (
                f"[{started}]\n"
                f"Checking Web Push Subscriptions...\n\n"
                f"Checked: {checked}\n"
                f"Valid: {valid}\n"
                f"Removed: {removed}\n"
                f"Errors (kept, retried later): {errored}\n\n"
                f"Completed Successfully{suffix}\n\n"
            )
            _append_log(report)
            self.stdout.write(report.strip())
        except Exception:  # noqa: BLE001 - report, never crash the schedule
            failure = (
                f"[{_now()}]\n\n"
                f"ERROR:\n{traceback.format_exc()}\n"
                f"Cleanup Failed\n\n"
            )
            _append_log(failure)
            self.stderr.write(failure.strip())
            raise SystemExit(1)
        finally:
            Path(lock).unlink(missing_ok=True)
