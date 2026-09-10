# OMS — Docker deployment guide

How to run the OMS stack on a Linux server with Docker.

This describes a real, tested configuration: everything in "Verify the
deployment" was run against the images these files build.

---

## 1. What you are deploying

Three containers, from two images:

| Service     | Image             | Role                                              | Published |
|-------------|-------------------|---------------------------------------------------|-----------|
| `web`       | `oms-web`         | nginx: serves the React bundle, proxies to the API | `:80`     |
| `backend`   | `oms-backend`     | Django under gunicorn (3 workers x 4 threads)      | loopback only |
| `scheduler` | `oms-backend`     | APScheduler worker, single process                 | nothing   |

`backend` and `scheduler` are the **same image** with different commands, so
the two cannot drift apart between deploys.

The database is **not** in this stack. It is an existing PostgreSQL server
holding live data, reached over the network via `DB_HOST` in `.env`.

### Request path

```
browser ──► nginx :80 ─┬─ /                → React bundle (SPA fallback)
                       ├─ /api/*           → gunicorn :8000
                       ├─ /static/*        → gunicorn (whitenoise, Django admin)
                       └─ /media/*         → gunicorn (user uploads)
```

Everything is one origin. That is the design decision the rest of this guide
leans on: the bundle calls a **relative** `/api`, so there is no CORS
preflight, no mixed content behind TLS, and the same image runs on
`localhost`, staging and `oms.jivo.in` with no rebuild.

---

## 2. Prerequisites

- Docker Engine 24+ and the Compose v2 plugin
  (`docker compose version` — not the old `docker-compose` binary)
- Outbound network access from the server to:
  - PostgreSQL (`DB_HOST:5432`)
  - SAP HANA Service Layer (`HANA_SERVICE_LAYER_URL`, usually `:50000`)
  - SAP SQL Server (`SAP_DB_HOST:1433`)
  - the NIC e-Invoice / e-Way Bill endpoints
  - any SMB share in `EINV_QR_*` / `PAYMENTS_IMAGES` (TCP 445)
- ~4 GB free disk. The backend image is ~1.2 GB — pandas, numpy, PyMuPDF and
  the HANA client are most of it.

Install Docker on Ubuntu/Debian:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"    # log out and back in for this to apply
```

---

## 3. Lay out the code

The backend and frontend are **separate git repositories**. This file and
`docker-compose.yml` live in the **backend** repo, and compose builds the
frontend from a sibling checkout:

```
oms/
├── backend/          # git repo — you run every command from HERE
│   ├── docker-compose.yml
│   ├── DEPLOYMENT.md
│   ├── Dockerfile
│   └── docker-entrypoint.sh
└── frontend/         # git repo — Dockerfile, nginx.conf
```

```bash
mkdir -p /srv/oms && cd /srv/oms
git clone <backend-repo-url> backend
git clone <frontend-repo-url> frontend
cd backend            # every command in this guide runs from here
```

**The sibling checkout is required, not a convention.** Compose builds the
web image from `../frontend`, so `docker compose build` fails immediately if
the frontend is missing or sits somewhere else. The two directory names must
be `backend` and `frontend`.

---

## 4. Configure

### 4.1 `.env` — the only file you must write

Copy it from a working environment — there is no committed template in the
backend repo today, so the authoritative list is the one below. It is
gitignored and must never be committed.

**The five that matter most for a production deploy**, because the defaults
are wrong for a server:

```env
# 1. Turn off debug. This is what switches on the security headers.
DEBUG=false

# 2. REQUIRED when DEBUG=false — Django refuses to start without it.
#    Generate one:
#      docker run --rm oms-backend:latest python -c \
#        "from django.core.management.utils import get_random_secret_key as k; print(k())"
SECRET_KEY=<paste the generated key>

# 3. Every hostname the server answers to. Not doing this gives a
#    "DisallowedHost" 400 on every request.
ALLOWED_HOSTS=oms.jivo.in,138.252.101.118,127.0.0.1,localhost

