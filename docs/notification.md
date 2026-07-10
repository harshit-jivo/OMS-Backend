# OMS Notifications — Architecture & Deployment Guide

This document explains the complete OMS notification system and how to deploy it
from scratch. It is written for a developer or DevOps engineer who has **never**
worked on this project before.

The system spans three apps that all talk to **one** Django backend:

| App | Location | Notification transport |
| --- | --- | --- |
| Django backend | `OMS Backend/` | Sends push to both clients |
| React web (Vite) | `OMS Frontend-web/` | Browser Web Push + Service Worker |
| React Native (Expo) | `OMS-app/` | Expo Push Notifications |

> **Scope note:** notification *business rules* (who gets notified, approval
> routing, message templates) are fixed and are **not** covered here — this
> guide is about configuration, deployment, and operations only.

---

## Notification Architecture

There is one backend and two independent delivery channels. The backend decides
recipients once (unchanged business logic) and fans a notification out to every
channel the recipient has.

```
                          ┌──────────────────────────┐
                          │     Django Backend        │
                          │  orders/notifications.py  │
                          │  deliver_notification()   │
                          └────────────┬──────────────┘
                                       │  (one recipient, one message)
                 ┌─────────────────────┴─────────────────────┐
                 │                                            │
     Mobile (Expo Push)                            Web (Web Push / VAPID)
                 │                                            │
                 ▼                                            ▼
       Expo Push Service                             Browser Push Service
       (exp.host)                                    (FCM / Mozilla / WNS)
                 │                                            │
                 ▼                                            ▼
     ┌───────────────────────┐                  ┌────────────────────────────┐
     │  React Native app      │                  │  Service Worker            │
     │  (foreground/back/     │                  │  (public/service-worker.js)│
     │   killed + deep link)  │                  │   • tab visible → popup    │
     └───────────────────────┘                  │   • tab hidden  → desktop  │
                                                 └────────────┬───────────────┘
                                                              ▼
                                                          Browser UI
                                              (bell • popup • sound • desktop)
```

### The two flows in words

**React Native → Django → Expo Push**

1. The app registers an Expo push token (`POST /api/orders/push-token/`).
2. When an order event fires, `deliver_notification()` saves the `Notification`
   row and calls `send_push_notification()` → HTTP POST to `exp.host`.
3. Expo delivers to the device (foreground, background, or killed).

**React Web → Service Worker → Web Push → Browser**

1. After login the browser subscribes with the VAPID public key and stores the
   subscription (`POST /api/orders/web-push/subscribe/`).
2. On an order event, `deliver_notification()` also calls
   `send_web_push_to_user()` → encrypted POST to the browser's push endpoint.
3. The Service Worker receives the `push` event and either relays it to an open
   tab (in-app popup + sound) or shows an OS desktop notification.

### How both clients share one backend

Both channels are driven from the **same** `deliver_notification()` call in
[`orders/notifications.py`](../OMS%20Backend/orders/notifications.py). It persists
one `Notification` record and then best-effort delivers to Expo **and** Web Push.
A failure in one channel never blocks the other or the API request. The two
channels use **separate** storage so they can never interfere:

- `PushToken` — Expo/mobile tokens.
- `WebPushSubscription` — browser subscriptions.

The notification list/history REST endpoints are shared by both clients.

---

## Environment Variables

All backend variables are read from `OMS Backend/.env` via `python-decouple`.

| Variable | What it is | Where used | Secret? | Example | Production recommendation |
| --- | --- | --- | --- | --- | --- |
| `VAPID_PUBLIC_KEY` | VAPID *application server key*; the web client passes it to `pushManager.subscribe`. | `settings.py` → `orders/webpush.py`; served to the browser via `GET /api/orders/web-push/public-key/`. | No (ships to browsers) | `BPiXZwTdWO7M9rC…` (87 chars) | Generate a real pair; keep public/private from the **same** pair. |
| `VAPID_PRIVATE_KEY` | Raw base64url P-256 private key that signs the VAPID JWT. | `orders/webpush.py` (`Vapid.from_raw`). | **YES** | `T7qmaLc_OjssOJv6…` (43 chars) | Store only in `.env` / secrets manager. Never commit. Rotate on leak. |
| `VAPID_ADMIN_EMAIL` | Contact address sent to push services in the VAPID `sub` claim. | `orders/webpush.py`. | No | `mailto:admin@example.com` (or `admin@example.com`) | Use a monitored mailbox; push services may email you about abuse. |
| `EXPO_ACCESS_TOKEN` | *(Optional)* Expo access token for the Expo Push API. **Not currently required** — the project posts to `exp.host` without it. | Would be used by `send_push_notification()` if Expo enforces auth. | **YES** | `xxxxxxxx-xxxx-…` | Only set if you enable Expo's "enhanced security" for push. |
| `DB_*`, `HANA_*`, `SAP_*` | Existing database/integration settings (not notification-specific). | `settings.py` | Mixed | — | Unchanged by this phase. |

