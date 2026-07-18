# Self-Healing Web Push Notifications

Browser Web Push in OMS is designed to need **zero manual maintenance** — the
same self-healing lifecycle the Expo mobile path has. This document covers the
nightly cleanup command and its scheduling. For the deep mechanics of the
scheduler (lock file, missed-run recovery, troubleshooting) the
[push-token-cleanup](./push-token-cleanup.md) doc applies verbatim — this one is
the Web Push counterpart.

---

## How self-healing works (three layers)

1. **Immediate cleanup on send** *(highest priority)* — every time a
   notification is pushed, a dead endpoint (`404`/`410 Gone`, or `401`/`403` for
   a subscription bound to an old VAPID key) is deactivated **on the spot** and
   delivery continues to the remaining subscriptions. Dead rows never survive to
   the next notification. See `deliver_web_push()` /
   `send_web_push_to_user()` in `orders/webpush.py`.

2. **Automatic re-subscription** — on every app **open / refresh / login** the
   web client runs `pushManager.getSubscription()`, checks it was created with
   the server's *current* VAPID key, and if it's missing or stale it
   re-subscribes and `POST`s the new subscription. Django upserts it with
   `update_or_create(endpoint=...)`, so there's always exactly one row per
   browser and the user never touches browser storage. See
   `subscribeToPush()` in `OMS Frontend-web/src/services/webPushClient.ts`.

3. **Nightly safety-net sweep** — `prune_web_push_subscriptions` validates every
   active subscription and deactivates any the push service reports as dead.
   This catches subscriptions belonging to users who receive no notifications
   (whose dead rows would otherwise linger active forever).

Layers 1–2 keep the system healthy during normal traffic; layer 3 is the
belt-and-braces sweep.

---

## The cleanup command

```bash
python manage.py prune_web_push_subscriptions             # validate + prune
python manage.py prune_web_push_subscriptions --dry-run   # report only
```

**How it validates:** Web Push has no "receipt" API (unlike Expo), so the only
way to know an endpoint is alive is to send it a push and read the HTTP status.
The command sends a tiny **data-only keepalive** (`{"type":"__keepalive__"}`)
that the service worker recognises and handles **silently** — nothing is shown
to the user. `201/200` = valid; `404/410/401/403` = dead → deactivated.

Output (also written to the log):

```text
[2026-07-18 00:00:01]
Checking Web Push Subscriptions...

Checked: 148
Valid: 145
Removed: 3
Errors (kept, retried later): 0

Completed Successfully
```

> ### ⚠️ Deploy the service worker BEFORE enabling the nightly job
> The keepalive is only silent for browsers running the **updated**
> `service-worker.js` (the one that skips `type === "__keepalive__"`). A browser
> still on the old SW would show a blank "Order update" notification for the
> ping. So: **ship the frontend once, then enable the cron/task.** After that,
> every browser auto-updates its SW and pings are silent. Immediate cleanup
> (layer 1) needs no such ordering.

Guarantees (identical to `prune_push_tokens`):

* logs every run — success **and** failure — to `WEB_PUSH_CLEANUP_LOG`;
* an atomic `O_EXCL` lock (`WEB_PUSH_CLEANUP_LOCK`) prevents overlapping runs
  (a second run logs *"Skipped"*); a lock older than 1 hour is treated as stale;
* never crashes the schedule — a failure logs the full traceback, exits `1`, and
  tomorrow's run is unaffected;
* every subscription is processed independently — one failure never stops the rest.

---

## Log & lock location

```
OMS Backend/logs/web_push_cleanup.log
OMS Backend/logs/web_push_cleanup.lock
```

Override in `.env` (absolute paths recommended in production):

```env
WEB_PUSH_CLEANUP_LOG=/var/log/oms/web_push_cleanup.log
WEB_PUSH_CLEANUP_LOCK=/var/run/oms/web_push_cleanup.lock
```

Defaults live in `OMS/settings.py` under *"Push token cleanup (scheduled)"*.
The `logs/` directory is gitignored.