# 4. Needed for the Django admin login form to submit over HTTPS.
CSRF_TRUSTED_ORIGINS=https://oms.jivo.in

# 5. Read the TLS warning in 4.2 before setting this.
SECURE_SSL_REDIRECT=true
```

Plus the connection details, which have **no defaults** — Django will not
import without them:

```env
DB_NAME=  DB_USER=  DB_PASSWORD=  DB_HOST=  DB_PORT=5432
HANA_DB_HOST=  HANA_DB_PORT=  HANA_DB_OIL_NAME=  HANA_DB_USER=  HANA_DB_PASSWORD=
HANA_SERVICE_LAYER_URL=  HANA_USERNAME=  HANA_PASSWORD=
CRYSTAL_URL=
JSAP_API_BASE=
SAP_DB_HOST=  SAP_DB_NAME=  SAP_DB_USER=  SAP_DB_PASSWORD=
SAP_APPROVER_USER=  SAP_APPROVER_PASSWORD=
```

Lock it down — it holds every credential the system has:

```bash
chmod 600 .env
```

### 4.2 TLS: the one setting that will bite you

With `DEBUG=false`, **`SECURE_SSL_REDIRECT` defaults to `true`**. Django then
301-redirects any request it believes is plain HTTP.

- **If you terminate TLS in front of this stack** (recommended — see §8),
  that proxy must send `X-Forwarded-Proto: https`. Django trusts that header
  and everything works.
- **If you are running HTTP-only for now**, you must set
  `SECURE_SSL_REDIRECT=false` in `.env`, or every request redirects
  to an `https://` URL that nothing is listening on.

### 4.3 Directories to create before the first start

```bash
mkdir -p logs secrets
```

- `logs/` — bind-mounted so logs survive the container and can be read
  during an incident without `docker exec`.
- `secrets/` — NIC e-invoice / e-way bill private keys, mounted read-only.
  Create it even if empty; a missing bind-mount source makes Docker create a
  root-owned directory.

You do **not** need to `chown` these. The entrypoint fixes ownership at
startup and then drops to an unprivileged user.

### 4.4 Optional build-time settings

Only if the defaults do not fit.

These are read by **Compose itself**, not by Django — but Compose reads the
same `.env` file that Django's settings do, because both sit in this
directory. Adding them to `.env` is therefore correct, and Django ignores
keys it does not know:

```env
WEB_PORT=80                            # host port for nginx
VITE_PUBLIC_APP_URL=https://oms.jivo.in   # see below
```

`VITE_PUBLIC_APP_URL` is worth setting. It is the base for the HAIS device QR
code, which gets **printed onto physical stickers** — unset, the app uses
whatever origin the browser happened to use, so a sticker printed from
someone's laptop carries a dead `localhost` URL for the rest of its life.

---

## 5. Build

```bash
cd /srv/oms/backend
docker compose build
```

Roughly 3-5 minutes cold. Both images build offline afterwards thanks to
layer caching; only a change to `requirements.txt` or `package-lock.json`
re-runs the expensive dependency install.

---

## 6. Migrate

Run this **explicitly**, as its own step, before starting the stack. It is
deliberately not automatic: this points at a live database, and a migration
that runs as a side effect of `up` is a migration nobody reviewed.

```bash
# Preview first — read-only, touches nothing
docker compose run --rm --no-deps backend python manage.py showmigrations --plan | grep '^\[ \]'

# Apply
docker compose run --rm --no-deps backend python manage.py migrate
```

`--no-deps` stops Compose starting the whole stack just to run one command.

First deploy only, if there is no admin account yet:

```bash
docker compose run --rm --no-deps backend python manage.py createsuperuser
```

---

## 7. Start

```bash
docker compose up -d
docker compose ps
```

Expected — note that `scheduler` correctly shows no health state, because it
serves no HTTP and its healthcheck is disabled on purpose:

```
NAME            STATUS
oms-backend     Up 17 seconds (healthy)
oms-scheduler   Up 12 seconds
oms-web         Up 17 seconds (healthy)
```