> The mobile app additionally reads `EXPO_PUBLIC_API_BASE_URL` at build time
> (in `OMS-app/`) to point at the backend. It is **not** a notification secret.

### Generating VAPID keys

```bash
cd "OMS Backend"
python manage.py generate_vapid_keys
```

This prints (never writes) a fresh pair in `.env` format plus instructions. Copy
the three lines into `OMS Backend/.env` and restart Django. It is safe to run
repeatedly — each run just prints new keys.

> The repository ships **dev-only throwaway** VAPID defaults in `settings.py` so
> the app works locally out of the box. **Always** override them in production.

---

## Local Development

| Area | Expected behavior locally |
| --- | --- |
| **HTTP limitations** | Web Push + Service Workers require a *secure context*. Plain `http://<ip>` (e.g. `http://103.89.45.75`) does **not** get Service Workers. |
| **localhost** | `http://localhost` and `http://127.0.0.1` are treated as secure by browsers, so **full Web Push works on localhost over HTTP** — ideal for dev. |
| **Browser notifications** | Permission prompt appears ~8s after an eligible user is active (never on first paint). Grant it to test popups/desktop notifications. |
| **Service Worker** | Registered from `/service-worker.js`. In dev, Vite serves it with `Cache-Control: no-cache` (see `vite.config.ts`). Use DevTools → Application → Service Workers to inspect/unregister. |
| **React Native testing** | Run `npx expo start` in `OMS-app/`. Push tokens only work on a **physical device** (or a dev build) — the Android emulator/iOS simulator are skipped by design. |
| **Expo testing** | With a physical device + Expo Go / dev build, log in → a token registers → trigger an order event → the device receives the push. |

Typical dev setup:

```bash
# Terminal 1 — backend
cd "OMS Backend" && python manage.py runserver 0.0.0.0:8000

# Terminal 2 — web (proxies /api → 127.0.0.1:8000)
cd "OMS Frontend-web" && npm run dev      # open http://localhost:5173

# Terminal 3 — mobile
cd "OMS-app" && npx expo start
```

---

## Production Deployment

1. **Generate VAPID keys** — `python manage.py generate_vapid_keys`.
2. **Configure `.env`** — paste `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`,
   `VAPID_ADMIN_EMAIL` into `OMS Backend/.env`. Ensure `pip install -r
   requirements.txt` has installed `pywebpush`, `py-vapid`, `http-ece`.
3. **Run migrations** — `python manage.py migrate` (creates
   `web_push_subscriptions` and notification indexes if not yet applied).
4. **Restart Django** so the new keys load.
5. **Build React** — `cd "OMS Frontend-web" && npm ci && npm run build`; deploy
   `dist/` behind your reverse proxy over **HTTPS**.
6. **Configure the Service Worker cache header** (see next section).
7. **Verify Service Worker** — load the site, DevTools → Application → Service
   Workers shows `/service-worker.js` **activated**; its response has
   `Cache-Control: no-cache`.
8. **Verify Browser Push** — log in as a notifier role, grant permission, trigger
   an order event from another account → bell increments, popup + sound appear;
   minimize the tab and trigger again → OS desktop notification appears; click it
   → the correct Sales Order opens.
9. **Verify Expo Push** — on a physical device, log in, trigger an event → push
   arrives in foreground/background/killed; tapping opens the order.
10. **Verify notification routing** — confirm the *right* roles receive each
    event (sales→approver, approver→billing, billing→auditor, auditor→creator,
    rejection/completion→creator). This must match pre-existing behavior exactly.

### Expected verification results

- New browser subscription rows appear in `web_push_subscriptions` after a web
  login with permission granted.
- New Expo tokens appear in `push_tokens` after a mobile login.
- Backend logs show `Notification created …` and either `Expo push …` or web
  push activity per event.

---

## HTTPS Requirements

Web Push and Service Workers **require a secure context**. Background browser
notifications work on:

- **HTTPS** (any hostname with a valid certificate), and
- **`http://localhost`** / **`http://127.0.0.1`** (dev only).

They do **not** work on:

- plain **HTTP** on a non-localhost host, or
- a **raw IP address over HTTP** (e.g. `http://103.89.45.75`).

This is enforced by the **browser** (Chrome/Edge/Firefox) and **cannot be
bypassed** by any application or server setting — no flag, header, or backend
change enables Service Workers on insecure origins.

### Fallback behavior already implemented

The web app detects the secure context with `isWebPushSupported()` in
[`webPushClient.ts`](../OMS%20Frontend-web/src/services/webPushClient.ts). When
Web Push is **not** available (insecure origin or unsupported browser) the app
**automatically falls back to lightweight polling** (30s) so the bell and list
still update — it just can't show background desktop notifications. When served
over HTTPS/localhost, polling is disabled and delivery is event-driven. No
configuration is needed; the switch is automatic.

---

## React Native (Mobile)

**React Native does NOT use VAPID.** It continues to use **Expo Push
Notifications** exactly as before. VAPID keys are irrelevant to the mobile app.

Complete mobile flow (see `OMS-app/src/services/notification.service.ts` and
`app/(main)/_layout.tsx`):

| Stage | Behavior |
| --- | --- |
| **Token registration** | On login the app ensures the Android channel, requests permission, gets an Expo push token, and `POST`s it to `/api/orders/push-token/`. Duplicate registrations are skipped; token rotation re-registers automatically. |
| **Foreground** | `setNotificationHandler` shows a banner + sound + badge; the unread bell refreshes. |
| **Background** | Delivered by the OS; tapping routes via the notification response handler. |
| **Killed app** | `useLastNotificationResponse()` surfaces the launch notification on cold start and deep-links once. |
| **Deep linking** | Navigates to `/orders/orderdetails?orderId=…` using the `order_id` from the payload (never by parsing text). |
| **Notification tap** | A single de-duped handler covers open/background/killed → exactly one navigation. |
| **Logout** | The device token is deactivated (`DELETE /api/orders/push-token/`) before storage is cleared, so a signed-out device stops receiving pushes. |

The backend payload includes structured fields (`order_id`, `event_type`,
`notification_type`, `title`, `timestamp`) so the app navigates from data, not
message text. These are additive — older app builds ignore unknown fields.

---

## Web

Complete web flow (see `OMS Frontend-web/src/`):