---

## Scheduling — Linux (cron)

`crontab -e`, then (adjust the two absolute paths):

```cron
# OMS: nightly Web Push subscription cleanup at 00:00
0 0 * * * cd /srv/oms/backend && /srv/oms/backend/.venv/bin/python manage.py prune_web_push_subscriptions >> /srv/oms/backend/logs/cron.out 2>&1
```

Run the mobile token cleanup on the same schedule — they're independent:

```cron
0 0 * * * cd /srv/oms/backend && /srv/oms/backend/.venv/bin/python manage.py prune_push_tokens >> /srv/oms/backend/logs/cron.out 2>&1
```

systemd-timer alternative: copy the unit files from
[push-token-cleanup.md](./push-token-cleanup.md#systemd-timer-alternative-to-cron),
changing `ExecStart` to `... manage.py prune_web_push_subscriptions`.

---

## Scheduling — Windows Server (Task Scheduler)

One command, in an **Administrator** terminal (adjust paths):

```bat
schtasks /Create ^
  /TN "OMS Web Push Cleanup" ^
  /TR "\"C:\OMS\OMS Backend\.venv\Scripts\python.exe\" \"C:\OMS\OMS Backend\manage.py\" prune_web_push_subscriptions" ^
  /SC DAILY /ST 00:00 /RU SYSTEM /RL HIGHEST /F
```

Verify / run once:

```bat
schtasks /Query /TN "OMS Web Push Cleanup" /V /FO LIST
schtasks /Run   /TN "OMS Web Push Cleanup"
```

GUI steps are identical to the token-cleanup doc — just use
`prune_web_push_subscriptions` as the argument, and tick **Do not start a new
instance** under *If the task is already running*.

---

## Change / disable / run manually

| Action | How |
| --- | --- |
| Change time | cron: edit the time fields; Windows: `schtasks /Change /TN "OMS Web Push Cleanup" /ST 03:30` |
| Disable | cron: comment the line; Windows: `schtasks /Change /TN "OMS Web Push Cleanup" /DISABLE` |
| Run manually | `.venv/bin/python manage.py prune_web_push_subscriptions` (add `--dry-run` to preview) |

> Disabling only stops the nightly sweep. **Immediate cleanup during sends and
> auto re-subscription keep working** — the system still self-heals, just a bit
> less proactively for silent (never-notified) users.

---

## Behaviour by scenario

| Scenario | What happens |
| --- | --- |
| User clears browser data | Old endpoint dies → deactivated on next send *or* nightly sweep. On next visit the client auto-creates a fresh subscription and upserts it. |
| User changes browser | New browser registers its own row on first visit; the old browser's row stays active until *it* goes dead. Two independent rows, one per browser. |
| User changes computer | New machine registers on first visit; the old machine's endpoint is deactivated the first time it's found dead (send or sweep). |
| Website deployed 100× | Nothing happens to subscriptions — VAPID keys are unchanged, so every existing subscription keeps working. No cleanup, no user action. |
| Endpoint expires | First notification to it returns 404/410 → removed immediately; the user gets a fresh subscription on their next login/visit. |

---

## Production requirements — how each is met

| Requirement | Met by |
| --- | --- |
| Keep the same VAPID key pair | Nothing rotates keys. A startup check (`orders/checks.py`) refuses to boot on a mismatched pair, and the dev-key fallback is blocked when `DEBUG=False`. |
| No browser cache clearing | Auto re-subscription replaces stale/missing subscriptions transparently. |
| No manual DB cleanup | Immediate + nightly deactivation handle it. |
| No manual re-subscription after deploys | Deploys don't touch subscriptions; the client re-subscribes only if one is actually missing/stale. |
| Web Push best practices | `update_or_create` on the unique endpoint, standard 404/410 pruning, silent keepalive validation, VAPID JWT per RFC 8292. |
| Fully self-healing | Three independent layers, none requiring an admin or a user. |
