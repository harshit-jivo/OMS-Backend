# Automatic Nightly Push Token Cleanup

Runs `python manage.py prune_push_tokens` every night at **00:00** to deactivate
Expo push tokens that the push service reports as dead.

---

## 1. What it does and why

An Expo **ticket** only means *"Expo accepted the message"*. The real delivery
outcome — notably `DeviceNotRegistered` (the app was uninstalled, its data
cleared, or the token rotated) — is reported only in the delivery **receipt**.

`send_push_notification()` already checks receipts during normal traffic, so
tokens are pruned as notifications go out. This nightly job is the **safety
net**: it sweeps tokens belonging to users who receive no notifications (whose
dead tokens would otherwise linger forever) and cleans up anything left behind.

Without it, dead tokens accumulate per user (we once found a single user with 5
tokens, 4 of them dead) and every push silently goes nowhere.

**It never disturbs users.** The check sends a *silent, data-only* push — no
`title`, no `body`, `_contentAvailable: true`. Nothing is displayed on any
device; the message exists purely to obtain a receipt.

---

## 2. Where the log file is stored

```
OMS Backend/logs/push_token_cleanup.log
```

Every run appends one record — success or failure. Successful run:

```text
[2026-07-17 00:00:01]
Started Push Token Cleanup...

Checked: 153 Active Tokens
Deactivated: 4 Dead Tokens
Remaining Active Tokens: 149

Completed Successfully
```

Failed run (the full traceback is written, and **tomorrow's run is unaffected**):

```text
[2026-07-18 00:00:02]

ERROR:
Connection Timeout

Cleanup Failed
```

Skipped run (a previous cleanup was still running):

```text
[2026-07-19 00:00:01]
Skipped: a push token cleanup is already running.
```

### Changing the log location

Set in `.env` (absolute path recommended):

```env
PUSH_TOKEN_CLEANUP_LOG=/var/log/oms/push_token_cleanup.log
PUSH_TOKEN_CLEANUP_LOCK=/var/run/oms/push_token_cleanup.lock
```

Defaults live in `OMS/settings.py` under *"Push token cleanup (scheduled)"*.

> The log is append-only and never rotated by the app. On Linux, add a
> `logrotate` rule if you want it capped.

---

## 3. Setup — Linux (cron)

Edit the crontab of the user that owns the project:

```bash
crontab -e
```

Add (adjust the two absolute paths):

```cron
# OMS: nightly push token cleanup at 00:00
0 0 * * * cd /srv/oms/backend && /srv/oms/backend/.venv/bin/python manage.py prune_push_tokens >> /srv/oms/backend/logs/cron.out 2>&1
```

Verify it registered:

```bash
crontab -l
```

Notes:

* `cd` isn't strictly required (the command resolves its own paths and runs from
  any working directory), but it keeps relative `.env` loading predictable.
* The `>> cron.out 2>&1` redirect is optional — the command writes its own log.
  It's useful to capture anything that fails *before* Django starts (e.g. a bad
  virtualenv path), which the app log can't record.
* Use the **venv's** python (`.venv/bin/python`), not the system python.

### systemd timer (alternative to cron)

`/etc/systemd/system/oms-push-cleanup.service`:

```ini
[Unit]
Description=OMS push token cleanup

[Service]
Type=oneshot
User=oms
WorkingDirectory=/srv/oms/backend
ExecStart=/srv/oms/backend/.venv/bin/python manage.py prune_push_tokens
```

`/etc/systemd/system/oms-push-cleanup.timer`:

```ini
[Unit]
Description=Run OMS push token cleanup nightly

[Timer]
OnCalendar=*-*-* 00:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oms-push-cleanup.timer
systemctl list-timers oms-push-cleanup.timer
```

`Persistent=true` runs a missed job once the machine is back up (cron does not).

---

## 4. Setup — Windows Server (Task Scheduler)

### Option A — one command (run in an **Administrator** terminal)

```bat
schtasks /Create ^
  /TN "OMS Push Token Cleanup" ^
  /TR "\"C:\OMS\OMS Backend\.venv\Scripts\python.exe\" \"C:\OMS\OMS Backend\manage.py\" prune_push_tokens" ^
  /SC DAILY /ST 00:00 /RU SYSTEM /RL HIGHEST /F
```

Adjust both paths. `/F` overwrites an existing task of the same name.

Verify / run once / inspect:

```bat
schtasks /Query  /TN "OMS Push Token Cleanup" /V /FO LIST
schtasks /Run    /TN "OMS Push Token Cleanup"
```

### Option B — GUI

