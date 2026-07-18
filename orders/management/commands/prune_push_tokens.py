"""Deactivate Expo push tokens that the push service reports as dead.

Why this exists
---------------
An Expo *ticket* only means "Expo accepted the message". The real delivery
outcome -- notably ``DeviceNotRegistered`` (the app was uninstalled, its data
cleared, or the token rotated) -- is reported only in the delivery *receipt*.
``send_push_notification`` checks receipts as notifications go out, so tokens are
pruned during normal traffic. This command is the safety net: it sweeps tokens
belonging to users who receive no notifications (their dead tokens would linger
forever) and cleans up anything left behind.

It sends a SILENT, data-only push: no ``title``/``body``, so nothing is ever
displayed to a user. It exists purely to obtain a receipt per token.

Designed to run unattended from cron / Windows Task Scheduler every night --
see ``docs/push-token-cleanup.md``. Accordingly it:

* appends a timestamped record of every run (success or failure) to
  ``settings.PUSH_TOKEN_CLEANUP_LOG``;
* takes a lock (``settings.PUSH_TOKEN_CLEANUP_LOCK``) so a slow run can never
  overlap the next night's run;
* never raises past ``handle()`` -- failures are logged in full and the process
  exits non-zero, so tomorrow's run is unaffected.

Usage::

    python manage.py prune_push_tokens             # prune dead tokens
    python manage.py prune_push_tokens --dry-run   # report only, change nothing
"""

import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from orders.models import PushToken
from orders.notifications import EXPO_PUSH_URL, EXPO_RECEIPT_URL

# Expo accepts up to 100 messages per request.
_BATCH_SIZE = 100
_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
# A lock older than this is assumed to be from a crashed run and is broken, so a
# hard kill can never disable the cleanup permanently.
_LOCK_STALE_AFTER_SECONDS = 60 * 60


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_log(text):
    """Append to the run log. Logging must never break the cleanup itself."""
    try:
        path = Path(settings.PUSH_TOKEN_CLEANUP_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:  # noqa: BLE001 - a bad log path must not stop the sweep
        pass


def _acquire_lock():
    """Atomically take the lock. Returns the path, or None if already running."""
    path = Path(settings.PUSH_TOKEN_CLEANUP_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > _LOCK_STALE_AFTER_SECONDS:
            path.unlink(missing_ok=True)  # stale: previous run died

    try:
        # O_EXCL is atomic on both Windows and POSIX: whoever creates the file
        # wins, so two runs can never both proceed.
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    try:
        os.write(fd, f"{os.getpid()} {_now()}".encode())
    finally:
        os.close(fd)
    return path


def _check_tokens(wait_seconds):
    """Return ``(checked, dead_tokens)`` using Expo tickets + delivery receipts."""
    tokens = list(
        PushToken.objects.filter(is_active=True)
        .values_list("token", flat=True)
        .distinct()
    )
    dead = []

    for start in range(0, len(tokens), _BATCH_SIZE):
        batch = tokens[start : start + _BATCH_SIZE]
        # Data-only => silent. No title/body means nothing is displayed.
        messages = [
            {"to": token, "data": {"tokenCheck": True}, "_contentAvailable": True}
            for token in batch
        ]
        response = requests.post(
            EXPO_PUSH_URL, json=messages, headers=_HEADERS, timeout=20
        )
        tickets = (response.json() or {}).get("data") or []

        # Tokens Expo already knows are dead are flagged on the ticket itself.
        for token, ticket in zip(batch, tickets):
            if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                dead.append(token)

        ticket_id_by_token = {
            token: ticket["id"]
            for token, ticket in zip(batch, tickets)
            if ticket.get("status") == "ok" and ticket.get("id")
        }
        if not ticket_id_by_token:
            continue

        time.sleep(wait_seconds)
        receipt_response = requests.post(
            EXPO_RECEIPT_URL,
            json={"ids": list(ticket_id_by_token.values())},
            headers=_HEADERS,
            timeout=20,
        )
        receipts = (receipt_response.json() or {}).get("data") or {}

        for token, ticket_id in ticket_id_by_token.items():
            receipt = receipts.get(ticket_id) or {}
            # A missing receipt just means "not ready yet" -- leave the token
            # active and let a later run decide. Never guess.
            if (receipt.get("details") or {}).get("error") == "DeviceNotRegistered":
                dead.append(token)

    return len(tokens), dead


class Command(BaseCommand):
    help = "Deactivate Expo push tokens reported dead (DeviceNotRegistered)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deactivated without changing anything.",
        )
        parser.add_argument(
            "--wait",
            type=int,
            default=10,
            help="Seconds to wait for Expo receipts (default: 10).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        lock = _acquire_lock()
        if lock is None:
            message = (
                f"[{_now()}]\nSkipped: a push token cleanup is already running.\n\n"
            )
            _append_log(message)
            self.stdout.write(self.style.WARNING(message.strip()))
            return

        started = _now()
        try:
            checked, dead = _check_tokens(options["wait"])

            if dry_run:
                deactivated = 0
            else:
                deactivated = (
                    PushToken.objects.filter(token__in=dead, is_active=True).update(
                        is_active=False
                    )
                    if dead
                    else 0
                )
            remaining = PushToken.objects.filter(is_active=True).count()

            suffix = " (dry-run: nothing changed)" if dry_run else ""
            report = (
                f"[{started}]\n"
                f"Started Push Token Cleanup...\n\n"
                f"Checked: {checked} Active Tokens\n"
                f"Deactivated: {len(dead) if dry_run else deactivated} Dead Tokens\n"
                f"Remaining Active Tokens: {remaining}\n\n"
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
            # Exit non-zero so monitoring / Task Scheduler shows the failure.
            # The schedule itself is unaffected: tonight's failure never stops
            # tomorrow's run.
            raise SystemExit(1)
        finally:
            Path(lock).unlink(missing_ok=True)