### Verify the deployment

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/            # 200  SPA
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/orders      # 200  deep link
curl -s http://127.0.0.1/healthz                                      # ok   nginx
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/api/auth/login/  # 405 (POST-only)
```

The one that actually proves the database is reachable — it runs a real
authentication query and must come back `401`, not `500`:

```bash
curl -s -X POST http://127.0.0.1/api/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"x","password":"y"}'
# {"success":false,"message":"Login failed", ...}
```

And that the scheduler took its lock:

```bash
docker compose logs scheduler | tail -2
# SAP sync scheduler running (reconcile every 60s).
```

---

## 8. Put TLS in front

The stack speaks plain HTTP on port 80 by design — certificate renewal does
not belong inside an application image. Terminate TLS on the host with Caddy
(simplest) or nginx + certbot.

First, stop publishing port 80 to the world. In `.env`:

```env
WEB_PORT=127.0.0.1:8080
```

Then `docker compose up -d web`.

**Caddy** (`/etc/caddy/Caddyfile`) — obtains and renews certificates
automatically:

```
oms.jivo.in {
    reverse_proxy 127.0.0.1:8080
}
```

Caddy sets `X-Forwarded-Proto` on its own, which is exactly what
`SECURE_SSL_REDIRECT` needs.

**nginx**, if you prefer it — the forwarded headers are not optional:

```nginx
server {
    listen 443 ssl http2;
    server_name oms.jivo.in;

    ssl_certificate     /etc/letsencrypt/live/oms.jivo.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/oms.jivo.in/privkey.pem;

    client_max_body_size 25m;   # must be >= the container's own limit

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # ← Django reads this
        proxy_read_timeout 120s;                      # ← match gunicorn
    }
}

server {
    listen 80;
    server_name oms.jivo.in;
    return 301 https://$host$request_uri;
}
```

Once HTTPS is confirmed on every hostname, you can enable HSTS by setting
`SECURE_HSTS_SECONDS=3600` in `.env`, then raising it to `31536000`.
Raise it slowly and never lower it in a hurry — browsers honour the longest
value they have seen.

---

## 9. Scheduled jobs

The long-running SAP sync scheduler is already covered by the `scheduler`
service. The **periodic sweeps** are separate one-shot commands — on Windows
these were the `run_*.bat` files under Task Scheduler. On Linux, use host
cron.

`crontab -e`:

```cron
# Document Tracker — stuck-invoice alerts, every 30 min
*/30 * * * * cd /srv/oms/backend && docker compose run --rm --no-deps backend python manage.py scan_stuck_alerts >> /srv/oms/backend/logs/cron.log 2>&1

# Stuck-invoice digest emails, hourly (scan first so rows are current)
0 * * * * cd /srv/oms/backend && docker compose run --rm --no-deps backend sh -c 'python manage.py scan_stuck_alerts && python manage.py email_stuck_alerts' >> /srv/oms/backend/logs/cron.log 2>&1

# JSAP budget-approval sync, every 10 min
*/10 * * * * cd /srv/oms/backend && docker compose run --rm --no-deps backend python manage.py sync_jsap >> /srv/oms/backend/logs/cron.log 2>&1

# Auto-IRN sweep, every 15 min
*/15 * * * * cd /srv/oms/backend && docker compose run --rm --no-deps backend sh -c 'python manage.py auto_generate_irns --company-db JIVO_OIL_HANADB --limit 30 && python manage.py auto_generate_irns --company-db JIVO_BEVERAGES_HANADB --limit 30' >> /srv/oms/backend/logs/cron.log 2>&1
```

Every one of these is idempotent, so an overlapping or repeated run is safe.

Adjust the `--company-db` values to match your `HANA_DB_*_NAME` settings.

---

## 10. Redeploy a new version

```bash
cd /srv/oms/backend
git pull && git -C ../frontend pull

docker compose build

# Check for new migrations, then apply them
docker compose run --rm --no-deps backend python manage.py showmigrations --plan | grep '^\[ \]'
docker compose run --rm --no-deps backend python manage.py migrate