| Feature | Behavior |
| --- | --- |
| **Permission request** | Never on first load. After an eligible user is active ~8s, a rationale card appears; only then is `Notification.requestPermission()` called. The choice is stored so the user isn't nagged. |
| **Subscription** | On grant, `pushManager.subscribe()` runs with the VAPID public key; the subscription is stored per user (`web-push/subscribe/`). Multiple browsers/devices per user are supported. |
| **VAPID** | Public key fetched from `GET /api/orders/web-push/public-key/`; private key signs pushes server-side. |
| **Service Worker** | `public/service-worker.js` handles `push`, `notificationclick`, and `pushsubscriptionchange`. |
| **Desktop notification** | Shown by the SW only when **no tab is visible**, with icon, `tag` (dedupe), timestamp, and `order_id`. |
| **Popup** | When a tab is visible, the SW messages the page; a non-intrusive toast (title, message, order #, "View order") auto-dismisses after 6s. |
| **Sound** | A short WebAudio chime; gated on first user interaction (autoplay policy) and debounced so bursts don't overlap. |
| **Notification click** | Focuses an existing app tab (or opens a new one) and navigates to the role-correct Sales Order route. |
| **Read state** | Opening a notification marks it read on the server; the badge, list, and every open tab update through one bus path (no double counting). |
| **History** | `GET /api/orders/notifications/history/` is paginated with Unread/All filters and Today/Yesterday/Older grouping — not limited to the latest 50 unread. |

---

## Service Worker Cache Headers

Only **`/service-worker.js`** should be served with `Cache-Control: no-cache`
so browsers always revalidate and pick up new versions immediately. Do **not**
disable caching for hashed JS bundles, CSS, images, or fonts — they are
content-hashed and should keep long-lived caching.

### Detected setup for this project

- **Dev / `vite preview`:** handled in-repo by a small plugin in
  [`vite.config.ts`](../OMS%20Frontend-web/vite.config.ts) that adds the header
  to `/service-worker.js` only.
- **Production:** the built `dist/` is served by an **external reverse proxy /
  static host** (the app uses a relative `/api` base and there is no server
  config committed to this repo). Apply the header **there**, as below.

### Nginx (recommended)

```nginx
# Long-lived caching for hashed build assets (default).
location / {
    root   /var/www/oms-web/dist;
    try_files $uri /index.html;
}

# The ONLY file that must never be cached hard.
location = /service-worker.js {
    root      /var/www/oms-web/dist;
    add_header Cache-Control "no-cache";
    # Optional but recommended so the header is always applied:
    # expires off;
}

# Proxy API to Django (WSGI/gunicorn).
location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Apache (`.htaccess` in the web root)

```apache
<Files "service-worker.js">
    Header set Cache-Control "no-cache"
</Files>
```

### Static hosts (Netlify / Cloudflare Pages)

Add a `public/_headers` file so it is copied into `dist/`:

```
/service-worker.js
  Cache-Control: no-cache
```

> Whatever serves `dist/` in your environment, apply the header to
> `/service-worker.js` **only**.

---

## Troubleshooting

| Symptom | Likely cause & fix |
| --- | --- |
| **Permission denied** | The user blocked notifications. It cannot be re-prompted programmatically — they must re-enable in browser Site Settings. The app respects the stored decision and won't nag. |
| **Service Worker not updating** | Missing `Cache-Control: no-cache` on `/service-worker.js`. Add it (see above), then hard-reload or "Update on reload" in DevTools → Application. |
| **Notification received but no sound** | Autoplay policy: the chime only plays after the user has interacted with the page. Click anywhere once, or rely on the OS sound for desktop notifications. |
| **No desktop notification** | It only shows when **no tab is visible**. If a tab is focused you'll see the in-app popup instead (by design, to avoid duplicates). Also check OS "Do Not Disturb". |
| **Push subscription expired** | Browsers rotate endpoints. The SW `pushsubscriptionchange` handler re-subscribes and an open tab re-persists it; the backend also deactivates dead subscriptions on 404/410. Users may need to revisit the app. |
| **Invalid VAPID key** | Public and private keys must come from the **same** pair. Regenerate with `generate_vapid_keys`, update `.env`, restart Django, and have users re-subscribe. |
| **Expo token invalid** | The device token was removed/rotated. Re-open the app while logged in to re-register; the backend prunes bad tokens. |
| **HTTP instead of HTTPS** | Web Push silently unavailable on insecure origins; the app falls back to polling. Serve over HTTPS to enable background push. |
| **Notification opens wrong page** | Navigation uses `order_id` from the payload + the user's role route. Verify the payload carries `order_id` and the user's role maps to the expected screen. |
| **Multiple tabs** | Every tab receives the push (via the SW) and read-state syncs via `BroadcastChannel`; the OS notification is de-duped by `tag`. If tabs disagree, ensure they share the same origin and the SW is controlling them. |
| **Browser cache** | If an old SW persists, confirm the `no-cache` header, then Unregister in DevTools and reload. Hashed assets are fine to keep cached. |

---

## Maintenance

**When to rotate VAPID keys**

- On suspected private-key leak, or per your security policy.
- Rotating **invalidates all existing browser subscriptions**. After rotation,
  every web user must re-subscribe (they will on next visit once permission is
  still granted; the app re-subscribes automatically). Mobile/Expo is unaffected.

**How to update the Service Worker safely**

1. Edit `public/service-worker.js`.
2. Rebuild the web app and deploy `dist/`.
3. Because `/service-worker.js` is served `no-cache`, browsers fetch the new
   version on next load; it installs and `skipWaiting()` + `clients.claim()`
   activate it promptly.
4. Verify in DevTools → Application → Service Workers that the new version is
   active (check the source/timestamp).

**How to deploy new versions**

1. Backend: `pip install -r requirements.txt` → `python manage.py migrate` →
   restart the WSGI server.
2. Web: `npm ci && npm run build` → publish `dist/` → confirm the SW header.
3. Mobile: build via EAS as usual (Expo). No VAPID involvement.

**How to verify production**

Run the checklist in *Production Deployment → Verify* (SW active, browser push
end-to-end, Expo push end-to-end, correct routing). Confirm:

- `web_push_subscriptions` gains rows on web logins.
- `push_tokens` gains rows on mobile logins.
- Backend logs show notification creation and per-channel delivery.
- Recipient routing is unchanged from previous releases.
