# ===========================================================================
#  OMS backend — Django + Gunicorn
#
#  Two stages. The builder gets a compiler and produces a self-contained
#  virtualenv at /opt/venv; the runtime stage copies that venv and keeps no
#  build toolchain, which is both smaller and a smaller attack surface.
#
#  Build:  docker build -t oms-backend ./backend
#  Run:    see ../docker-compose.yml — the image is used by three services
#          (web, scheduler, and one-shot `manage.py` jobs) that differ only
#          in their command.
# ===========================================================================

# Matches the interpreter the project is developed against (.venv is 3.14.4).
# Pinned to the minor version deliberately: pandas 3.0 / numpy 2.4 / hdbcli
# ship version-specific wheels, and a silent bump to 3.15 would rebuild them
# from source or fail outright.
ARG PYTHON_VERSION=3.14


# --------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Compilers and headers for any dependency without a cp314 wheel. Present in
# THIS stage only — nothing here is copied into the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt alone in its own layer: the dependency install is the
# expensive step and application edits must not invalidate it.
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# Gunicorn is deliberately NOT in requirements.txt. That file is shared with
# the Windows deployment (run_*.bat / Task Scheduler), where gunicorn cannot
# run at all — it needs fcntl. Pinning it here keeps the WSGI server a
# property of this image rather than of every environment.
RUN pip install "gunicorn==23.0.0"


# --------------------------------------------------------------------------
# Stage 2 — runtime
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=OMS.settings

# Native binaries the Python packages SHELL OUT to — pip does not install
# these and their absence is a runtime failure, not an import error:
#   tesseract-ocr   pytesseract        -> legal/ label OCR
#   poppler-utils   pdf2image          -> PDF page rendering (`pdftoppm`)
#   libpq5          psycopg2           -> the wheel bundles libpq, but the
#                                         system one keeps `pg_isready`-style
#                                         tooling and OpenSSL in step
#   curl            HEALTHCHECK below
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        libpq5 \
        curl \
        tzdata \
        gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Non-root. Created before COPY so the ownership is set in one layer rather
# than duplicating the tree with a later `chown -R`.
RUN useradd --create-home --uid 10001 oms

COPY --chown=oms:oms . /app

# Directories Django writes to at run time. `logs/` in particular is created
# by settings.py AT IMPORT (LOG_DIR.mkdir) — if /app is not writable by the
# app user, every single management command dies before Django starts.
RUN mkdir -p /app/logs /app/media /app/staticfiles \
    && chown -R oms:oms /app/logs /app/media /app/staticfiles

# COPY preserves the host's file mode, and a Windows checkout has no exec bit
# to preserve — the image would then fail to start with "permission denied"
# on a script that looks perfectly fine in the editor. Set it here so the
# build does not depend on which OS produced the context.
# (Line endings are already handled: .gitattributes pins *.sh to LF, and a
# CRLF shebang fails just as opaquely.)
RUN chmod +x /app/docker-entrypoint.sh

# Drop to `oms` for collectstatic so staticfiles/ ends up owned by the user
# that will serve it, rather than by root.
USER oms

# Static files are baked into the image, not collected at boot.
#
# CompressedManifestStaticFilesStorage REQUIRES staticfiles.json to exist —
# without it whitenoise raises on the first templated {% static %} lookup, so
# this cannot be deferred to a runtime entrypoint.
#
# WHY THE WALL OF DUMMY VALUES
# ----------------------------
# settings.py calls config('X') with NO default for 22 variables — the
# Postgres DSN, the HANA pair, the SAP SQL Server, the Service Layer and
# JSAP — and python-decouple raises UndefinedValueError on the first one
# missing. That is at MODULE IMPORT, so it fires for every management
# command, collectstatic included.
#
# The values are never used: collectstatic reads the filesystem and opens no
# connection. Passing the REAL .env here instead would be worse — it would
# bake production credentials into an image layer, and make the image
# environment-specific when the whole point is that one image runs anywhere.
#
# They are inline on this RUN rather than ENV so they exist for exactly this
# command and cannot leak into the running container's environment.
#
# If a future settings change adds another required variable, this build
# fails with that variable's name in the error. Add it to the list.
RUN set -eu; \
    export DEBUG=true; \
    export DB_NAME=build DB_USER=build DB_PASSWORD=build \
           DB_HOST=localhost DB_PORT=5432; \
    export HANA_DB_HOST=localhost HANA_DB_PORT=30015 \
           HANA_DB_OIL_NAME=build HANA_DB_USER=build HANA_DB_PASSWORD=build; \
    export HANA_SERVICE_LAYER_URL=https://localhost:50000/b1s/v2 \
           HANA_USERNAME=build HANA_PASSWORD=build; \
    export CRYSTAL_URL=http://localhost:8008; \
    export JSAP_API_BASE=http://localhost; \
    export SAP_DB_HOST=localhost SAP_DB_NAME=build \
           SAP_DB_USER=build SAP_DB_PASSWORD=build; \
    export SAP_APPROVER_USER=build SAP_APPROVER_PASSWORD=build; \
    python manage.py collectstatic --noinput --clear

# Back to root for the ENTRYPOINT, which needs to chown the mounts before it
# hands the process to `oms` via gosu. See docker-entrypoint.sh — nothing in
# this image runs the application as root.
USER root
ENTRYPOINT ["/app/docker-entrypoint.sh"]

EXPOSE 8000

# Hits a real Django URL through the full WSGI stack. `/api/auth/login/`
# answers 405 to GET (it is POST-only), which proves routing and middleware
# are alive — so the status is matched explicitly rather than trusting a 2xx.
#
# X-Forwarded-Proto is REQUIRED here, not decoration. With DEBUG=false
# settings.py turns on SECURE_SSL_REDIRECT (defaulting to true) and trusts
# that header as the proof of TLS. A bare loopback request therefore gets a
# 301 to https and the container is marked unhealthy while it is serving
# real traffic perfectly well. Sending the header is exactly what nginx does
# on every proxied request, so this checks the same path users take.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -sS -o /dev/null -w '%{http_code}' \
        -H 'X-Forwarded-Proto: https' \
        http://127.0.0.1:8000/api/auth/login/ | grep -qE '^(200|405)$' || exit 1

# 3 workers × 4 threads. Threads rather than more processes because this app's
# slow paths are WAITING — SAP Service Layer, HANA, the NIC e-invoice API —
# not burning CPU, and a thread blocked on a socket costs a stack instead of
# a whole interpreter. `--timeout 120` covers the SAP pushes, which routinely
# outlast gunicorn's 30s default and would otherwise be killed mid-request.
# Override WEB_CONCURRENCY per host; the default suits a 2-core box.
ENV WEB_CONCURRENCY=3
CMD ["sh", "-c", "exec gunicorn OMS.wsgi:application \
     --bind 0.0.0.0:8000 \
     --workers ${WEB_CONCURRENCY} \
     --threads 4 \
     --timeout 120 \
     --graceful-timeout 30 \
     --access-logfile - \
     --error-logfile - \
     --capture-output"]