docker compose up -d
```

`up -d` recreates only the containers whose image or config changed.

Expect a few seconds of downtime — Compose stops the old backend before
starting the new one. For a zero-downtime rollout you would need two backend
replicas behind the proxy, which this compose file does not set up.

**Rollback**: images are tagged `:latest`, so there is nothing to roll back
to. If you need that, tag each release before building:

```bash
docker tag oms-backend:latest oms-backend:$(git rev-parse --short HEAD)
docker tag oms-web:latest     oms-web:$(git -C ../frontend rev-parse --short HEAD)
```

---

## 11. Operations

```bash
docker compose logs -f backend            # follow
docker compose logs --tail=100 scheduler
tail -f logs/oms.log                      # on the host, no docker needed
tail -f logs/oms_errors.log               # warnings and above only

docker compose restart backend
docker compose exec backend python manage.py shell
docker compose stats --no-stream
```

Container logs are capped at 3 x 10 MB per service. Django's own file logs
rotate at 10 MB x 6 (`oms.log`) and 5 MB x 6 (`oms_errors.log`), so
`logs/` cannot exceed roughly 90 MB.

### Backups

Two things hold state:

1. **PostgreSQL** — external, and by far the important one. Back it up with
   your existing database procedure; nothing in this stack does it for you.
2. **The `oms-media` volume** — user uploads.

```bash
docker run --rm -v oms_oms-media:/data -v "$PWD:/backup" alpine \
    tar czf /backup/oms-media-$(date +%F).tar.gz -C /data .
```

---

## 12. Troubleshooting

**`SECRET_KEY is not set`** — `DEBUG=false` with no `SECRET_KEY`. See §4.1.
Fatal on purpose: falling back to a default key is how a committed secret
survives being "removed".

**`DisallowedHost` / 400 on every request** — the hostname is missing from
`ALLOWED_HOSTS`. See §4.1.

**Everything 301-redirects, or the healthcheck never goes green** —
`SECURE_SSL_REDIRECT` is on but nothing is sending `X-Forwarded-Proto`.
See §4.2.

**`UndefinedValueError: X not found`** — a required variable is missing from
`.env`. The error names it. Full list in §4.1.

**`PermissionError: '/app/logs/oms.log'`** — the entrypoint could not fix
mount ownership, which normally means it did not run as root. Check you have
not added a `user:` override to the `backend` service.

**Frontend loads but every API call fails** — check `docker compose ps` for
the backend, then `docker compose logs backend`. nginx stays up when the API
is down, by design: you get a real error instead of a connection refused.

**`port is already allocated`** — something else holds port 80. Find it with
`sudo ss -ltnp | grep :80` and either stop it or set `WEB_PORT`.

**Build fails at `collectstatic`** — a new required setting was added to
`settings.py`. Add it to the dummy-value list in `Dockerfile`; the error
names the variable.

**`unable to prepare context: path "../frontend" not found`** — the frontend
repo is not checked out beside this one. See §3.

---

## 13. Security notes

Already handled by these files:

- Both application processes run as **non-root** (uid 10001). nginx workers
  drop to `nginx`.
- Django is **not** published to the network — `127.0.0.1:8000` is loopback
  only, for debugging. Reach it from a laptop with
  `ssh -L 8000:127.0.0.1:8000 user@server`.
- No secrets in either image. `.env` and `secrets/` are mounted at run time,
  so rotating a credential is a file edit plus a restart, never a rebuild.
- Build-time credentials are dummies, and inline to a single `RUN` so they
  never enter the container environment.
- nginx caps request bodies at 25 MB.

Worth doing on the host:

- Firewall everything except 80/443 and SSH.
- `chmod 600 .env`, and the same for anything in `secrets/`.
- Rotate `SECRET_KEY` if the old hardcoded one was ever live — it is in git
  history and signs every JWT. Rotating invalidates all issued tokens, so it
  needs a maintenance window.