1. **Task Scheduler → Create Task** (not *Basic Task*).
2. **General**: name `OMS Push Token Cleanup`; select **Run whether user is
   logged on or not**; tick **Run with highest privileges**.
3. **Triggers → New**: *Daily*, start `00:00:00`, **Enabled**.
4. **Actions → New → Start a program**:
   * **Program/script**: `C:\OMS\OMS Backend\.venv\Scripts\python.exe`
   * **Add arguments**: `manage.py prune_push_tokens`
   * **Start in**: `C:\OMS\OMS Backend`
5. **Settings**: tick **Run task as soon as possible after a scheduled start is
   missed** (the equivalent of systemd's `Persistent=true`), and set *If the task
   is already running* → **Do not start a new instance**.

> Quote any path containing spaces (e.g. `OMS Backend`). Use the **venv's**
> `python.exe`, never the system one.

---

## 5. How to change the schedule

**Linux (cron)** — edit the five time fields in `crontab -e`:

| When | Cron expression |
| --- | --- |
| Every day 00:00 (default) | `0 0 * * *` |
| Every day 03:30 | `30 3 * * *` |
| Every Sunday 00:00 | `0 0 * * 0` |
| Every 6 hours | `0 */6 * * *` |

**systemd** — change `OnCalendar=` then
`sudo systemctl daemon-reload && sudo systemctl restart oms-push-cleanup.timer`.

**Windows** — change the trigger time:

```bat
schtasks /Change /TN "OMS Push Token Cleanup" /ST 03:30
```

…or edit the **Triggers** tab in the GUI.

---

## 6. How to disable the cleanup

**Linux (cron)** — comment the line out with `#` in `crontab -e`, or delete it.

**systemd**:

```bash
sudo systemctl disable --now oms-push-cleanup.timer
```

**Windows** — disable (keeps the task) or delete it:

```bat
schtasks /Change /TN "OMS Push Token Cleanup" /DISABLE
schtasks /Delete /TN "OMS Push Token Cleanup" /F
```

Re-enable with `/ENABLE`.

> Disabling only stops the **nightly sweep**. Receipt-based pruning inside
> `send_push_notification()` keeps working — dead tokens are still cleaned up as
> notifications are sent.

---

## 7. How to run it manually

From the backend directory, using the venv's python:

```bash
# Linux / macOS
.venv/bin/python manage.py prune_push_tokens

# Windows
.venv\Scripts\python.exe manage.py prune_push_tokens
```

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--dry-run` | Report what *would* be deactivated. Changes nothing. **Start here.** |
| `--wait N` | Seconds to wait for Expo receipts (default `10`). Raise on a slow link. |

Example:

```bash
.venv/bin/python manage.py prune_push_tokens --dry-run
```

Manual runs write to the same log and take the same lock, so they can't collide
with a scheduled run.

---

## 8. Behaviour guarantees

| Requirement | How it's met |
| --- | --- |
| Runs unattended, survives reboots | Owned by cron / systemd / Task Scheduler — not the Django process |
| Every run logged | Appended to `PUSH_TOKEN_CLEANUP_LOG`, success **and** failure |
| Failures never stop future runs | Exceptions are caught, the traceback is logged, exit code `1`. The scheduler fires again on the next tick regardless |
| No duplicate executions | Atomic `O_EXCL` lock file (`PUSH_TOKEN_CLEANUP_LOCK`). A second run logs *"Skipped"* and exits. A lock older than 1 hour is treated as stale and broken, so a hard kill can't disable the job permanently |
| Lightweight, never interrupts delivery | Runs as a separate short-lived process; batches ≤100 tokens per Expo request; only a single bulk `UPDATE` on dead rows. Sends **silent data-only** pushes, so no notification is ever shown |
| Never guesses | A token is deactivated **only** on an explicit `DeviceNotRegistered`. A missing/not-ready receipt leaves the token active for a later run |

---

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Log file never appears | Task/cron isn't firing. Linux: `grep CRON /var/log/syslog`. Windows: `schtasks /Query /TN "OMS Push Token Cleanup" /V /FO LIST` → check **Last Result** (`0x0` = success). |
| `ModuleNotFoundError` / `ImproperlyConfigured` | Wrong interpreter — use the **venv's** python, not the system one. |
| Always *"Skipped: already running"* | A stale lock. It self-clears after 1 hour; to clear now, delete the file at `PUSH_TOKEN_CLEANUP_LOCK`. |
| `Deactivated: 0` every night | Healthy — it means no dead tokens. Confirm with `--dry-run`. |
| A user gets no notifications | Their token was pruned as dead. They simply **open the app once** — `registerDeviceToken()` runs on every launch and registers a fresh token. |
