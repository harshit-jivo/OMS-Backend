# Encoded invariants, mined from the source

This codebase states its rules in comments and docstrings rather than in
types or tests. Those statements are the constraints a refactor is most
likely to break, because nothing fails when they are violated -- the
behaviour just goes quietly wrong.

Extracted mechanically (sentences asserting a prohibition, a guarantee, a
deliberate choice, or a hazard) and grouped by app. **Recall-oriented:**
some entries are ordinary prose that matched. Read, do not trust blindly.

Each entry is `file:line` so it can be checked against the code.


## `HAIS` — 1 statements


### `HAIS/models.py`

- `HAIS/models.py:195` — **`AssetLog`** — Append-only; never updated after write.

## `OMS` — 18 statements


### `OMS/settings.py`

- `OMS/settings.py:44` — SECURITY WARNING: keep the secret key used in production secret!
- `OMS/settings.py:47` — SECURITY WARNING: don't run with debug turned on in production!
- `OMS/settings.py:116` — Mobile Version Policy: blocks out-of-date ANDROID/IOS clients with HTTP 426. Placed AFTER CorsMiddleware so it never interferes with the browser preflight, and only ever acts on requests carrying a mobile X-Platform header — the web is never gated.
- `OMS/settings.py:155` — The payments module owns its own PostgreSQL schema, so its 19 tables group together in pgAdmin instead of being scattered through `public` alongside orders, users and Django's own. `payments` comes FIRST so unqualified names resolve there, and `public` stays on the path because everything else — users_user, django_content_type, the FKs those tables point at — still lives there. Dropping public wou
- `OMS/settings.py:170` — Default HANA schema / company DB used by raw queries (hana/services/connection.py:60, tracker/sap.py:21). `.env` has historically defined this as HANA_DB_OIL_NAME, so accept that (and HANA_COMPANY_DB, which holds the same value) rather than requiring a duplicate HANA_DB_NAME key. Explicit HANA_DB_NAME still wins if it is set.
- `OMS/settings.py:356` — Access lifetime kept at 1 day: the current web/mobile clients do NOT run a refresh flow, so shortening this would log active users out mid-session (a compatibility break). Shorten once clients adopt the /auth/refresh/ endpoint added in this phase.
- `OMS/settings.py:363` — Rotation + blacklist: a refresh issues a new refresh token and the old one is blacklisted so it cannot be replayed.
- `OMS/settings.py:372` — Defaults to SECRET_KEY (so all EXISTING tokens stay valid). Override with a dedicated JWT_SIGNING_KEY in production via .env. Rotating this key invalidates every issued token, so only change it deliberately.
- `OMS/settings.py:413` — CORS_ALLOW_ALL_ORIGINS wildcards the ORIGIN only — it does NOT allow arbitrary request HEADERS. The web client attaches device/version metadata headers to every request (see Frontend-web/src/services/webDeviceService.ts), and those are non-simple headers, so the browser sends a CORS preflight first. Any header missing from this list makes the browser reject the preflight and CANCEL the real reques
- `OMS/settings.py:498` — After an IRN generation attempt: - EINV_MIRROR_HANA: write the attempt into the HANA table OMS_IRN_LOG (same column shape as the SAP add-on's @UTL_MDEXTH): 'S' on success, 'F' on failure, and a cancel stamp on cancellation. - EINV_SAP_WRITEBACK: PATCH the IRN back onto the SAP invoice's e-Billing protocol so SAP shows it as e-invoiced (validate on sandbox first). Both off by default and best-effor
- `OMS/settings.py:555` — Company DBs scanned when looking up an invoice by DocNum (the configured HANA_OIL_COMPANY_DB is always tried first). Comma-separated in .env.
- `OMS/settings.py:589` — --- Web Push (VAPID) ------------------------------------------------------- Keys for browser Web Push (Phase 3), loaded from .env via python-decouple like the rest of this file. Only the WEB client uses these — React Native / Expo push does NOT use VAPID. VAPID_PUBLIC_KEY application server key handed to the browser at subscribe time (safe to expose; it ships to clients). VAPID_PRIVATE_KEY raw ba
- `OMS/settings.py:615` — A browser subscription is permanently bound to the application server key it was created with. So silently falling back to the throwaway dev pair in a real deployment POISONS every subscription made while the fallback was active: once real keys are set, those rows can never be pushed to again (FCM answers 403, WNS answers 401). Only allow the fallback in DEBUG, and refuse to start otherwise rather
- `OMS/settings.py:635` — --- Notification framework: registration resolvers (Phase 3.6) ------------- The generic notification providers resolve a recipient's existing device registrations through these configured callables (dotted paths), so the framework never imports a business module. The concrete resolvers live in orders/notification_resolvers.py and READ (never write) the existing push_tokens / web_push_subscription
- `OMS/settings.py:680` — ========================================================================= Logging ========================================================================= Until now the project had NO LOGGING configuration at all. Every `logger.info(...)` in the codebase went to Django's default handler, which for a non-DEBUG process means nowhere — so the structured notification delivery lines (`channel=expo out

### `OMS/test_settings.py`

- `OMS/test_settings.py:0` — The live Postgres role cannot CREATE DATABASE, so Django's test runner cannot build its `test_*` database at all.
- `OMS/test_settings.py:0` — The migration history cannot be replayed onto an empty database — `users/0004_userrole_user_role` fails with "table users_role already exists".
- `OMS/test_settings.py:47` — --- HAIS schema qualifier flattened for SQLite ---------------------------- The HAIS app owns a Postgres SCHEMA and declares its tables as `db_table = 'hais"."tbl_X'`, which Postgres reads as "hais"."tbl_X". SQLite has no schemas, so it reads `hais` as an attached-database name and EVERY app's tests fail with `unknown database "hais"` — including tests that never touch HAIS, because Django builds 

## `approvals` — 38 statements


### `approvals/admin.py`

- `approvals/admin.py:40` — **`ApprovalActionInline`** — Read-only: the action log is append-only and must never be edited here.
- `approvals/admin.py:66` — Financial approval history is never deleted through the admin.

### `approvals/models.py`

- `approvals/models.py:0` — Why not extend the `orders` flow: `OrderRateApproval.order` is a concrete FK to Order (orders/models.py:435-439), and `OrderFlowConfig` expresses stages as three independent booleans with no sequence column (orders/models.py:211-213), so "Level 2 of 3" cannot be represented there at all.
- `approvals/models.py:48` — Segregation of duties: the submitter may never approve their own document.
- `approvals/models.py:56` — Partial unique: only ONE active workflow per (type, company). Superseded workflows must survive because historical requests FK into their levels, so uniqueness can only apply to active rows — which unique_together cannot express.
- `approvals/models.py:81` — How many distinct approvals clear this rung. Always 1 — one approver per stage. Kept as a field (rather than dropped) because the quorum check in services.approve reads it; editable only from Django admin if a multi-approver stage is ever genuinely needed.
- `approvals/models.py:165` — Snapshotted at submit. If an admin edits the workflow mid-flight, an in-progress request keeps the ladder it started on — otherwise "Level 2 of 3" could silently become "Level 2 of 2" and skip an approval.
- `approvals/models.py:190` — Structurally prevents two open approval chains on one document — the classic double-submit bug.
- `approvals/models.py:203` — **`level_label`** — 'Level 2 of 3' — the thing the orders flow cannot express.

### `approvals/permissions.py`

- `approvals/permissions.py:23` — **`IsApprovalAdmin`** — The one thing it does NOT open is approving your own document; `services.act` blocks that independently of any permission, so this cannot become a self-approval path.

### `approvals/serializers.py`

- `approvals/serializers.py:26` — **`validate`** — Because `level` is read-only it never reaches validated_data, so DRF cannot build its usual UniqueTogetherValidator — without this check the constraint surfaces as an uncaught IntegrityError.
- `approvals/serializers.py:26` — **`validate`** — Reject a duplicate grant with a 400 instead of a 500.
- `approvals/serializers.py:55` — Auto-assigned on create (next in the ladder) so the admin never types a number that could collide with the (workflow, sequence) unique constraint. Reordering is done with the up/down arrows, which PATCH it explicitly.
- `approvals/serializers.py:59` — Never required in a payload — create() assigns it. Declared here as well as on the field because ModelSerializer re-derives `required` from the model, where the column is NOT NULL.
- `approvals/serializers.py:103` — One approver per stage — the UI no longer offers this, so a stray value in a payload must not create a stage nobody can clear.
- `approvals/serializers.py:125` — **`validate_code`** — Clearing Meta.validators dropped the auto-generated check along with the unique-together one, so it is restored here rather than silently lost.
- `approvals/serializers.py:214` — **`get_levels`** — `position` (1-based) is what `current_level` counts, NOT `sequence` — the two differ whenever a workflow's sequence numbers do not start at 1.

### `approvals/services.py`

- `approvals/services.py:0` — Deliberately modelled on tracker/services.py, whose docstring states the intent: "All stage-movement rules live here so the views stay thin and the logic is testable in isolation." Two things it does that orders/views.py does not, and which this module must not lose: * @transaction.atomic on every transition * select_for_update() on the row being decided `UpdateOrderStatusView.post` (orders/views.
- `approvals/services.py:39` — **`register_hooks`** — The final rung fires ``on_approved`` instead, never ``on_level_advanced``.
- `approvals/services.py:59` — **`_fire`** — Atomicity is the point: a payment cannot be 'approved' without also being queued for SAP, because both writes commit together or neither does.
- `approvals/services.py:75` — **`eligible_approver_ids`** — A level with neither a role nor grants has no eligible approver — the caller surfaces that at submit time rather than letting the document deadlock at that rung days later.
- `approvals/services.py:120` — Match the PRIMARY role or any extra role — a Manager granted "Payment Approver" via extra_roles keeps "manager" as their primary, so filtering on role_id alone would never find them.
- `approvals/services.py:150` — **`current_level_approvers`** — Reuses :func:`eligible_approver_ids` for the current rung (the single source of truth for who may approve), so this never becomes a second, diverging approver-selection system.
- `approvals/services.py:150` — **`current_level_approvers`** — This is the approval-notification contract that matches the Orders reference behaviour: only the stage that now owns the document is notified, never every level at once.
- `approvals/services.py:231` — **`_level_at`** — Those two only coincide when the sequences happen to start at 1 and have no gaps — delete the first level of a workflow and they diverge, at which point matching on `sequence` finds nothing and every document deadlocks with "this level no longer exists".
- `approvals/services.py:258` — **`_reconcile_levels`** — That protection is right, but it must not STRAND the document: delete the last rung of a two-level workflow and every request parked at level 2 points at a level that no longer exists, and nobody can act on it.
- `approvals/services.py:258` — **`_reconcile_levels`** — `total_levels` is snapshotted at submit so an admin editing the ladder cannot retroactively skip an approval on a document already moving through it.
- `approvals/services.py:354` — **`submit`** — Raises ValidationError if no workflow is configured or a level has nobody who could ever approve it — failing at submit beats deadlocking later.
- `approvals/services.py:388` — Resubmit. Prior actions are NEVER deleted — round_number rises so the history of the earlier attempt stays attributable. This is exactly the reset that UpdateOrderView.put omits (orders/views.py:2208-2347), which leaves stale REJECTED rows and deadlocks the order permanently.
- `approvals/services.py:529` — **`_validate`** — Centralised so a bulk endpoint can never be laxer than the single one — the mistake tracker/services.py:163-217 explicitly avoids.
- `approvals/services.py:643` — Never show someone a document they cannot act on because they raised it.
- `approvals/services.py:683` — Remove the final rung's APPROVE so the level reads as undecided again. Rejections are never touched — they are a different outcome entirely.
- `approvals/services.py:702` — **`document_ids_for_view`** — The tracking screen's filters are about THIS approver, not about the document in the abstract: awaiting_me the request is pending AND stopped at a rung this user may act on right now — not merely "pending somewhere" approved_by_me this user personally recorded an APPROVE rejected_by_me this user personally recorded a REJECT Document status cannot express any of those: two receipts both reading PEN
- `approvals/services.py:727` — Everything THIS approver has a stake in: awaiting them now, or already decided by them. Deliberately NOT every request in the company — a rung-2 approver has no business seeing a document still sitting at rung 1, and showing it there was the bug this replaces.

### `approvals/views.py`

- `approvals/views.py:89` — The serializer needs the URL's level to check for a duplicate grant, since `level` is read-only and never appears in the request body.
- `approvals/views.py:105` — Same duplicate check as the create view — an edit that changes the company scope can collide with an existing grant just as easily.
- `approvals/views.py:115` — **`WorkflowPreviewView`** — Without this, configuring levels is guesswork — an admin cannot otherwise tell whether a level has anyone able to approve it until a document deadlocks there.
- `approvals/views.py:198` — Hide PENDING rows whose current level this user cannot act on. Everything already decided is left alone, so history stays whole.

## `attachments` — 8 statements


### `attachments/models.py`

- `attachments/models.py:0` — This table holds metadata only — there is no FileField, because Django's storage layer is deliberately not involved.
- `attachments/models.py:37` — UUID-based name actually written to the share, e.g. '9f0c2dbe8d8f4f7c.jpg'. The original name is never used on disk — it is attacker-controlled and would allow collisions and path traversal.

### `attachments/serializers.py`

- `attachments/serializers.py:6` — **`AttachmentSerializer`** — Note there is NO path field — the network location is an internal detail and is never exposed to a client.

### `attachments/storage.py`

- `attachments/storage.py:0` — This deliberately reuses the approach already proven by EINV_QR_SAVE_DIR (einvoice/services.py:386-455): smbclient with an explicit session when credentials are configured, plain filesystem I/O otherwise, and a temp-write-then-rename so a reader never sees a half-written file.
- `attachments/storage.py:26` — Validation — kept deliberately strict. The existing upload endpoints (SKU/views.py:12, legal/views.py:12) validate nothing at all.
- `attachments/storage.py:146` — `stored_name` is a UUID we generated, but treat it as untrusted anyway — basename() guarantees it can never escape the configured directory.

### `attachments/views.py`

- `attachments/views.py:0` — Cheque images are customer bank instruments, so every read here goes through an explicit authorisation check and the network path is never exposed to the client.
- `attachments/views.py:24` — **`can_view_attachment`** — Delegates to the owning document when it exposes `can_be_viewed_by`, so the rule lives with the domain object rather than being duplicated here.

## `audit` — 4 statements


### `audit/middleware.py`

- `audit/middleware.py:0` — a bulk ``queryset.update()`` or an endpoint whose model isn't individually audited), the middleware writes one simple fallback row so the change is never lost.

### `audit/signals.py`

- `audit/signals.py:0` — Connected only to the models in ``AUDITED_MODELS`` so normal sales/order traffic is never logged.
- `audit/signals.py:25` — Payments config masters. The transactional tables (PaymentReceipt, BankDeposit) are deliberately NOT here — they have their own purpose-built logs (PaymentStatusHistory, ApprovalAction, SapCallLog) which also capture IP and user agent, and routing high-volume financial writes through the per-field signal handler would only add write amplification for strictly worse data.
- `audit/signals.py:38` — Many-to-many relations to audit, as (owner model label, field name). These don't fire normal save signals, so they're handled via m2m_changed, which produces a single consolidated row per change (all added/removed at once).

## `core` — 7 statements


### `core/models.py`

- `core/models.py:33` — **`DocumentCounter`** — Allocation always goes through `next_number()`, which takes a row lock.
- `core/models.py:46` — The period the sequence resets on. Now a DAY ('20260805'); previously an Indian fiscal year ('2026-27'). The column keeps its name so the existing rows — and their unique constraint — survive the change: an old row simply holds an old-format scope that is never matched again. Widened from 9: 'YYYYMMDD' is 8 and fitted only by luck, leaving no room for a future scope (e.g. an hourly or per-branch o
- `core/models.py:88` — **`next_document_number`** — MUST be called inside the caller's transaction so the number and the row that uses it commit together — otherwise a rolled-back create burns a number and leaves a visible gap in a financial sequence.

### `core/pagination.py`

- `core/pagination.py:17` — Wrapped in the same {success, message, data} envelope the rest of the new API uses, so a client never has to special-case list endpoints.
- `core/pagination.py:34` — **`ordering_from`** — Copied from devices/admin_views.py:53-67, whose comment is worth repeating: never interpolate user input into order_by().

### `core/responses.py`

- `core/responses.py:0` — Existing endpoints are deliberately left alone; retrofitting them would break the live mobile and web clients.
- `core/responses.py:0` — The existing API has five competing shapes ({success,message,data}, bare data, raw SAP passthrough, ad-hoc keys, nested results) and five error keys (message / error / detail / details / error+detail), so a client cannot write one handler.

## `devices` — 54 statements


### `devices/admin_views.py`

- `devices/admin_views.py:51` — Only these columns may be sorted on — never interpolate user input into order_by(), which would expose arbitrary field/relation traversal.
- `devices/admin_views.py:63` — Sorting by the owning user, for the Device Activity table. Explicitly allow-listed relation fields only — never arbitrary traversal.
- `devices/admin_views.py:70` — **`_paginate`** — Server-side by design: the device table grows one row per user per device, so the browser must never receive all of it (Task 11).
- `devices/admin_views.py:132` — Derived activity status (online/idle/offline/inactive) -> a last_active window. Computed, never stored; an unknown value is simply ignored.
- `devices/admin_views.py:259` — --- version policy: latest vs old per mobile platform -------------- "Latest" = on the required build for that platform; "old" = a real device below it. Only devices with a matching active policy are classified, so platforms without a policy contribute zero to both (and the web is never counted here). This feeds the four analytics cards and drives the per-row Update Status column.
- `devices/admin_views.py:298` — --- version adoption: build -> device count, per mobile platform --- For the "Build 5 / 12 users" chart. Ordered by build descending so the newest build reads first. `required_build` is echoed so the client can colour the required bar. Distinct user count so multi-device users aren't double-counted in the "users" figure.
- `devices/admin_views.py:378` — **`AdminVersionPolicyView`** — There is intentionally no list/history and no DELETE: this is a policy, not a release log.

### `devices/context.py`

- `devices/context.py:0` — Caveat for anyone leaning on this: ``device_id`` is client-generated telemetry, not an authentication factor.
- `devices/context.py:0` — Treat a mismatch as a lead worth investigating, never as proof on its own.
- `devices/context.py:48` — **`_label_from_user_agent`** — Fallback label for a request whose device was never registered.
- `devices/context.py:60` — **`describe_request_device`** — Audit writes must never fail because telemetry was missing, so every branch here degrades to an empty string rather than raising.
- `devices/context.py:60` — **`describe_request_device`** — Both values are always present and always strings -- empty when the client sent no usable header and the User-Agent told us nothing.

### `devices/models.py`

- `devices/models.py:0` — Design decisions (indexes, the two-axis platform/app_type model, why ``build_number`` is the comparison key and version strings never are) are documented inline and in the Phase 1 foundation design.
- `devices/models.py:0` — Existing identity data (name, role, company, ...) lives on ``users.User`` and is joined via the FK, never copied here.
- `devices/models.py:0` — The deployed app is the sole source of truth for its own version and build: each client reports what it is actually running, and this table records it.
- `devices/models.py:0` — There is deliberately no server-side release/policy table — nothing here decides what *should* be running, so no admin action is needed after a deploy.
- `devices/models.py:0` — This is telemetry and compatibility data, never an authorization source (see the security notes in the Phase 1 design).
- `devices/models.py:61` — **`UserDevice`** — Uniqueness is ``(user, device_id)`` — per user, never global — so a shared tablet used by two staff is two rows, and a forged ``device_id`` can only ever touch the forger's own rows.
- `devices/models.py:87` — Device facts. Blank on platforms where they don't apply.
- `devices/models.py:98` — Web only; server-derived from the User-Agent header, never client-reported.
- `devices/models.py:105` — first_login is set once and never updated; distinct from created_at so the two can legitimately diverge if a row is ever created outside a login.
- `devices/models.py:123` — One row per install per user. This is the load-bearing invariant.
- `devices/models.py:131` — Version-distribution analytics, always grouped by platform+app_type.
- `devices/models.py:144` — Only native mobile platforms are ever version-gated. The web is a live deploy — a browser always loads the current bundle — so it must never be validated.
- `devices/models.py:152` — **`VersionPolicy`** — The rule is deliberately strict EQUALITY (build == required, version == required), per the feature spec: anything not on the exact required build is told to update.
- `devices/models.py:152` — **`VersionPolicy`** — The web is never represented here and is never validated.
- `devices/models.py:182` — At most ONE active policy per platform. A partial unique index (PostgreSQL) makes a second active row for the same platform impossible at the database level, so the middleware's "the active policy" lookup can never be ambiguous.

### `devices/permissions.py`

- `devices/permissions.py:19` — **`IsAdminRole`** — NOTE: the device tables expose every user's device names, activity and software inventory across the org, so these endpoints must never be AllowAny — unlike the older dashboard views in `orders.views`, whose permission choice is deliberately not copied here.

### `devices/serializers.py`

- `devices/serializers.py:0` — Input serializers validate the client contract strictly at the boundary but store device-reported strings loosely (bad telemetry must degrade to an ugly row, never a 500 or a failed login).
- `devices/serializers.py:104` — **`AdminUserDeviceSerializer`** — User identity is READ from the FK (never duplicated onto the device table).
- `devices/serializers.py:118` — Derived, never stored. Computed server-side so the badge can never disagree with the ?status= filter (a skewed browser clock would).
- `devices/serializers.py:164` — `now` is passed in context so every row in a page is judged against the same instant (and we don't re-read the clock per row).
- `devices/serializers.py:170` — `policies` is passed in via context (one lookup for the whole page) to avoid a query per row. Only ANDROID/IOS with an active policy are classified; everything else is "unknown" (the web is never gated).
- `devices/serializers.py:180` — **`VersionPolicySerializer`** — The one-active-per-platform rule is a DB constraint (partial unique index); this validates the version format and surfaces a duplicate as a clean field error instead of an IntegrityError.
- `devices/serializers.py:180` — **`VersionPolicySerializer`** — ``platform`` is restricted to the two mobile choices, so the web can never be given a policy through this endpoint.

### `devices/services.py`

- `devices/services.py:0` — All device writes go through here so the views stay thin and the write rules (idempotent upsert, throttled last_active, per-user scoping) live in one place and can be reused by later phases (e.g.
- `devices/services.py:0` — The user is ALWAYS passed in by the caller from ``request.user`` (the JWT subject) and never taken from client input.
- `devices/services.py:15` — last_active is a hot column. We coalesce writes to at most one per device per window via the cache, so a burst of requests from one device is a single DB update. NOTE: the project's default cache is per-process LocMemCache, so this throttle is best-effort per worker (a 4-worker deploy may write up to 4x per window). That is acceptable for a "last seen ~within 15 min" signal; switch CACHES to Redis
- `devices/services.py:23` — Mutable telemetry fields refreshed on every register/update. Identity fields (user, device_id, first_login) are set once and never in this set.
- `devices/services.py:46` — **`register_device`** — Idempotent upsert of the caller's device, keyed on (user, device_id).

### `devices/status.py`

- `devices/status.py:0` — Buckets are mutually exclusive and exhaustive, so the four counts always sum to the total: online last_active >= now - 5m idle now - 30m <= last_active < now - 5m offline now - 30d <= last_active < now - 30m inactive last_active < now - 30d Note: the spec describes "offline" as older than 30 minutes and "inactive" as older than 30 days, which overlap.
- `devices/status.py:0` — Status is NOT stored — it is a pure function of `last_active` and "now", so it can never go stale and needs no schema change.
- `devices/status.py:0` — This module is the single source of truth for the thresholds and is shared by: * the admin serializer -> the per-row `status` field (what the badge shows) * the admin list filter -> ?status=online|idle|offline|inactive * the analytics view -> the summary-card counts Keeping all three on one definition means a filtered list, its badges and the cards can never disagree.

### `devices/urls.py`

- `devices/urls.py:26` — --- admin endpoints ----------------------------------------------------- Note: these live under /api/admin/... and do not collide with Django's own /admin/ site, which is mounted at the project root.

### `devices/utils.py`

- `devices/utils.py:0` — It covers the browsers this org's users actually run and degrades gracefully to empty strings for anything it doesn't recognise (browser is telemetry, so an unknown UA must never break registration).
- `devices/utils.py:0` — The browser/OS parsing here is a deliberately small, dependency-free heuristic.
- `devices/utils.py:12` — Order matters: Edge/Opera/Samsung UAs all also contain "Chrome", and Chrome's UA also contains "Safari", so the more specific patterns must be tried first.
- `devices/utils.py:45` — **`parse_os`** — Used only as a fallback for web clients that don't self-report the OS; native clients send os_name/os_version explicitly.
- `devices/utils.py:60` — A permissive shape check: reject obvious garbage (unbounded/rich text) without being so strict that a legitimate UUID library variant is refused. The real guarantee we need is "bounded and not free-form", not "canonical UUIDv4".

### `devices/version_policy.py`

- `devices/version_policy.py:0` — Two mobile platforms (ANDROID, IOS) can be version-gated; the web never is.
- `devices/version_policy.py:104` — **`VersionPolicyMiddleware`** — The web (and any request without mobile version headers) is never touched.

### `devices/views.py`

- `devices/views.py:0` — There is no version policy to serve: the deployed app is the source of truth for its own version and build, so nothing here tells a client what it should be running.
- `devices/views.py:35` — **`DeviceRegisterView`** — Browser/OS for web clients are derived from the User-Agent server-side, never trusted from the body.
- `devices/views.py:35` — **`DeviceRegisterView`** — POST /api/devices/register/ — idempotent upsert of the caller's device.

## `einvoice` — 50 statements


### `einvoice/client.py`

- `einvoice/client.py:59` — Cache the auth session per GSTIN so identities don't collide.
- `einvoice/client.py:111` — Per NIC's reference code, the auth JSON must be Base64-encoded BEFORE RSA encryption. NIC's server RSA-decrypts, then Base64-decodes to get the JSON. Skipping the Base64 step makes that decode fail server-side and yields a generic "Application Error in Auth" (5001).
- `einvoice/client.py:148` — **`_session_from_body`** — Decrypt Data once -> JSON whose "Sek" is the plaintext base64 session key (just base64-decode it; do NOT decrypt again).
- `einvoice/client.py:148` — **`_session_from_body`** — The SEK must be AES-decrypted with our AppKey EXACTLY ONCE; decrypting it twice yields a wrong key and the server then fails with "Padding is invalid" on the first encrypted payload we send.
- `einvoice/client.py:239` — **`cancel_irn`** — reason_code: 1-Duplicate, 2-Data entry mistake, 3-Order cancelled, 4-Other.

### `einvoice/crypto.py`

- `einvoice/crypto.py:0` — The API never accepts/returns plain JSON for the sensitive parts.

### `einvoice/management/commands/auto_generate_irns.py`

- `einvoice/management/commands/auto_generate_irns.py:0` — Idempotent — invoices that already succeeded (or were skipped) are not re-processed.
- `einvoice/management/commands/auto_generate_irns.py:0` — Polling sweep: generate IRNs for recent SAP invoices that don't have one yet.
- `einvoice/management/commands/auto_generate_irns.py:54` — DocEntries already handled (succeeded or skipped) — don't re-process.

### `einvoice/management/commands/backfill_qr_png.py`

- `einvoice/management/commands/backfill_qr_png.py:0` — When the QR file-save hook fails (typically the app host cannot reach \JIVO-APP), IRN generation still succeeds: the row is written with the signed QR string in U_UTL_QRST but U_UTL_QRPT left NULL, and the report then prints no QR.
- `einvoice/management/commands/backfill_qr_png.py:0` — python manage.py backfill_qr_png [--company <DB>] [--docentry N] [--limit N] [--dry-run] Idempotent: only rows with a blank U_UTL_QRPT are touched.

### `einvoice/management/commands/run_sandbox_tests.py`

- `einvoice/management/commands/run_sandbox_tests.py:108` — Unique per run so we never hit duplicate-IRN errors.

### `einvoice/mapping.py`

- `einvoice/mapping.py:0` — SAP fills the BillFrom* block from the MAIN business place, so on a branch-to-branch document (customer = another registration of the same legal entity) it echoes the RECIPIENT's GSTIN and the invoice is rejected as NIC 2211 "supplier and recipient GSTIN must not be the same".
- `einvoice/mapping.py:55` — **`_loc`** — Pick the first non-empty place name; fall back to the GST state name so the mandatory NIC `Loc` field is never empty (error 5002).
- `einvoice/mapping.py:115` — **`_sap_tax_kind`** — SAP's own tax codes are authoritative for the jurisdiction: comparing the seller state against the place of supply silently produces CGST+SGST on a document SAP taxed as IGST whenever the seller state is misread (see the BillFromGSTIN note in the module docstring).
- `einvoice/mapping.py:152` — **`build_irn`** — Lines whose HSN cannot be resolved get "" so the pre-submit validator flags them rather than silently sending a bad code.
- `einvoice/mapping.py:165` — The invoice's own VATRegNum is the GSTIN of the branch that issued it and wins over EWayBillDetails.BillFromGSTIN, which SAP fills from the main business place (module docstring). The state always follows the GSTIN.
- `einvoice/mapping.py:172` — Export detection: a non-IN Bill-to country. Exports are always inter-state (POS = 96 "Other Territory") and carry IGST (EXPWP) or no tax under LUT (EXPWOP). For a foreign-currency export, amounts are taken from the SAP system-currency (INR) fields — NIC expects INR values, with the foreign currency reported in ExpDtls.ForCur. (SEZ supplies are not auto-detected.)

### `einvoice/models.py`

- `einvoice/models.py:55` — Cancellation (24h window). Reason: 1=Duplicate 2=Data entry 3=Order cancelled 4=Other.

### `einvoice/oms_irn_log.py`

- `einvoice/oms_irn_log.py:0` — DocEntry is an auto-increment identity, so we never supply it.
- `einvoice/oms_irn_log.py:0` — Everything here is best-effort — a failure is logged and never breaks the IRN flow.
- `einvoice/oms_irn_log.py:113` — **`irn_status_by_docentry`** — Checks the SAP add-on's @UTL_MDEXTH first, then OMS's OMS_IRN_LOG (fallback), so an invoice whose IRN was generated in SAP is recognised even though it was never written to the OMS Django table.

### `einvoice/sample.py`

- `einvoice/sample.py:0` — State codes (Stcd / Pos) are derived from the GSTINs so they always match the first two digits — NIC rejects a mismatch.

### `einvoice/sap.py`

- `einvoice/sap.py:43` — **`get_session`** — Known company DBs (OIL / BEVERAGE / MART) go through SAPServiceLayerManager, whose session cache is keyed PER COMPANY — so an OIL session is never handed to a BEVERAGE caller.
- `einvoice/sap.py:103` — **`_known_company_dbs`** — Configurable via settings.EINV_COMPANY_DBS; the configured default is always tried first.
- `einvoice/sap.py:203` — **`_get_json`** — Best-effort: these are enrichment lookups and must never fail an invoice fetch.
- `einvoice/sap.py:257` — **`normalize_seller_branch`** — On a branch-to-branch invoice — customer = another GST registration of the same legal entity — it comes back holding the RECIPIENT's GSTIN and address, which makes the e-invoice self-dealing (NIC 2211 "supplier and recipient GSTIN must not be the same") and flips the supply from inter- to intra-state.
- `einvoice/sap.py:257` — **`normalize_seller_branch`** — The document's own VATRegNum is the issuing branch's GSTIN and is authoritative, so it decides whether the block is stale.
- `einvoice/sap.py:287` — Always overwritten, even with None: a leftover address from the other registration is worse than an absent one, which the pre-submit validator reports as a missing mandatory field.
- `einvoice/sap.py:298` — BP address GST registration types that may stand in for a missing document GSTIN. Only a plain registered address qualifies. Measured on the live Oil books (CRD1."GSTType"): 9,189 addresses are type 1 "Regular/TDS/ISD" — the Service Layer's `gstRegularTDSISD` — and every one of them carries a well-formed 15-char GSTIN; 3,719 are null with no GSTIN at all (genuinely unregistered, where URP is the c
- `einvoice/sap.py:324` — **`_master_gstin`** — Matched on the document's own PayToCode / ShipToCode against BPAddresses.AddressName + AddressType — never "some GSTIN on this card".
- `einvoice/sap.py:375` — **`normalize_buyer_gstin`** — Both the Crystal bill print (`OMS_SP_GST_INVOICE`, which never mentions INV12) and the GSTR-1 extracts (`GSTR1_B2B` reports CRD1."GSTRegnNo" directly; `GSTR1_B2CS` resolves IFNULL(INV12."BpGSTN", CRD1."GSTRegnNo") and drops anything non-blank) join CRD1 on CardCode + PayToCode/ShipToCode + AdresType, exactly as this does.
- `einvoice/sap.py:375` — **`normalize_buyer_gstin`** — The document is tried first and always wins; this only runs on what it left blank.
- `einvoice/sap.py:375` — **`normalize_buyer_gstin`** — Without it `mapping` can never emit ShipDtls, which the GSTN advisory of 17.06.2026 requires from 01/08/2026 wherever ship details accompany an e-way bill.

### `einvoice/services.py`

- `einvoice/services.py:0` — Persistence is best-effort: if a NIC call SUCCEEDS but the DB write fails, we log and still return the NIC result to the caller (never lose an IRN over a DB blip), signalling the failure via the returned `record is None`.
- `einvoice/services.py:30` — **`PayloadInvalid`** — Raised when the invoice fails pre-submit validation (never reaches NIC).
- `einvoice/services.py:149` — Never downgrade an already-GENERATED record to FAILED (e.g. duplicate resubmit).
- `einvoice/services.py:199` — Cancel must be authenticated as the GSTIN that owns the IRN (multi-GSTIN PAN).
- `einvoice/services.py:368` — **`post_generate_hooks`** — Best-effort side effects after a successful IRN generation, gated by settings: - EINV_QR_SAVE_DIRS -> write the QR PNG into THIS company's folder - EINV_MIRROR_HANA -> write the row into HANA OMS_IRN_LOG (UDO-shaped) - EINV_SAP_WRITEBACK -> write the IRN back onto the SAP invoice (e-Billing) Never raises; failures are logged so they can't break the IRN flow.
- `einvoice/services.py:403` — **`save_qr_to_dir`** — Best-effort: the caller wraps this and never lets a failure break IRN generation.
- `einvoice/services.py:403` — **`save_qr_to_dir`** — Writes to a temp name then renames, so a reader never sees a half-written file.
- `einvoice/services.py:478` — **`smb_username_for`** — Interactive accounts accept either form (which is why the same code worked from a shell), so we always qualify.
- `einvoice/services.py:543` — **`_duplicate_irn`** — For a 2150 (Duplicate IRN) error, return the already-registered IRN.
- `einvoice/services.py:570` — **`auto_generate_irn`** — Best-effort: never raises, so it is safe to call inline from the invoice-create flow or a polling job.
- `einvoice/services.py:586` — Mirror failures into HANA OMS_IRN_LOG as 'F' rows (successes are written by post_generate_hooks; SKIPPED/duplicate is not a real attempt row).
- `einvoice/services.py:642` — Duplicate at NIC (2150): the invoice is already e-invoiced there but we have no local record. Surface the existing IRN in the log rather than a bare error, and treat it as SKIPPED (nothing to regenerate).

### `einvoice/validation.py`

- `einvoice/validation.py:0` — It never raises; the caller decides what to do with the errors.

### `einvoice/views.py`

- `einvoice/views.py:222` — **`list_invoices`** — List recent SAP invoices that DO NOT yet have an IRN, so the user can generate them.
- `einvoice/views.py:270` — An IRN may already exist in HANA even if the OMS Django table doesn't know it: generated by the SAP add-on (@UTL_MDEXTH) or by OMS (OMS_IRN_LOG). Check both so such invoices don't wrongly show as "not generated".
- `einvoice/views.py:381` — **`cancel_irn`** — Body: { "irn": "...", "reason_code": "2", "remarks": "..." } reason_code: 1-Duplicate, 2-Data entry mistake, 3-Order cancelled, 4-Other.

## `hana` — 10 statements


### `hana/services/connection.py`

- `hana/services/connection.py:67` — **`_open_so_schemas`** — All configured SAP company DBs (OIL / BEVERAGES / MART), de-duplicated.
- `hana/services/connection.py:310` — **`get_warehouses`** — `Inactive` is left out of the filter deliberately — it is not present on every B1 build, and a missing column fails the whole query rather than degrading.
- `hana/services/connection.py:824` — **`get_costing_code`** — Product varieties always live in dimension 1.
- `hana/services/connection.py:878` — **`get_duplicate_num_at_card`** — Cancelled documents don't hold a reference, so they're excluded.

### `hana/services/services.py`

- `hana/services/services.py:187` — **`find_duplicate_num_at_card`** — Empty list when the reference is free (or blank -- a blank NumAtCard is never a duplicate; SAP only enforces the rule on a supplied value).

### `hana/utils.py`

- `hana/utils.py:114` — A case pack of 0 would be a broken item master; guard rather than raise, so one bad item cannot take the whole report down.
- `hana/utils.py:205` — **`build_inventory_report`** — Every row-level total is the sum across *these* columns only -- so when the caller narrows the report to a few warehouses, the totals narrow with it instead of quietly reporting stock the user cannot see.
- `hana/utils.py:205` — **`build_inventory_report`** — The warehouse columns are whatever warehouses actually appear in `rows`, ordered by how much stock they hold, so the busy ones (BH-BT, BH-PF) sit first and an empty warehouse never earns a column at all.
- `hana/utils.py:245` — One item can hold stock in the same warehouse only once, but sum anyway rather than overwrite -- a duplicate row must not go missing.

### `hana/views.py`

- `hana/views.py:12` — Deliberately narrower than hana.utils.VALID_BRANCHES, which includes MART. Most Queries.* methods here have no MART arm and fall through to the OIL schema, so letting MART past this gate globally would quietly serve Oil data for a Mart request. Views whose query genuinely handles MART opt in by passing `allowed=BRANCHES_WITH_MART`.

## `invoice` — 17 statements


### `invoice/models.py`

- `invoice/models.py:70` — SAP identifiers of the invoice this log created, captured on a successful post. `sap_doc_num` is the visible invoice number; `sap_doc_entry` is the internal OINV key the Crystal bill print is actually rendered from. Kept as text because SAP only guarantees them to be printable, not numeric.
- `invoice/models.py:91` — Soft delete. The review screen hides these rows, but the log and its history survive: an invoice log is an audit record, and a reviewer clearing clutter must not be able to destroy the trail of who submitted what and why it was turned down. deleted_by is SET_NULL rather than CASCADE so removing a user account does not take the deleted rows with it.
- `invoice/models.py:113` — **`revision_chain`** — Guarded against a cycle: `supersedes` is only ever set to a pre-existing row so a loop should be impossible, but a bad backfill must not hang a request.

### `invoice/serializers.py`

- `invoice/serializers.py:24` — {item_code: product name} for the payload's lines, so the review screen can show what the item actually is instead of an FG number. Alongside the payload rather than inside it — the payload is the record of what went to SAP and must not be rewritten.
- `invoice/serializers.py:40` — Lineage is established by the create view after it has verified the source is genuinely REJECTED — never taken from the request body. The delete stamps are set only by the delete/restore endpoints, which enforce the status rules; a PATCH must not be able to bypass them.

### `invoice/services/item_names.py`

- `invoice/services/item_names.py:0` — The payload is never touched: it is the record of what was sent to SAP, and rewriting it to carry names would corrupt the audit trail.

### `invoice/services/jsap_db.py`

- `invoice/services/jsap_db.py:29` — **`JSAPConnection`** — Use as a context manager so the connection is always closed: with JSAPConnection() as jsap: flow_id = jsap.get_credit_flow_id(doc_id)

### `invoice/views.py`

- `invoice/views.py:133` — **`_close_edited_source`** — The rejection_reason is deliberately left on the row — it is the record of why the invoice was reworked.
- `invoice/views.py:148` — Only a rejected invoice can be reworked; anything else (approved, already posted to SAP, or removed from the review screen) must not be moved by a resubmission.
- `invoice/views.py:179` — A deleted entry is off the review screen; it must not be approvable, rejectable or postable to SAP until someone restores it.
- `invoice/views.py:251` — Idempotent: a double-click or a retry on a row the client has already dropped from its list must not read as a failure.
- `invoice/views.py:444` — Each ref number maps to exactly one invoice draft, so keep the log idempotent: a repeat submit (double-click / retry / StrictMode) updates the existing row instead of inserting a duplicate.
- `invoice/views.py:540` — credit_limit_logs is keyed by invoice_log_id (one request per invoice). Check BEFORE calling DSR — otherwise a duplicate attempt creates a second CL document in JSAP and only then fails on the insert.
- `invoice/views.py:603` — Lost a race with a concurrent request. The CL document exists in JSAP either way, so report it as the same conflict rather than a 500.
- `invoice/views.py:642` — Resolve the flow id straight from the JSAP database. The previous route — POST GetAllDocuments for the *current* month and scan it for this document — silently failed for any request raised in an earlier month, which is exactly when a reviewer wants to check a pending approval.
- `invoice/views.py:877` — Statuses that have NOT reached SAP. POSTED_TO_SAP is deliberately absent: once the invoice posts, SAP has already taken the stock out of the batch, so OIBT reports the reduced quantity. Holding it here as well would subtract the same pieces twice and make a batch look emptier than it is -- harmless when a hold merely hid the batch, wrong now that the quantity is netted off. This endpoint exists on
- `invoice/views.py:896` — Scoped like the review screens, so a beverage user is not blocked by an oil draft they cannot even see.

## `notifications` — 24 statements


### `notifications/apps.py`

- `notifications/apps.py:40` — Load the event registry at startup so its registration point exists for business modules to register into (from their OWN AppConfig.ready()) in a later phase. This mirrors how the approvals engine exposes its `_HOOKS` registry. It performs no database query, no network call and no business logic, and imports only this app's own submodule — never a business module, so no circular import is possible

### `notifications/constants.py`

- `notifications/constants.py:0` — They are plain strings: the framework never imports orders/payments to "know" about them.
- `notifications/constants.py:27` — --------------------------------------------------------------------------- Event-name contract. Convention (see is_valid_event_name): UPPERCASE, underscore-separated, machine-readable, independent of UI wording and of workflow status strings. e.g. PAYMENT_APPROVED — never "Approved", "Pending", or "Order Approved". These are the events the framework already anticipates. A business module will reg

### `notifications/models.py`

- `notifications/models.py:0` — A new module integrates by publishing an event with its own entity + company; it never edits this file.
- `notifications/models.py:0` — Business-module agnostic by construction: * the source object is attached generically via (content_type, object_id) + GenericForeignKey — reusing the proven ``approvals.ApprovalRequest`` pattern — so there is NO `order`/`payment`/`deposit`/`invoice` foreign key; * the only concrete relationships are the GENERIC `User` and `Company`, declared by STRING reference (`settings.AUTH_USER_MODEL`, `'users
- `notifications/models.py:0` — It is deliberately SEPARATE from the old Orders notification tables (`notifications`, `push_tokens`, `web_push_subscriptions`), which stay exactly as they are and keep serving Orders.
- `notifications/models.py:36` — Company scope. The module supplies this from the entity/event context; the framework never derives it from business logic. Nullable so notifications that are genuinely company-less (e.g. a global system message) are valid.
- `notifications/models.py:71` — Explicitly schema-qualified to the PUBLIC schema, and deliberately DISTINCT from the old Orders `notifications` table. This project runs with `search_path=payments,public` (the Payments app owns a `payments` schema). A plain, unqualified table name would be created in the FIRST schema on the path (`payments`) — wrong for a SHARED framework used by Payments, Deposits and future modules. Pinning `pu

### `notifications/providers/__init__.py`

- `notifications/providers/__init__.py:0` — A new channel (Email, WhatsApp, SMS, …) is added by writing a provider that implements :class:`~notifications.providers.base.NotificationProvider` and listing it in :func:`default_providers` — the dispatcher and business modules do not change.

### `notifications/providers/base.py`

- `notifications/providers/base.py:0` — Contract: ``send()`` MUST NOT raise for an ordinary delivery failure — it captures the outcome in a :class:`ProviderResult`.
- `notifications/providers/base.py:0` — New channels (Email, WhatsApp, SMS) are added by implementing this interface; the dispatcher and business modules never change.
- `notifications/providers/base.py:0` — The dispatcher additionally isolates any unexpected exception so one provider can never break the others.

### `notifications/providers/resolvers.py`

- `notifications/providers/resolvers.py:0` — A resolver that raises is isolated and logged — it never breaks notification creation or the business operation.
- `notifications/providers/resolvers.py:0` — It therefore calls a resolver configured by DOTTED PATH in settings; the concrete resolver lives outside ``notifications/`` (it may read Orders-owned tables) and is loaded lazily via ``import_string`` so the framework never imports it — or any business module — directly.

### `notifications/registry.py`

- `notifications/registry.py:0` — Business modules register their handlers in a later phase; what a handler *does* (the dispatcher contract) is also a later phase and is deliberately left unspecified here.
- `notifications/registry.py:28` — **`register`** — Raises on a malformed event name, a non-callable handler, or a duplicate registration, so wiring mistakes fail loudly at startup rather than silently.

### `notifications/serializers.py`

- `notifications/serializers.py:0` — Business-module-agnostic: the entity is surfaced GENERICALLY as ``entity_type`` / ``entity_id`` (from the canonical payload builder), never as an order/payment/deposit FK.

### `notifications/services/dispatcher.py`

- `notifications/services/dispatcher.py:37` — **`notify`** — Never trusts a client-supplied id.
- `notifications/services/dispatcher.py:37` — **`notify`** — Resolved internally to (content_type, object_id) — the caller never builds those.
- `notifications/services/dispatcher.py:37` — **`notify`** — The framework never infers type or module from this text.
- `notifications/services/dispatcher.py:37` — **`notify`** — recipients : iterable of users Each must be an instance of the project's user model.
- `notifications/services/dispatcher.py:119` — **`_deliver`** — One recipient's or provider's failure never stops the others, and this never raises — a push failure must not turn the business operation into a 500.

### `notifications/services/payloads.py`

- `notifications/services/payloads.py:0` — Mobile and Web providers deliver the same dict; there are no per-channel duplicate builders.
- `notifications/services/payloads.py:0` — The entity is represented GENERICALLY as ``entity_type`` + ``entity_id`` (from the GenericForeignKey), never as ``order_id``/``payment_id``/etc.

## `orders` — 87 statements


### `orders/admin.py`

- `orders/admin.py:16` — --------------------------------------------------------------------------- Notification delivery — read-only operational views --------------------------------------------------------------------------- These three models had no admin at all, so the only way to answer "was this user actually notified?" or "does this device still have a live token?" was a database client. They are registered here 

### `orders/apps.py`

- `orders/apps.py:8` — Register the VAPID key-pair validation so a mismatched pair fails at startup instead of silently breaking Web Push for every browser.

### `orders/checks.py`

- `orders/checks.py:0` — If the server later signs with a private key from a *different* pair, every existing subscription is rejected forever -- FCM answers ``403`` ("the VAPID credentials in the authorization header do not correspond to the credentials used to create the subscriptions") and WNS answers a bare ``401``.
- `orders/checks.py:0` — The dangerous part is that a mismatched pair looks completely healthy at boot: tokens are signed fine, requests are sent fine, and the failure only shows up as per-subscription rejections later.

### `orders/management/commands/generate_vapid_keys.py`

- `orders/management/commands/generate_vapid_keys.py:0` — It NEVER writes any file (does not touch ``.env``), so it is safe to run as many times as you like — each run just prints new keys.

### `orders/management/commands/migrate_schemes_v2.py`

- `orders/management/commands/migrate_schemes_v2.py:0` — Idempotent — re-running updates in place rather than duplicating.
- `orders/management/commands/migrate_schemes_v2.py:36` — **`slugify_code`** — scheme_name is free text ("1 free pcs on 10 boxes"), so the slug is truncated and de-duplicated with a numeric suffix rather than trusted to be unique.
- `orders/management/commands/migrate_schemes_v2.py:179` — A scheme with no trigger never fires. `party_product_assignments.scheme_id` is the only place the legacy model recorded which product earns a scheme, and on deployments where the Add Sales picker sets the scheme straight on the order line that column is empty — so there is genuinely nothing to migrate. Those schemes need a trigger typed in before they work in v2.

### `orders/management/commands/prune_push_tokens.py`

- `orders/management/commands/prune_push_tokens.py:0` — Accordingly it: * appends a timestamped record of every run (success or failure) to ``settings.PUSH_TOKEN_CLEANUP_LOG``; * takes a lock (``settings.PUSH_TOKEN_CLEANUP_LOCK``) so a slow run can never overlap the next night's run; * never raises past ``handle()`` -- failures are logged in full and the process exits non-zero, so tomorrow's run is unaffected.
- `orders/management/commands/prune_push_tokens.py:48` — A lock older than this is assumed to be from a crashed run and is broken, so a hard kill can never disable the cleanup permanently.
- `orders/management/commands/prune_push_tokens.py:57` — **`_append_log`** — Logging must never break the cleanup itself.
- `orders/management/commands/prune_push_tokens.py:82` — O_EXCL is atomic on both Windows and POSIX: whoever creates the file wins, so two runs can never both proceed.
- `orders/management/commands/prune_push_tokens.py:139` — A missing receipt just means "not ready yet" -- leave the token active and let a later run decide. Never guess.
- `orders/management/commands/prune_push_tokens.py:210` — Exit non-zero so monitoring / Task Scheduler shows the failure. The schedule itself is unaffected: tonight's failure never stops tomorrow's run.

### `orders/management/commands/prune_web_push_subscriptions.py`

- `orders/management/commands/prune_web_push_subscriptions.py:0` — It: * appends a timestamped record of every run (success or failure) to ``settings.WEB_PUSH_CLEANUP_LOG``; * takes an atomic lock (``settings.WEB_PUSH_CLEANUP_LOCK``) so a slow run can never overlap the next night's run; * never raises past ``handle()`` -- failures are logged in full and the process exits non-zero, so tomorrow's run is unaffected; * processes every subscription independently -- on
- `orders/management/commands/prune_web_push_subscriptions.py:0` — This command is the **safety net** for subscriptions that never receive a notification (they would otherwise linger active forever).
- `orders/management/commands/prune_web_push_subscriptions.py:0` — We send a tiny data-only *keepalive* payload the service worker handles silently (see ``public/service-worker.js``), so **nothing is ever shown to a user**; we only care about the HTTP status the push service returns.
- `orders/management/commands/prune_web_push_subscriptions.py:0` — Why this exists --------------- A browser ``PushSubscription`` dies silently: the user clears site data, the endpoint expires, the browser garbage-collects it, or the machine is gone.
- `orders/management/commands/prune_web_push_subscriptions.py:52` — A lock older than this is assumed to be from a crashed run and is broken, so a hard kill can never disable the cleanup permanently.
- `orders/management/commands/prune_web_push_subscriptions.py:61` — **`_append_log`** — Logging must never break the cleanup itself.
- `orders/management/commands/prune_web_push_subscriptions.py:86` — O_EXCL is atomic on Windows and POSIX: whoever creates the file wins, so two runs can never both proceed.
- `orders/management/commands/prune_web_push_subscriptions.py:131` — deliver_web_push already deactivates dead rows (404/410/401/403) and swallows transient errors -- one bad endpoint never stops the loop. In --dry-run we still probe (to classify each one) but revert any deactivation it performs.

### `orders/models.py`

- `orders/models.py:333` — `qty_scheme` above is always PIECES, because that is what a SAP DocumentLine quantity means. The pair below records the giveaway as the scheme actually spelled it — "1 BOX" — so the UI and any later audit can show the unit instead of a bare converted number. Also a snapshot: a pack size changing in SAP must not retroactively alter an approved order.
- `orders/models.py:404` — The business line this offer belongs to. An OIL scheme never fires on a MART or BEVERAGES line, however it was targeted — targeting says *who* gets an offer, this says *what it is an offer on*. Blank = every category, which is what every scheme created before this field existed means.
- `orders/models.py:564` — A carve-out. Exclusions are absolute at every level, not specificity-ranked — an explicit carve-out is always deliberate.
- `orders/models.py:648` — **`WebPushSubscription`** — Separate from :class:`PushToken` (Expo/mobile) so that web push work never touches the mobile token flow.

### `orders/notification_resolvers.py`

- `orders/notification_resolvers.py:0` — It is intentionally a NEW file so the existing Orders notification implementation is left completely untouched.
- `orders/notification_resolvers.py:0` — These functions are strictly READ-ONLY: they never create, update or delete a registration, and they add no business-module fields.

### `orders/notifications.py`

- `orders/notifications.py:0` — NOTE (Task 7): the Expo push behaviour is intentionally preserved byte-for-byte -- same endpoint, same payload shape, same headers, same timeout.
- `orders/notifications.py:0` — Responsibilities are deliberately separated: * :class:`NotificationTemplates` -- generate the user-facing message text (Task 4: centralised, reusable message templates).
- `orders/notifications.py:0` — This module never imports from ``views.py``, keeping the dependency graph acyclic.
- `orders/notifications.py:37` — Expo delivery *receipts*. A ticket only means "Expo accepted the message" -- the real outcome (notably DeviceNotRegistered, i.e. the app was uninstalled, its data cleared, or the token rotated) is only reported here. Without this check dead tokens are never noticed: the send logs "accepted" and the row stays active forever, so one user accumulates a pile of dead tokens and the push silently goes n
- `orders/notifications.py:89` — **`NotificationTemplates`** — The strings are intentionally identical to the previous inline messages so that existing notification behaviour is unchanged.
- `orders/notifications.py:99` — --- Push headings (title). The body always stays the message below, so navigation/logic never depends on the title text. ----------------------
- `orders/notifications.py:166` — **`_build_push_data`** — Existing keys (``notification_id``, ``order_id``, ``screen``) are always present for backward compatibility (Task 9).
- `orders/notifications.py:166` — **`_build_push_data`** — The two builders previously repeated the same eight keys, so a field added to one silently diverged from the other; this keeps a single definition of the payload shape while producing exactly the same dict Expo received before.
- `orders/notifications.py:379` — Anything unforeseen (malformed response shape, encoding error). Delivery is best-effort: a push failure must never surface as a 500 on the business request that triggered it.
- `orders/notifications.py:392` — **`build_notification_payload`** — A single source of truth so both channels navigate from the same structured fields (order_id/event_type) rather than parsing message text.
- `orders/notifications.py:416` — **`deliver_notification`** — It persists the record, then best-effort delivers to: * Expo mobile push (existing behaviour, unchanged) * browser Web Push (Phase 3, additive) A failure in one channel never blocks the other or the request.
- `orders/notifications.py:432` — Mobile. Wrapped for the same reason web push always was: the record is already committed, so a transport failure must not propagate into the business request that triggered it. Previously only the internals of send_push_notification were guarded, so a database error while reading PushToken rows would surface as a 500 on order approval.
- `orders/notifications.py:480` — **`deliver_notification_to_many`** — One recipient failing never stops the rest: ``deliver_notification`` isolates each channel, and the summary line below records how many of the intended recipients actually got a record.

### `orders/scheme_engine.py`

- `orders/scheme_engine.py:106` — `qty` is in `free_uom` — what the scheme was written in, and what the UI shows. `qty_pieces` is the same giveaway expressed in single units, which is the only thing SAP understands: DocumentLines.Quantity is always pieces, never cartons. For a PCS benefit the two are equal; for BOX, qty_pieces is qty x the giveaway item's sal_factor2.
- `orders/scheme_engine.py:193` — **`_scope_filter`** — Blank context values are left out rather than matched as '' — a party with no state must not collect every scheme whose scope_value happens to be empty.
- `orders/scheme_engine.py:406` — **`_apply_pack_factors`** — A scheme may be written in cartons ("1 box free"), but a SAP DocumentLine quantity is always in single units — the paid line sends the order's `qty`, which is pieces.
- `orders/scheme_engine.py:496` — Both must be present and equal — a missing category is a mismatch.
- `orders/scheme_engine.py:508` — Third of the three: the scheme's own category. Under the strict order-flow rule it must be present and equal to the party/product category — a blank (wildcard) scheme is not shown.

### `orders/serializers.py`

- `orders/serializers.py:174` — Preserve a real comment the billing user typed; only fall back to the generic "Accepted by billing" label when they approved with no note (otherwise the typed remark is lost and never shown).
- `orders/serializers.py:546` — **`SchemeWriteSerializer`** — Rejects an exact duplicate of (scheme_name, state_code, item_code).
- `orders/serializers.py:650` — per_qty is a divisor; a ratio with nothing on the giveaway side would silently produce zero free stock on every order.
- `orders/serializers.py:683` — `scheme` is a non-null FK, so DRF would demand it in the request body — but the parent is always known from context: the URL for the assignments endpoint, or the enclosing scheme for a nested write. Read-only keeps it in the response without requiring callers to repeat it.

### `orders/views.py`

- `orders/views.py:381` — The free half of a combo pack is priced at 0 by design, so the 0-vs-0 comparison below must not drag the whole order into rate approval.
- `orders/views.py:414` — **`_scheme_entry`** — `benefit_item_code` is a snapshot: the SAP push reads it rather than re-resolving the giveaway item, so editing a scheme cannot change what an approved order ships.
- `orders/views.py:427` — `qty` is what ships, and a SAP DocumentLine quantity is always pieces. A BOX benefit therefore arrives already converted (scheme_qty), with the unit it was written in kept alongside so the UI can still say "1 box".
- `orders/views.py:448` — **`_scheme_v2_category_allows`** — Strict mirror of the engine's category wall (scheme_engine.resolve_schemes, strict_category=True) at save time, so a stale or hand-rolled client cannot store a scheme that the UI would never have shown.
- `orders/views.py:561` — **`_apply_engine_schemes`** — An older client, a resumed draft, or an order placed through a screen that never called the preview endpoint would otherwise save no giveaway at all — and since the SAP push builds its free lines from `OrderItemScheme`, the customer's free stock would silently never ship.
- `orders/views.py:561` — **`_apply_engine_schemes`** — Anything the client already sent for the same (line, giveaway item) is left alone, so a hand-typed override is never overwritten.
- `orders/views.py:561` — **`_apply_engine_schemes`** — Re-resolving server-side makes the engine the single source of truth for what is owed.
- `orders/views.py:625` — Snapshot: the SAP push ships exactly this, so editing the scheme afterwards cannot change what an approved order sends.
- `orders/views.py:671` — Mart / distributor flow constants — kept in one place so the queue, the create branch and the approve/reject endpoints never drift.
- `orders/views.py:2180` — One free unit per combo unit: the order form counts pieces, and a combo pack carries one of the free product per piece. The trailing "4 PCS" / "6 PCS" in a combo name is the paid SKU's carton config (it equals sal_factor2), not a free count, so it is deliberately not parsed. Set `free_qty_per_unit` on the assignment for the rare pack that gives away more than one.
- `orders/views.py:2234` — Free of cost — the auto-added order line is always zero-priced.
- `orders/views.py:2295` — Combo pack -> free-of-cost companion line. `free_item` is null when the combo has no mapping yet, and the UI then behaves as it always did.
- `orders/views.py:2757` — Distributor edit (Mart Approval role adjusting the order): re-save the lines and keep it in the Mart flow — never route through billing.
- `orders/views.py:2815` — If the edit sends the order back into Rate Approval, a fresh approval round begins (a new pending rate-approval log is created below), so any prior approver decisions must be cleared back to PENDING. Otherwise the edit bypasses rate approval and existing decisions are preserved by assign_rate_approvers().
- `orders/views.py:2847` — Save every unique order as a template, but skip true duplicates.
- `orders/views.py:2917` — ── Distributor (Mart / company 3) orders take their OWN path ──────── They never enter the billing / auditor / rate-approval flow: they are simply recorded and parked at "Mart Approval" for the Mart Approval role to review. This keeps the entire billing flow below untouched.
- `orders/views.py:3002` — **`_finalize_distributor_order`** — Deliberately isolated from the billing/auditor/rate-approval machinery: no rate approvers, no templates, no billing notifications.
- `orders/views.py:3021` — Distributor orders always dispatch from the factory.
- `orders/views.py:3024` — Default every distributor order to GP-FGM, regardless of what the client sent, so the warehouse is always stored. An explicit choice (e.g. Mart Approval switching to DL-MP on the edit screen) is preserved.
- `orders/views.py:3031` — Land at the Mart Approval stage (status id 12). On a fresh submit we always move it there; on an approver edit we only reset it to pending if it isn't already an approved/rejected order.
- `orders/views.py:3696` — If a placeholder row exists for this status (created earlier with no performer), update it instead of inserting a duplicate row.
- `orders/views.py:3795` — A distributor (or any non-privileged user) can only ever see the orders they themselves placed, never another user's.
- `orders/views.py:4167` — **`MartResendSapView`** — Used when the initial SAP push (at Mart approval) failed: the order is left in 'Mart Approved' and never reached 'Completed'.
- `orders/views.py:4356` — **`SchemeManageListView`** — `SchemeListView` (/orders/schemes/) is deliberately left alone — it feeds the Add Sales picker and returns only scheme_id/scheme_name/state_code.
- `orders/views.py:4540` — **`_active_users_with_role`** — This targets a specific role -- it is never a broadcast to all users.
- `orders/views.py:4553` — **`_resolve_notification_recipients`** — ``recipients`` is a list of ``User`` objects -- always the exact user(s) responsible for the next action, never a broadcast to all users.
- `orders/views.py:4752` — **`delete`** — Optional endpoint -- older app versions that never call it are unaffected.
- `orders/views.py:4802` — update_or_create on the unique endpoint prevents duplicates and re-homes an endpoint to the current user if it moved.
- `orders/views.py:5206` — SAP/HANA unreachable: return what we know but don't break the page.
- `orders/views.py:5227` — **`CancelSalesQuotationView`** — The SAP cancellation is the source of truth: OMS is only updated if SAP confirms.
- `orders/views.py:5515` — **`delete`** — OrderItemScheme.scheme_v2 is PROTECT, so a scheme referenced by any order line cannot be deleted at all -- the giveaway record has to survive.
- `orders/views.py:5611` — **`SchemePreviewView`** — Body: {card_code, category, lines: [{item_code, category, sub_group, brand, qty, pcs, boxes, ltrs, is_auto_free, combo_source_code, item_type}, ...]} This is what makes state-wide targeting usable -- the salesperson never picks a scheme from a dropdown, the engine proposes and they confirm.

### `orders/webpush.py`

- `orders/webpush.py:0` — Kept separate from the mobile push code (``notifications.send_push_notification``) so the two channels never interfere (Task 6).
- `orders/webpush.py:21` — HTTP statuses a push service returns for a permanently dead subscription. 404/410 -- the browser unsubscribed / the endpoint no longer exists. 403/401 -- the row was created against a DIFFERENT application server key (the VAPID pair was rotated, or it predates the real keys and was made with the dev fallback). A subscription is permanently bound to the key it was created with, so this can never su
- `orders/webpush.py:50` — VAPID "sub" must be a mailto: (or https:) URI. Accept the admin email with or without an explicit "mailto:" prefix so either .env style works.
- `orders/webpush.py:74` — **`deliver_web_push`** — On a permanent failure (see ``_GONE_STATUSES``: the browser unsubscribed, the endpoint expired, or the row is bound to an old VAPID key) the row is switched off immediately so we never target it again.
- `orders/webpush.py:74` — **`deliver_web_push`** — Transient errors are logged and swallowed so one bad endpoint never breaks a batch.

## `payments` — 208 statements


### `payments/analytics.py`

- `payments/analytics.py:0` — Anything still in the OMS pipeline (draft, awaiting approval, mid-post) or that SAP refused or never confirmed is excluded.
- `payments/analytics.py:0` — The rest of this app sums in Python because it is always working with one document's few lines; a dashboard spans every receipt ever raised, and materialising those to add up a column would get slower every month.
- `payments/analytics.py:0` — They are money, so Decimal is what the database and the ORM carry, but JSON has no decimal type and the client formats for display only — never for arithmetic that is written back.
- `payments/analytics.py:53` — ONLY documents that reached SAP successfully. The dashboard reports money that is settled in the books of record, not money that is somewhere in the OMS pipeline. Everything else is excluded and each for its own reason: DRAFT / PENDING_APPROVAL / APPROVED / POSTING_TO_SAP still in flight — no SAP document exists yet, and an approval can still be refused, so counting these would report revenue that
- `payments/analytics.py:99` — The subset that needs a human to look. The rest advance on their own as the approval chain moves; these two are stuck until someone acts: PENDING_ERROR SAP refused it — correct and resubmit. SAP_UNKNOWN SAP never answered — must be reconciled before retrying, or the payment could post twice.
- `payments/analytics.py:140` — Both ends are required; a half-open custom range is a client bug, and silently substituting today would show numbers nobody asked for.
- `payments/analytics.py:155` — Anything unrecognised falls back to the default window rather than erroring, so a stale client cannot break the page. Derived from DEFAULT_PRESET so the fallback cannot drift away from it.
- `payments/analytics.py:179` — **`_pending`** — Two aggregates rather than one combined figure: a pending receipt and a pending deposit are different problems for different people, and adding them would also double-count the same money where a receipt has been banked but neither document has posted.
- `payments/analytics.py:310` — Fixed order so the colours do not shuffle between refreshes. Any method that appears in the data but not in the model choices is still shown, appended after the known ones, rather than silently dropped.
- `payments/analytics.py:351` — **`_participants`** — FOUR participation paths, and they do not share an identity type — which is why rows are keyed by `(kind, id)` rather than a bare integer: collected PaymentReceipt.received_from_person -> CollectionPerson banked BankDeposit.deposited_by -> CollectionPerson recorded PaymentReceipt.created_by -> User submitted BankDeposit.created_by -> User A CollectionPerson and a User can both have id 3 and be dif
- `payments/analytics.py:455` — **`collection_performance`** — Not limited to a top few: the point of the table is to show the whole team, and a cut-off would silently hide the people whose figures most need looking at.
- `payments/analytics.py:504` — Name sorts alphabetically; the money columns sort by magnitude. Name is cased-folded so "amit" and "Amit" do not end up in separate blocks.

### `payments/analytics_person.py`

- `payments/analytics_person.py:93` — **`_daily_series`** — Past the cap the series is omitted and the client falls back to the totals rather than drawing a dense smear that implies detail it cannot show.

### `payments/apps.py`

- `payments/apps.py:10` — Register the approval hooks. Done here (not at import time) so the approval engine never imports `payments`, which would be circular.

### `payments/bank_master.py`

- `payments/bank_master.py:22` — **`BankMasterUnavailable`** — SAP unreachable and nothing cached — the bank list cannot be verified.
- `payments/bank_master.py:28` — Two entries, deliberately. FRESH expires after the TTL and is what makes a newly-configured SAP bank appear. STALE long outlives it purely so an outage serves the last known-good list instead of an empty one, which would read as "no bank exists".
- `payments/bank_master.py:121` — **`PaymentAccountResolver`** — Two inputs, deliberately separated: * SAP owns the ACCOUNTS (bank_master, cached from DSC1) * OMS owns the CHOICE of which account each method uses (PaymentMethodMapping) Note this is always OUR account.
- `payments/bank_master.py:162` — **`deposit_source_gl`** — Separate from cash_gl() because SAP validates the two roles differently: a receipt's CashAccount must be a cash-flow account (OACT.Finanse='Y'), while a deposit's CardCode must NOT be.

### `payments/hana_queries.py`

- `payments/hana_queries.py:0` — The schema name still has to be interpolated (HANA cannot bind an identifier), so it comes from SapCompanyMap — a DB allow-list — and never from a request.
- `payments/hana_queries.py:0` — `HANAConnection.execute(sql, params)` has always supported them (connection.py:42-44); they were simply never used, which is why `get_customer_details` interpolates request query params straight into SQL.
- `payments/hana_queries.py:70` — **`fetch_open_invoices`** — * PaidToDate can be NULL, so IFNULL is required or the arithmetic yields NULL and the row silently disappears.
- `payments/hana_queries.py:88` — HANA CHAR columns come back space-padded — always strip.
- `payments/hana_queries.py:96` — **`parties_with_open_invoices_sql`** — The WHERE clause is deliberately IDENTICAL to open_invoices_sql — if the two ever drift, a party appears in the picker and then shows an empty invoice list (or disappears while genuinely owing).
- `payments/hana_queries.py:224` — **`company_banks_sql`** — DSC1 is the account table and is the source of truth: a bank present in the ODSC master but absent here has no account to pay into or out of, and SAP refuses the document.
- `payments/hana_queries.py:251` — **`fetch_company_banks`** — Deliberately uncached: caching, and what to do when this raises, belong to the service layer that knows whether a stale answer is acceptable.
- `payments/hana_queries.py:268` — SAP has no dedicated IFSC column; Indian localisations keep it in ControlKey, falling back to SWIFT. Blank when neither is filled in — never invented.
- `payments/hana_queries.py:295` — **`invoice_details_sql`** — `DocTotal` is the invoice's own value and is NEVER the amount a payment applied to it — a partial payment leaves the two different, which is exactly what the receipt PDF must show.
- `payments/hana_queries.py:316` — **`fetch_invoice_details`** — ONE query for all of them — never one per allocation.
- `payments/hana_queries.py:371` — **`fetch_invoice_branches`** — Deliberately NOT swallowing errors: the branch decides which SAP ledger a payment lands in, so "could not check" must never be mistaken for "use the default".
- `payments/hana_queries.py:371` — **`fetch_invoice_branches`** — Raises if SAP cannot be reached.
- `payments/hana_queries.py:409` — **`incoming_payment_series_sql`** — Locked='N' and IsManual='N' mirror the existing series lookup at hana/services/connection.py:398-406 — a locked or manual-numbering series cannot be used for an automated post.
- `payments/hana_queries.py:439` — **`fetch_incoming_payment_series`** — Company-specific by design: the SAME month is a different series in each database (August 2026 is 2514 in BEVERAGES and 2564 in TEST_OIL), so this must never be hardcoded or shared between companies.
- `payments/hana_queries.py:494` — **`fetch_payment_cancellation`** — Returning None on ANY failure is deliberate: reconciliation must never downgrade a document because SAP was briefly unreachable.
- `payments/hana_queries.py:526` — **`fetch_payment_trans_id`** — It is the only key that reaches JDT1, so without this read the journal entry cannot be linked from OMS.
- `payments/hana_queries.py:526` — **`fetch_payment_trans_id`** — TransId is a convenience for tracing, never a posting precondition — the payment has already succeeded by the time this runs, and a failure here must not disturb that.

### `payments/hooks.py`

- `payments/hooks.py:0` — Every hook runs INSIDE the approval transaction, which is the point: a receipt cannot be marked approved without also being queued for SAP, because both writes commit together or neither does.
- `payments/hooks.py:0` — The approval engine never imports `payments` (that would be circular); it looks these up by model label instead.
- `payments/hooks.py:15` — **`_on_receipt_approved`** — Deferred to transaction.on_commit rather than run inline, for two reasons: a 5-7 second SAP call would hold the approval's row locks open for its whole duration, and a SAP failure must NOT roll the approval back.
- `payments/hooks.py:36` — Notify the submitter that their receipt was approved. Runs inside the approval transaction: the Notification records commit with the approval, and external push delivery is deferred to on_commit (so a rollback sends nothing). Delivery failures are isolated by the framework and never break the approval.
- `payments/hooks.py:85` — **`_on_receipt_level_advanced`** — The engine has already incremented current_level before firing this, so the publisher resolves exactly the rung that now owns the receipt (Orders-parity: only the current stage is notified, never every level at once).

### `payments/management/commands/reconcile_sap_cancellations.py`

- `payments/management/commands/reconcile_sap_cancellations.py:0` — It never posts, reposts or cancels anything in SAP; it only reads ORCT.Canceled and records what it finds in OMS.

### `payments/models.py`

- `payments/models.py:0` — * Money invariants are DB CheckConstraints, not only serializer rules.
- `payments/models.py:35` — **`SapCompanyMap`** — Replaces `resolve_company_db_for_order` (sap_sync/services/sync_service.py:301), which derives the DB from ITEM category (a payment has no items), only handles BEVERAGES, and silently routes MART to the OIL database.
- `payments/models.py:49` — SAP G/L for cash receipts. NOT from DSC1: a cash drawer is not a house bank account, so SAP has no row for it and it must be named here. Every other G/L is resolved live from the bank the user selected.
- `payments/models.py:53` — SAP G/L credited when collected cash is BANKED (the deposit's CardCode). It cannot be `cash_gl_account`. A deposit posts as a DocType 'A' account transfer, and SAP refuses a cash-flow account (OACT.Finanse='Y') as the CardCode of one — which is exactly what a cash drawer G/L is. The same account is required, and valid, as CashAccount on a receipt, so one field cannot serve both: receipts need Fina
- `payments/models.py:111` — **`PaymentMethodMapping`** — This table holds only business configuration: one bank can expose several G/L accounts and OMS cannot guess which one a tender should use, so an administrator says it once here and no user ever sees a G/L number again.
- `payments/models.py:111` — **`PaymentMethodMapping`** — `bank_key` is SAP's "BANKCODE:GLACCOUNT" and is stored as plain text on purpose: SAP is the master, so a foreign key is impossible, and resolution happens against the live cache every time it is used.
- `payments/models.py:172` — SAP never answered (timeout / connection lost). The document may or may not exist in SAP, so resubmitting could duplicate a payment. Reconciliation resolves it; the creator cannot resubmit meanwhile.
- `payments/models.py:190` — Frozen at creation. Deriving it at post time would mean an .env edit between draft and post silently posts to a different company.
- `payments/models.py:206` — Derived from the method rows inside the create transaction — never taken from the client payload (which is what orders/views.py:2530 does, leaving total_amount disagreeing with SUM(items) after a partial write).
- `payments/models.py:228` — SAP's exact words from the last posting attempt — the success confirmation or the rejection reason. Shown verbatim in the UI so a user can act on it without database access or developer help. SAP branch chosen by the user, for an ADVANCE only. An invoice payment inherits its branch from the invoice and must never be overridden — SAP refuses a mismatch. An advance settles nothing, so there is no br
- `payments/models.py:241` — SAP's own words, stored exactly as returned and NEVER rewritten. `sap_response` above is written for the person holding the document and adds a plain-language summary plus a "what to do" line. That is right for a collector and useless for a SAP administrator, who needs the literal string to search notes and logs against. Keeping both means neither audience is served a translation of the other's me
- `payments/models.py:251` — ---- SAP-side cancellation ------------------------------------------- A document can post successfully and be cancelled IN SAP afterwards. These fields are SEPARATE from sap_response on purpose: sap_response records what SAP said about the POSTING, which succeeded and remains historically true. Overwriting it would destroy that record. ORCT.CancelDate, read back during reconciliation. Null until 
- `payments/models.py:260` — Human-facing explanation of the cancellation, composed by the reconciliation service. Never SAP's posting response.
- `payments/models.py:326` — Tenders an employee physically carries to a bank, so they can appear in an OMS Bank Deposit. UPI (and any future NEFT/RTGS) arrives electronically — there is nothing to hand over, and offering it would invite a deposit for money nobody ever held. CASH and CHEQUE are depositable for DIFFERENT reasons, and the deposit poster depends on the distinction: CASH — still sitting in the cash-sale clearing 
- `payments/models.py:475` — SAP never answered (timeout / connection lost). The document may or may not exist in SAP, so resubmitting could duplicate a payment. Reconciliation resolves it; the creator cannot resubmit meanwhile.
- `payments/models.py:535` — SAP's own words, stored exactly as returned and NEVER rewritten. `sap_response` above is written for the person holding the document and adds a plain-language summary plus a "what to do" line. That is right for a collector and useless for a SAP administrator, who needs the literal string to search notes and logs against. Keeping both means neither audience is served a translation of the other's me
- `payments/models.py:545` — ---- SAP-side cancellation ------------------------------------------- A document can post successfully and be cancelled IN SAP afterwards. These fields are SEPARATE from sap_response on purpose: sap_response records what SAP said about the POSTING, which succeeded and remains historically true. Overwriting it would destroy that record. ORCT.CancelDate, read back during reconciliation. Null until 
- `payments/models.py:554` — Human-facing explanation of the cancellation, composed by the reconciliation service. Never SAP's posting response.
- `payments/models.py:578` — These two encode exactly the rules already enforced in the mobile UI, so the API cannot be bypassed to store an invalid deposit.
- `payments/models.py:618` — A receipt can never appear in two deposits — the money-safety constraint on the deposit side.
- `payments/models.py:654` — Always a POST to one of two endpoints, so the verb is not stored. Retries are not stored either: posting is synchronous, so one call is one row, and the attempt sequence across resubmissions is readable from the ordered SAP_* entries in PaymentStatusHistory.
- `payments/models.py:720` — What happened. Defaults to STATUS_CHANGED so every existing row stays valid without a data migration inventing an action it never recorded.

### `payments/notification_events.py`

- `payments/notification_events.py:0` — Company is therefore derived per-recipient inside ``notify()`` from the recipient's own ``users.Company`` (server-side) — a receipt raised by a user in company A notifies that user in company A, never a company-B user.
- `payments/notification_events.py:0` — Company note: ``PaymentReceipt.company`` is a SAP *category* string (CATEGORY_CHOICES), not a ``users.Company`` FK, so it cannot be passed as the framework's ``company`` (a ``users.Company``).
- `payments/notification_events.py:0` — Dependency direction: payments -> notifications (never the reverse).
- `payments/notification_events.py:28` — Event names OWNED BY PAYMENTS. The framework validates the name format only. Lifecycle split (see docs/NOTIFICATION_INTEGRATION.md): *_SUBMITTED -> the eligible APPROVERS (a decision is now required of them) *_APPROVED -> the SUBMITTER (the outcome of their document) *_REJECTED -> the SUBMITTER "Submitted for approval" is deliberately NOT "created": a draft sitting in the database notifies no one;
- `payments/notification_events.py:40` — Bank Deposits live inside the payments app (payments.BankDeposit), so their event names are owned here too. Same rule as above: the framework only validates the NAME FORMAT — it never learns what a "deposit" is.
- `payments/notification_events.py:56` — **`_open_request_for`** — Filters by the exact (content_type, object_id) of THIS document, so a recipient is never resolved from some other document's workflow.
- `payments/notification_events.py:74` — **`_current_level_recipients`** — Recipient resolution stays in approvals (``current_level_approvers``, the single source of truth); this helper only removes the submitter and collapses duplicates.
- `payments/notification_events.py:74` — **`_current_level_recipients`** — This is the Orders-parity rule: only the stage that now owns the document is notified — never every level at once.
- `payments/notification_events.py:185` — **`publish_receipt_decision`** — Never raises for a delivery problem — the framework isolates that.
- `payments/notification_events.py:228` — **`publish_deposit_decision`** — Never raises for a delivery problem — the framework isolates that.
- `payments/notification_events.py:228` — **`publish_deposit_decision`** — The only differences are the event names, the wording and the entity — the framework and its guarantees are identical (generic ``entity`` = the BankDeposit, no deposit FK in the framework; company derived per-recipient because ``BankDeposit.company`` is a SAP category string, not a ``users.Company`` FK).

### `payments/permissions.py`

- `payments/permissions.py:0` — Hiding a button in the client is a usability affordance, not a security boundary — every one of these checks must hold even when the request does not come from our own UI.
- `payments/permissions.py:0` — These four keys extend it from "which pages can you open" to "which actions can you take": Payments_Create raise a payment receipt Payments_Approve approve / reject a payment receipt Deposit_Create raise a bank deposit Deposit_Approve approve / reject a bank deposit They are deliberately stored in the SAME `extra_pages` list rather than a new model: the grant UI, the login payload, the mobile clie
- `payments/permissions.py:32` — Read-only access to the analytics dashboard — every receipt and deposit in the company, in aggregate. Deliberately SEPARATE from the four action keys above: the people who record and approve payments are not automatically the people who should see company-wide collection totals, and a manager who should see the totals has no business raising a receipt. Neither implies the other.
- `payments/permissions.py:47` — Human labels, surfaced by the /api/payments/my-permissions/ endpoint so the clients never hardcode their own copy of this wording.
- `payments/permissions.py:58` — **`is_admin`** — Same three-way rule as approvals/permissions.py:is_admin — kept identical so "admin" cannot mean one thing in one module and something else in another.
- `payments/permissions.py:72` — NOTE: there is deliberately no role -> permission map. A role is IDENTITY ("this account works in payments"); it grants nothing. All authority comes from two places, and only these two: 1. `extra_pages` — the boxes an admin ticks: which pages the user opens and which actions they may take. 2. Workflow assignment — whether they are an approver at a level, which `approvals` resolves per document. On
- `payments/permissions.py:92` — **`granted_keys`** — Granting from the role as well meant a user with two boxes ticked was silently given all four — the admin's choice was overwritten by the role, which is the opposite of what the Permissions page appears to promise.

### `payments/receipt_invoices.py`

- `payments/receipt_invoices.py:0` — * **Nothing is invented.** A field OMS cannot establish is returned as None and the PDF omits it, while still showing the invoice number and amount applied.
- `payments/receipt_invoices.py:0` — Both values come from ONE batched SAP read for the whole receipt, never one query per allocation.
- `payments/receipt_invoices.py:0` — Kept out of ``receipt_pdf`` on purpose.
- `payments/receipt_invoices.py:0` — That module renders; deciding where a value comes from and what to do when SAP cannot answer is not rendering.
- `payments/receipt_invoices.py:0` — Two rules the whole module exists to honour: * **Invoice Total is never derived from Amount Applied.** They answer different questions and are routinely different — a partial payment against a ₹50,000 invoice applies ₹30,000 and the receipt must say so.
- `payments/receipt_invoices.py:66` — Invoice NUMBER: SAP's DocNum is what a person reads on the invoice. Falls back to the OMS snapshot, then to DocEntry — never to the payment's own DocEntry, which is a different document entirely.

### `payments/receipt_pdf.py`

- `payments/receipt_pdf.py:0` — * The database is never mutated.
- `payments/receipt_pdf.py:0` — Guarantees: * No SAP call is made to render the PDF.
- `payments/receipt_pdf.py:0` — It is deliberately NOT the SAP-rendered Crystal Report file — the real Crystal Reporting-Service integration is a separate, future phase (Option B).
- `payments/receipt_pdf.py:152` — **`_fetch_sap_document`** — Best-effort ONLY: a SAP outage / 404 / any error must NOT break the receipt.
- `payments/receipt_pdf.py:170` — Resolve the SAP company DB from the ACTIVE company mapping table (SapCompanyMap), NOT from receipt.company_db — that column can hold a stale name from when the receipt was created (e.g. an old TEST_OIL_15122025 before the DB was renamed). The mapping is the single source of truth the admin maintains on the Masters page.
- `payments/receipt_pdf.py:309` — SAP IS THE SOURCE OF TRUTH. The live SAP payment document (fetched by the stored sap_doc_entry) drives EVERY printed field — customer, address, branch, document number, posting date, currency, amount, payment method, invoices settled and remarks — so the receipt always shows exactly what SAP holds, even if OMS's stored copy has since drifted. OMS-stored values are used ONLY as a fallback when SAP 
- `payments/receipt_pdf.py:342` — ---- Bill To + Information (two columns) -------------------------- Customer NAME comes from SAP's ORCT CardName WHEN the payment is customer-type (DocType 'rCustomer') — there CardName is the real customer. For an account-type receipt (DocType 'rAccount') CardName is the BANK account, not the customer, so we use the OMS-captured customer name there. Customer CODE always comes from SAP's ORCT Card
- `payments/receipt_pdf.py:426` — The INVOICE's own total — never the amount applied.
- `payments/receipt_pdf.py:459` — ---- Payment (single tender) — the tender SAP recorded -------------- SAP names the tender it posted (Cash / Transfer / Cheque) with SAP's figure. Cash-denomination breakdown is an OMS-only detail shown only when SAP also records a cash tender, so it can never contradict SAP.
- `payments/receipt_pdf.py:507` — ---- Remarks — SAP's ORCT.Remarks (source of truth) ----------------- Fall back to the OMS-stored remark only when SAP is unreachable.

### `payments/sap_client.py`

- `payments/sap_client.py:0` — Modelled on einvoice/sap.py — the cleanest SAP client in the project: module level typed functions, _base()/_verify()/_timeout() helpers so a call can never forget its timeout, one typed exception, truncated error bodies, and `raise ...
- `payments/sap_client.py:0` — This module talks to three company DBs, so a cached OIL session would be used for a BEVERAGES post — silently crediting the wrong company.
- `payments/sap_client.py:0` — Two bugs in serviceLayer/service.py are deliberately NOT reproduced: * its session cache key is the global 'b1_session' with no company DB in it (service.py:15).
- `payments/sap_client.py:116` — **`_parse_error`** — The body is not always JSON (an HTML 502 from a proxy, for instance), so this never assumes it is — serviceLayer/views.py:62 calls .json() unguarded and turns a clean 4xx into an opaque 500.
- `payments/sap_client.py:136` — **`request`** — A retry AFTER a timeout is a different matter and is deliberately NOT done here — see sap_poster.post_document.
- `payments/sap_client.py:150` — The exact JSON about to go over the wire, UNMASKED. The call log deliberately masks cheque and bank detail, which makes it impossible to tell from the log alone whether a "***" was stored or sent. This answers that question directly. Gated on DEBUG level so it is silent in normal operation and never writes customer bank data to a production log by default — enable with LOGGING for 'payments.sap_cl
- `payments/sap_client.py:193` — **`fetch_document`** — Any other failure raises — a caller must never read "could not check" as "not there".
- `payments/sap_client.py:219` — **`post_deposit`** — Not /Deposits (ODPS): the company has never used that object — 0 rows in all three live databases — and every real deposit is an account-type Incoming Payment.

### `payments/sap_payloads.py`

- `payments/sap_payloads.py:10` — Tenders that reach SAP as a bank transfer (TransferSum / TransferAccount). CHEQUE is here on the strength of real SAP records, not convenience. All 66 customer cheque receipts in JIVO_BEVERAGES_HANADB post with TrsfrAcct/TrsfrSum and leave CheckAcct NULL and CheckSum 0; RCT1 (SAP's cheque-lines table) has zero rows in every company database. The earlier PaymentChecks attempt failed with "-2028 No 
- `payments/sap_payloads.py:27` — **`money`** — Quantised BEFORE conversion so the float can never carry more precision than the amount actually has.
- `payments/sap_payloads.py:40` — **`build_cheque_remarks`** — When the result exceeds SAP's 254 characters the user's text is clipped and the OMS identifier is always kept whole — losing the receipt number would break the reconciliation this format exists for.
- `payments/sap_payloads.py:78` — **`build_incoming_payment`** — `U_OMS_REF` / `U_OMS_IDEM` were removed because they do not exist on ORCT, and SAP rejects a payload carrying any property it does not recognise.
- `payments/sap_payloads.py:78` — **`build_incoming_payment`** — `series` is the numbering series for the POSTING month, resolved from NNM1 by the caller (hana_queries.fetch_incoming_payment_series); omitted when not supplied so nothing changes for callers that do not pass it.
- `payments/sap_payloads.py:116` — Numbering series for the posting month. Company-specific: August 2026 is 2514 in BEVERAGES and 2564 in TEST_OIL, so it is never hardcoded.
- `payments/sap_payloads.py:133` — SAP has ONE TransferAccount/TransferSum pair per document, so a receipt may carry only one transfer-type method. The serializer enforces one method per receipt; this guard is the backstop for any path that bypasses it (a shell, a data fix, a future caller). It raises instead of silently picking the first account. That silent pick is what produced DocEntry 20802: ₹12,00,000 of cheque money posted t
- `payments/sap_payloads.py:196` — **`build_deposit`** — A cheque in the same deposit is deliberately excluded: it debited the bank when its RECEIPT posted, so re-posting it here would double-debit.
- `payments/sap_payloads.py:196` — **`build_deposit`** — The company has never used the Deposit object — ODPS holds 0 rows in all three live databases — while account-type Incoming Payments are how every real deposit is recorded.

### `payments/sap_poster.py`

- `payments/sap_poster.py:0` — Guessing either way risks a duplicate payment or a lost one, so the document goes to SAP_UNKNOWN and resubmission is blocked until `reconcile_unknown()` — or a human — establishes what really happened.
- `payments/sap_poster.py:0` — If SAP never replies (timeout, dropped connection), the document may or may not have been committed there.
- `payments/sap_poster.py:0` — THE ONE CASE THAT CANNOT BE ANSWERED SYNCHRONOUSLY.
- `payments/sap_poster.py:0` — The guarantee rests on a single check: the document is reloaded under `select_for_update()` immediately before the call and refused if it already carries a `sap_doc_entry` or is already POSTED.
- `payments/sap_poster.py:35` — **`_history`** — The old receipt-only guard was a limitation of the dropped `payment_sap_posting_history` table, whose foreign key pointed at PaymentReceipt — deposit posts were silently discarded here.
- `payments/sap_poster.py:35` — **`_history`** — Wrapped defensively: an audit-trail failure must never abort a real SAP post that already succeeded.
- `payments/sap_poster.py:56` — **`_reopen_approval`** — Wrapped defensively for the same reason as `_history`: the SAP outcome has already been recorded on the document, and failing to reopen must not lose that or raise into the caller.
- `payments/sap_poster.py:80` — **`_sap_keys`** — It may be absent — the Service Layer does not always echo it — so callers must treat None as "not reported", not as a failure.
- `payments/sap_poster.py:107` — SAP's own messages are written for a consultant reading a trace, not for the person who has to fix the document. "Posting period locked; specify an alternative date" does not say WHICH date is wrong, or what to do about it. Each entry adds a plain-language explanation and the action that clears it. SAP's exact words are ALWAYS kept underneath — support needs the original, and a paraphrase that dri
- `payments/sap_poster.py:130` — -2028 is SAP's GENERIC "No matching records found". It does NOT mean the business partner is missing -- naming the BP here sent people hunting for a party that was present and active all along. The commonest cause by far is a CHEQUE line whose bank has no House Bank Account defined (Banking > Bank Statements and Reconciliations > House Bank Accounts): SAP cannot resolve where the cheque is deposit
- `payments/sap_poster.py:163` — Unknown code — SAP's words are all there is, so do not dress them up.
- `payments/sap_poster.py:192` — **`post_document`** — Never raises for a SAP-side failure — the failure IS the result, and the caller shows `document.sap_response` to the user.
- `payments/sap_poster.py:201` — --- duplicate guard, under a row lock ---------------------------------
- `payments/sap_poster.py:209` — Visible while the call is in flight, so a second request cannot start another post and the UI can show "Posting to SAP...".
- `payments/sap_poster.py:226` — No HTTP status means the request never completed: SAP may still have committed it. That is NOT the same as SAP rejecting the document.
- `payments/sap_poster.py:230` — SAP's own words, kept apart from the message we compose for the person holding the document. A SAP administrator needs the literal string to search their notes and logs against; handing them a rewritten one sends them looking for text SAP never produced.
- `payments/sap_poster.py:263` — SAP ANSWERED "no", so nothing was committed there and the final approval did not really stand. Put it back in front of the last approver, who is the one who can correct and retry. Only for a clean rejection — an ambiguous timeout must NOT reopen anything, because the document may exist in SAP and a second approval could post it twice.
- `payments/sap_poster.py:286` — 2xx with no usable key. The document probably EXISTS in SAP, so this must not be treated as a plain failure the creator can resubmit.
- `payments/sap_poster.py:311` — The Service Layer does NOT expose TransId — confirmed against a real posted document: absent from both the POST response and a later GET, while ORCT held it. So fall back to reading the table directly. Best-effort: the payment has already succeeded, and a failed trace lookup must never turn a posted document into an error. Applies to deposits too: they are account-type Incoming Payments and land i
- `payments/sap_poster.py:371` — **`reconcile_unknown`** — It never POSTs — it only reads.
- `payments/sap_poster.py:371` — **`reconcile_unknown`** — The only safety net kept from the old worker, and it exists solely for the case a synchronous call cannot answer.
- `payments/sap_poster.py:408` — No DocEntry to check against. SAP holds no OMS reference field, so this cannot be resolved automatically — a human must look.

### `payments/sap_reconciliation.py`

- `payments/sap_reconciliation.py:0` — * Re-running is idempotent: a document already CANCELLED_IN_SAP is skipped, so no duplicate history rows accumulate.
- `payments/sap_reconciliation.py:0` — * SAP identifiers are never cleared.
- `payments/sap_reconciliation.py:0` — * `sap_response` is never overwritten.
- `payments/sap_reconciliation.py:0` — It gets its own status, CANCELLED_IN_SAP, and never becomes PENDING_ERROR — the post did succeed.
- `payments/sap_reconciliation.py:0` — SAP keeps the original ORCT row, sets `Canceled = 'Y'` and writes a reversing journal entry — it never deletes.
- `payments/sap_reconciliation.py:39` — Composed for the person holding the document. Deliberately states that the posting succeeded, so this is not mistaken for a posting failure.
- `payments/sap_reconciliation.py:66` — Only a POSTED document can be cancelled in SAP. Anything else moved on under us and must not be overwritten.
- `payments/sap_reconciliation.py:74` — sap_response, sap_doc_entry, sap_doc_num and sap_trans_id are deliberately NOT touched.
- `payments/sap_reconciliation.py:106` — **`reconcile_document`** — Uses the document's own server-side `sap_doc_entry` and `company` — never anything supplied by a caller — so this cannot be pointed at a different SAP database or document.
- `payments/sap_reconciliation.py:123` — Unreadable: SAP down, network, or the row is genuinely absent. Never a reason to downgrade a posted document.
- `payments/sap_reconciliation.py:168` — One bad document must not stop the sweep.

### `payments/serializers.py`

- `payments/serializers.py:43` — **`SapCompanyMapSerializer`** — It is a second field rather than a reuse of `cash_gl_account` because SAP validates the two roles differently: a receipt's CashAccount must be a cash-flow account (OACT.Finanse='Y'), a deposit's CardCode must not be.
- `payments/serializers.py:110` — **`_generate_code`** — The numeric suffix rises until it is free, so two people with the same name never collide on the unique constraint.
- `payments/serializers.py:168` — Never let a SAP outage break reading a payment.
- `payments/serializers.py:181` — OPTIONAL. A UTR is not always to hand when the receipt is raised, so it is not demanded — but when one IS given it is cleaned and checked, because a malformed reference is worse than none: it looks reconcilable and is not.
- `payments/serializers.py:195` — Store trimmed either way, so a stray space can never make two records of the same UTR look different.
- `payments/serializers.py:200` — The CUSTOMER's bank, as printed on the cheque — not one of ours, so it is deliberately NOT validated against our accounts. It is sent to SAP verbatim as BankCode. Uppercased here as well as in the UI so the stored value is consistent whatever the client.
- `payments/serializers.py:213` — The cash breakdown must be present AND equal the cash amount. This mirrors the rule enforced in the mobile UI. It is REQUIRED, not optional: the breakdown is the count of the notes physically handed over, and a cash receipt without one cannot be reconciled against what the collector is carrying. Previously the whole block was skipped when the list was empty, so a cash receipt could be created for 
- `payments/serializers.py:278` — The branch this receipt WILL post to, and where that came from, so the UI can show it before posting and label it correctly: an invoice payment inherits it and must not be editable, an advance is the user's choice.
- `payments/serializers.py:282` — Who raised this entry. Stored since day one but never exposed, so no client could show it — the list and detail screens both need it.
- `payments/serializers.py:312` — **`get_sap_branch`** — {'bpl_id', 'name', 'source', 'editable'} — never raises.
- `payments/serializers.py:326` — SAP being unreachable must not break reading a payment, but a coding error here would otherwise hide silently — which is exactly what a bare `rows = []` did during development. The message is truncated: a HANA connect failure carries a long multi-line RTE dump that adds no signal beyond "SAP is down".
- `payments/serializers.py:390` — **`validate`** — Reading `attrs` alone would treat an omitted `is_advance` as False and reject an edit that never touched it.
- `payments/serializers.py:425` — ONE method per receipt — the rule Finance actually follows: a customer paying by three tenders becomes three Incoming Payments in SAP, not one document carrying all three. It is also the only structural fix for a real misposting. SAP has a SINGLE TransferAccount/TransferSum pair, so a receipt mixing UPI and CHEQUE had to merge them: DocEntry 20802 sent ₹12,00,000 of cheque money to the UPI bank G/
- `payments/serializers.py:506` — Derived, never taken from the payload. orders/views.py:2530 trusts the request for total_amount, so a partial write leaves the header disagreeing with SUM(children).
- `payments/serializers.py:545` — **`update`** — Children are REPLACED, not merged: the client sends the complete set it wants, and diffing method rows by index would silently re-map a cheque's details onto a cash line when the user deletes one in the middle.
- `payments/serializers.py:545` — **`update`** — Deliberately NOT editable: `receipt_no` (issued once, quoted elsewhere), `company` (it decides the SAP database and the party's identity — a change there means a different document), `status`, and every SAP field.
- `payments/serializers.py:791` — **`validate`** — Reading `attrs` alone would treat an omitted field as absent and reject an edit that never touched it.
- `payments/serializers.py:825` — Banked already? The answer lives in BankDepositLine, which is the only written link and holds the unique constraint on `receipt` that makes double-banking impossible at the database level. A receipt already on THIS deposit is excluded: on an edit it is not a double-banking, it is the row being kept.
- `payments/serializers.py:851` — Resolve the chosen bank against SAP and snapshot it. The user picks a bank; the G/L is never typed. Verified here so a bank removed from SAP is caught at entry rather than at posting.
- `payments/serializers.py:907` — **`update`** — The deposit NUMBER never changes — it is the same physical hand-over, and a new number would break the link to whatever has already referenced it.

### `payments/services.py`

- `payments/services.py:0` — `orders/views.py` has zero transaction.atomic across 4,537 lines — this module must not repeat that.
- `payments/services.py:60` — The old SapPostingHistory.Action values, mapped onto the single timeline. Kept as an explicit map so the call sites in sap_poster.py — which are SAP posting logic and are deliberately left untouched — keep passing the strings they always have.
- `payments/services.py:74` — **`record_sap_history`** — Deposits are recorded too (the old table had a FK to PaymentReceipt, so deposit posts were silently dropped), and a SAP attempt now interleaves with the approval decisions that led to it in one ordered list instead of sitting in a separate table.
- `payments/services.py:74` — **`record_sap_history`** — The signature is unchanged so every call site in `sap_poster.py` — SAP posting logic, deliberately not modified — keeps working as-is.
- `payments/services.py:109` — **`resolve_company_db`** — Replaces resolve_company_db_for_order (sync_service.py:301), which reads ITEM categories (a payment has none) and silently routes MART to OIL.
- `payments/services.py:127` — **`resolve_bpl_id`** — SAP refuses a payment whose branch differs from the invoice being paid — "Ensure selected branch is the same as the branch of documents to be paid" — so the branch is a property of the INVOICE, never a fixed setting.
- `payments/services.py:127` — **`resolve_bpl_id`** — SapCompanyMap.default_bpl_id, ONLY for an advance, which settles no invoice and so has no branch to inherit Raises ValidationError rather than guessing when invoices disagree, when a branch cannot be mapped, or when SAP cannot be reached: posting to the wrong ledger is worse than not posting.
- `payments/services.py:161` — Legacy rows only — raised before this field existed. A new advance cannot be submitted without a branch (see validate_receipt).
- `payments/services.py:278` — An advance inherits no branch from an invoice, so one must be chosen. Enforced at submit rather than only in the form: the API is reachable without it, and a missing branch is only discovered by SAP otherwise.
- `payments/services.py:289` — **`_validate_gl_accounts`** — Also catches a mapping whose account has since been removed from SAP, so a payment can never post to a ledger that no longer exists.
- `payments/services.py:367` — Only a resubmission after a SAP failure belongs in the SAP history — a first submission has not involved SAP at all, and logging it there would imply an attempt that never happened.
- `payments/services.py:386` — **`_bank_accounts_for`** — CASH -> SapCompanyMap.cash_gl_account (a drawer is not a house bank) UPI -> the account mapped for UPI CHEQUE -> the account mapped for cheques; the payer's bank is a separate field on the line and is sent as BankCode Raises BankMasterUnavailable when SAP cannot be reached and nothing is cached, and ValidationError when a mapping points at an account SAP no longer has — posting to a stale ledger i
- `payments/services.py:420` — **`post_receipt_to_sap`** — A SAP failure must not roll the approval back: the approval genuinely happened, and the document simply lands in PENDING_ERROR for the creator to correct.
- `payments/services.py:420` — **`post_receipt_to_sap`** — Called AFTER the approving transaction commits, deliberately.
- `payments/services.py:439` — A cheque posts as a bank transfer, so it needs a collection account. If none is mapped, fail HERE with a message naming the company — never fall back to the cash G/L, which would park the money in a clearing account that no deposit ever empties.
- `payments/services.py:454` — Resolved from NNM1 for the POSTING month — never the cheque date.
- `payments/services.py:470` — Only physically-carried tenders can be in a deposit: CASH and CHEQUE. UPI arrives electronically, so there is nothing to hand over and no deposit to record. One non-depositable line disqualifies the whole receipt — a receipt is banked or it is not; it cannot be half-banked. Reads PaymentMethodEntry.DEPOSITABLE_METHODS, the same definition the picker uses, so the two can never drift apart.
- `payments/services.py:527` — Re-verify the bank against SAP at the LAST point before the approval chain opens. SAP configuration can change between saving a draft and submitting it, and an unverifiable bank must never enter the workflow.
- `payments/services.py:567` — **`sap_postable_amount`** — Posting it again would debit the bank twice and credit a clearing account that never held it.
- `payments/services.py:629` — Mixed deposit: SAP receives the CASH share only. The OMS record keeps the full physical amount (cash + cheques), which is what the employee actually carried to the bank; the two figures answer different questions and must not be reconciled into one. The G/L being emptied. Same field the RECEIPTS debited (bank_master.cash_gl -> SapCompanyMap.cash_gl_account), so the clearing account provably nets t

### `payments/urls.py`

- `payments/urls.py:92` — Attachment download — permission-checked, never a raw share path.

### `payments/views.py`

- `payments/views.py:0` — A money module must not rely on a default that does not exist.
- `payments/views.py:68` — **`_flag`** — Accepting all three avoids a filter silently doing nothing because of the spelling.
- `payments/views.py:79` — **`user_companies`** — Deriving company access from it also meant a user with no assignments silently saw a different list from one with them.
- `payments/views.py:204` — **`PersonAnalyticsView`** — `kind` is part of the path because a CollectionPerson and a User can share an id and be different people — see analytics_person for why the two identity spaces are deliberately not merged.
- `payments/views.py:238` — **`PartyListView`** — Deliberately NOT scoped by UserPartyAssignment, unlike PartyView in the orders flow (orders/views.py:1984-2023).
- `payments/views.py:287` — Refusing is safer than silently showing every party: the user asked for "parties that owe money" and would have no way to tell the filter had quietly stopped working.
- `payments/views.py:320` — **`OpenInvoiceListView`** — Read live, never mirrored: a balance changes every time anyone takes a payment, and a stale one would let two collectors over-apply against the same invoice.
- `payments/views.py:336` — Authorise the PAIR server-side — never trust that the client walked the cascade to get here. The check is that the party exists in this company, NOT that the user is assigned to it: a collection agent takes money from whoever pays, so an assignment list would block legitimate collections. See PartyListView.
- `payments/views.py:377` — **`SapBranchListView`** — Scoped to the company and to active rows — the shared /api/sap/branches/ endpoint returns all 22 across every company and is AllowAny, so it cannot be used to populate a payment form.
- `payments/views.py:552` — Approver-relative views. `status` alone cannot express these: whether a PENDING_APPROVAL receipt is waiting on THIS user depends on which rung it stopped at, which lives on the approval request.
- `payments/views.py:616` — **`_document_permissions`** — The client cannot derive these: `can_decide` depends on which rung the approval is parked at and whether the user is eligible for it (named approvers narrow a level), and self-approval is forbidden.
- `payments/views.py:616` — **`_document_permissions`** — The logic never depended on anything receipt-specific: it reads the approval chain, the creator, and the status enum that each model carries as `Status`.
- `payments/views.py:645` — Who may still change the figures, and when. THE APPROVER HOLDING IT may always edit. That is the point: SAP rejects a document for reasons only visible at posting time — a locked period, a wrong GL, a bad cheque reference — and the person who has to clear it is the one it is parked with. Making them reject the whole entry, wait for the creator, and re-walk the ladder to fix a date would be pure fr
- `payments/views.py:742` — **`SapReceiptPdfView`** — Security: the client supplies only the OMS receipt id.
- `payments/views.py:742` — **`SapReceiptPdfView`** — The backend loads the receipt from the visible queryset, enforces per-receipt access via ``can_be_viewed_by``, and reads sap_doc_entry/num/trans_id from the DB — a client-supplied SAP DocEntry is never trusted.
- `payments/views.py:757` — Visibility scope first (404 for a receipt the user cannot see at all)…
- `payments/views.py:802` — Submitting is part of raising the document, so it takes the same grant as creating one — otherwise a user could create drafts they can never submit.
- `payments/views.py:861` — Approver-relative views — the same contract the receipts list honours (PaymentReceiptListCreateView). Without this the parameter was accepted and silently IGNORED, so an approver who picked "Pending" on Deposit Tracking got every deposit back, POSTED and PENDING_ERROR included: the filter looked applied and was not. `status` alone cannot express these: whether a PENDING_APPROVAL deposit is waiting
- `payments/views.py:1003` — When EDITING a deposit, its own receipts must still be offered — they are banked, but banked HERE. Without this the edit form loads with an empty picker and the selection it was prefilled with cannot be seen, changed, or totalled.
- `payments/views.py:1152` — **`MyPaymentPermissionsView`** — The clients need this to hide buttons they must not offer.

## `sap_sync` — 22 statements


### `sap_sync/services/sync_service.py`

- `sap_sync/services/sync_service.py:25` — **`DuplicateCustomerReference`** — duplicated customer/vendor reference number".
- `sap_sync/services/sync_service.py:186` — **`_resolve_combo_parent_item_code`** — Gated on the "+" in the stored line name so an order without a combo never queries the mapping table: this runs per line on every SAP post, and map_order_to_sap is otherwise pure for orders that carry no combo.
- `sap_sync/services/sync_service.py:186` — **`_resolve_combo_parent_item_code`** — Quantity and price are deliberately untouched: the customer agreed the combo's rate, so the parent line carries it and the SAP document total still matches the order.
- `sap_sync/services/sync_service.py:281` — **`_get_order_item_scheme_entries`** — When it is present the caller ships exactly that item and never re-resolves from the scheme tables — otherwise editing a scheme would change what an already-approved order sends to SAP.
- `sap_sync/services/sync_service.py:343` — **`serialized_sync`** — Before the tables had unique constraints, two overlapping runs quietly inserted duplicate rows — that is how ~2952 duplicates accumulated in sap_party_addresses.
- `sap_sync/services/sync_service.py:343` — **`serialized_sync`** — Now the same race raises IntegrityError inside _bulk_upsert's atomic block and rolls the entire sync back, so the second run has to be turned away at the door rather than left to collide.
- `sap_sync/services/sync_service.py:403` — **`_sync_lock`** — Behind a transaction-pooling proxy such as pgbouncer, session advisory locks do not behave as expected.
- `sap_sync/services/sync_service.py:557` — Company 3 (Mart / distributor) is a separate SAP company. Route it to the Mart CompanyDB based purely on the order's company code, before any item-category logic — a company-3 order always books into Mart.
- `sap_sync/services/sync_service.py:712` — Collapse to one payload per (item_code, category) so duplicate source rows don't turn into conflicting writes.
- `sap_sync/services/sync_service.py:792` — Collapse to one payload per (card_code, category) so duplicate source rows don't turn into conflicting writes.
- `sap_sync/services/sync_service.py:870` — Collapse the SAP rows into one payload per unique key first, so duplicates in the source don't turn into conflicting writes.
- `sap_sync/services/sync_service.py:1100` — **`_resolve_costing_code`** — Never raises -- an unresolvable name must not fail the whole mapping.
- `sap_sync/services/sync_service.py:1131` — Mart lines don't resolve a profit center per sub_group (its OPRC has only the default General Center); use a single configured code, or none.
- `sap_sync/services/sync_service.py:1174` — A mapped combo reaches SAP as the product it actually is; the combo code stays on the OMS order and never leaves it.
- `sap_sync/services/sync_service.py:1183` — Line 1: always the ordered item at its price
- `sap_sync/services/sync_service.py:1199` — Snapshot taken at order creation — authoritative. Editing the scheme afterwards must not change what this order ships.
- `sap_sync/services/sync_service.py:1231` — A scheme whose giveaway IS the ordered item ("buy 3 boxes, get 2 pcs of the same free") is legitimate and must still be sent — as its own zero-price line, so the paid line keeps its price. Skipping it here silently dropped the customer's free stock.
- `sap_sync/services/sync_service.py:1257` — SAP wants the CRD1 Address *name* (a short code, max 50 chars), not the full address text. The order-entry page happens to store the name in ship_to_address/bill_to_address, so sending those worked -- but the Distributor page stores the real street address there, and SAP rejects it with "Value too long in property 'PayToCode'". Resolving from the id is identical for party orders (the stored text a
- `sap_sync/services/sync_service.py:1320` — **`_assert_num_at_card_available`** — Never blocks on a lookup failure: HANA being unreachable must not stop a posting that SAP would have accepted.
- `sap_sync/services/sync_service.py:1320` — **`_assert_num_at_card_available`** — SAP rejects a duplicate with a bare "-5002 ...
- `sap_sync/services/sync_service.py:1320` — **`_assert_num_at_card_available`** — duplicated customer/vendor reference number" that names neither the reference nor the document holding it, which makes the real cause (usually a retry of a submission that actually succeeded) hard to see.
- `sap_sync/services/sync_service.py:1370` — Before the login round trip: a duplicate reference is a certain rejection, and failing here names the document in the way.

## `serviceLayer` — 5 statements


### `serviceLayer/ap_views.py`

- `serviceLayer/ap_views.py:256` — **`APInvoiceCreateView`** — POST /api/service-layer/ap/invoice/?branch=OIL Body (friendly fields; the SAP payload is built here, never passed through): { "grpo_entry": 25773, # required "num_at_card": "INV/2026/01", # required (vendor's invoice no) "doc_date": "2026-08-25", # optional (yyyy-mm-dd) "due_date": "2026-09-14", # optional "comments": "…", # optional "attachment_entry": 170747, # optional (e.g.

### `serviceLayer/service.py`

- `serviceLayer/service.py:12` — **`schema_for`** — Accepts both 'BEVERAGE' and 'BEVERAGES' (both spellings are used across the codebase); anything else -> OIL, so a missing/unknown branch can never leave the schema undefined.
- `serviceLayer/service.py:42` — **`_cache_keys`** — A single shared key would hand a cached OIL session back to a BEVERAGE caller, silently reading/writing the wrong company.

### `serviceLayer/views.py`

- `serviceLayer/views.py:13` — **`_maybe_auto_irn`** — Fire fire-and-forget auto IRN generation, with clear logging of why it was skipped (so a missing IRN is never silent).
- `serviceLayer/views.py:56` — Fire-and-forget auto IRN generation. This view only ever posts real invoices (drafts go through DraftView), so it always applies. The IRN MUST be generated against — and mirrored into — the same company DB the invoice was created in, so resolve it from `branch`.

## `tracker` — 39 statements


### `tracker/admin_views.py`

- `tracker/admin_views.py:171` — --------------------------------------------------------------------------- Tracker user management (create / list / delete) — TRACKER users only. A tracker admin can only ever touch users who hold a tracker sub-role, never other OMS users. ---------------------------------------------------------------------------

### `tracker/jsap.py`

- `tracker/jsap.py:0` — JSAP approves a SAP *draft* document against a budget before it is posted, so everything here is keyed on an ODRF DocEntry — never an OPCH one: tracker Invoice -> ODRF (TRIM(NumAtCard) + CardCode, in the invoice's company DB) | ODRF.DocEntry v bud.jsDocEntry one row per (draft, approval template); status A/P/R | id v bud.jsBudgetStatusWorkflow per-action log; `description` holds the approver's rea
- `tracker/jsap.py:45` — jsBudgetTable.Branch -> company DB. Mart is deliberately absent: it has no Branch value in JSAP, so Mart invoices are never budget-approved there.

### `tracker/management/commands/email_stuck_alerts.py`

- `tracker/management/commands/email_stuck_alerts.py:0` — Idempotent — safe to schedule every N minutes (mirrors scan_stuck_alerts / the auto-IRN sweep).
- `tracker/management/commands/email_stuck_alerts.py:0` — Rules: * Pre-audit FULL holds are skipped — a full hold parks the invoice on purpose, so it should not raise a "stuck" email.

### `tracker/management/commands/scan_stuck_alerts.py`

- `tracker/management/commands/scan_stuck_alerts.py:0` — Idempotent — safe to run every N minutes from Task Scheduler (mirrors the auto-IRN sweep pattern).

### `tracker/management/commands/seed_tracker.py`

- `tracker/management/commands/seed_tracker.py:0` — Idempotent — safe to run repeatedly.
- `tracker/management/commands/seed_tracker.py:17` — "Head Office In", not "Invoice Entry": this is what the stage has been called on the live database since it was renamed in the UI, and what the export workbook's first column is built around (see exports.py). The seed carries the full defaults dict, so leaving the old name here meant any re-run silently renamed the stage back. Aligned 2026-08-26.

### `tracker/models.py`

- `tracker/models.py:47` — Transport, RM-PM, Contractor, Fixed Asset, Security, Other Invoice/Consumable, Cash Voucher, Staff Imprest, Refreshment, E-COM
- `tracker/models.py:167` — **`InvoiceManager`** — Default manager — hides soft-deleted invoices from every read path (queue, reports, alerts, scoped lists) so callers never see them.
- `tracker/models.py:194` — NOT globally unique — unique per vendor, over live rows only, via the partial index in Meta. Invoice numbers are a per-supplier series, so two vendors both issuing "058" is normal and must be allowed.
- `tracker/models.py:206` — Always derived on save: taxable + GST amount + additional charge.
- `tracker/models.py:267` — One LIVE invoice per (vendor, number). Scoped to the vendor because invoice numbers are only unique within a vendor's own series — two suppliers legitimately both issue an "058". The vendor key is party_code (SAP CardCode) when present, falling back to party_name for hand-typed vendors that aren't in SAP; without the fallback every code-less invoice would share one empty bucket and collide with th
- `tracker/models.py:308` — Invoice value is always derived: taxable + GST amount + additional charge.
- `tracker/models.py:318` — --------------------------------------------------------------------------- Append-only handoff log — the single source of truth for every timeline. ---------------------------------------------------------------------------
- `tracker/models.py:392` — User enters the percentages; the amounts below are always derived server-side. discount% is applied to the NET invoice value (invoice_value - debit); tds% is applied to the taxable value (before GST / additional charges).

### `tracker/permissions.py`

- `tracker/permissions.py:0` — (Corrected 2026-08-26; this paragraph previously claimed the opposite, which `tracker_pages_for` below has never done.)
- `tracker/permissions.py:0` — Every view (and, mirrored, the frontend) reads from it, so to change who sees what you change a user's role, never page code.
- `tracker/permissions.py:35` — The single source of truth: tracker sub-role -> visible pages. Stuck Alerts and the all-invoices list are admin-only.

### `tracker/sap.py`

- `tracker/sap.py:211` — **`resolve_sap_document`** — Searches the invoice's own company DB first; if nothing matches there the other companies are tried, since the unit/branch on the tracker row is not always the company the document was booked in.

### `tracker/serializers.py`

- `tracker/serializers.py:84` — Amounts/balance/status are always derived server-side from the percentages, the hold-release flag and the paid amount; only those inputs are writable.
- `tracker/serializers.py:95` — **`InvoiceWriteSerializer`** — `invoice_value` is NOT accepted — it is always derived on the server as taxable + GST amount + additional charge.
- `tracker/serializers.py:116` — **`validate`** — Duplicate check, scoped to the vendor.
- `tracker/serializers.py:116` — **`validate`** — Mirrors the `uniq_live_vendor_invoice_number` partial index exactly: * LIVE rows only — a soft-deleted invoice releases its number (deletion is capped at stage 6, so it was never saved in SAP or paid, and re-entry is how a bad entry gets corrected); * vendor key = party_code, falling back to party_name when the vendor was hand-typed rather than picked from SAP; * case-insensitive on both parts.
- `tracker/serializers.py:143` — Same fallback the index uses, so the two can never disagree.

### `tracker/services.py`

- `tracker/services.py:35` — Category names (normalised) that don't require a hold amount on a partial hold.
- `tracker/services.py:53` — **`can_act`** — `user=None` means the system itself is acting (the JSAP sync mirroring a decision made in JSAP), which is always allowed — there is no person to map to a stage, and the event is logged with acted_by=NULL.
- `tracker/services.py:71` — **`stage_recipients`** — Only the three tracker sub-roles (tracker_admin / tracker_entry / tracker_user) are mailed — never plain OMS users or superusers.
- `tracker/services.py:92` — **`is_full_hold`** — A full hold keeps the invoice in place (partial holds advance it), so the presence of a FULL hold event on this visit means it's parked on purpose.
- `tracker/services.py:275` — Reason enforcement: always on RETURN, plus any reason-required status — EXCEPT a pending rejection, which is allowed precisely because it has no remarks yet (they come later, when it is returned).
- `tracker/services.py:403` — **`compute_payment`** — * total_owed — the full obligation, which ALWAYS includes the hold (it is part of net invoice value).
- `tracker/services.py:403` — **`compute_payment`** — The open balance is measured against this, so a withheld hold keeps the invoice open (balance never drops below the hold) until it is released and paid.
- `tracker/services.py:531` — A handler rejected this by hand and is still writing the reason. Their decision outranks the mirror — otherwise the next sweep could quietly advance an invoice someone had deliberately parked.
- `tracker/services.py:542` — Mart is never budget-approved in JSAP — it would otherwise sit here forever, so let it straight through.
- `tracker/services.py:568` — **`sync_jsap_all`** — One invoice's failure never stops the sweep.

### `tracker/views.py`

- `tracker/views.py:48` — **`JsapStatusView`** — Always 200 — "we could not link this invoice to SAP" is an answer the desk needs to show, not an error.
- `tracker/views.py:312` — Flag invoices that arrived at their current desk via a RETURN (rework), so the UI can split "Current" from "Returned" and surface the reason. A return always closes a visit at the immediately-later stage, so the invoice's latest closed event tells us how it got here.
- `tracker/views.py:462` — A full hold is an in-place note (never exits), so fall back to when the row was written.
- `tracker/views.py:523` — **`StageExportView`** — A decision log can list one invoice twice (debited twice); the register is one row per invoice, so duplicates collapse.

## `uilabels` — 3 statements


### `uilabels/models.py`

- `uilabels/models.py:4` — **`UILabel`** — The table is intentionally generic (one row per field key) so more labels can be added later — `price_list`, `basic_price`, `tax`, `confirm_button`, … — without any backend change beyond a data row.

### `uilabels/views.py`

- `uilabels/views.py:24` — **`PublicLabelsView`** — Kept deliberately tiny and unwrapped for cheap client use.
- `uilabels/views.py:24` — **`PublicLabelsView`** — This is the single source of truth both frontends fetch once after login and cache.

## `users` — 14 statements


### `users/management/commands/create_super_user.py`

- `users/management/commands/create_super_user.py:0` — Idempotent: re-running updates the existing user (and resets the password).

### `users/models.py`

- `users/models.py:56` — Combo packs ("COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS") are a wrapper around two real products: the paid half before the "+" and the free half after it. Both are mapped explicitly on the Combo Mapping page rather than parsed out of the name -- the names are inconsistent enough that guessing gets it wrong. Ordering a mapped combo puts BOTH halves on the order as separate lines (parent pric
- `users/models.py:215` — ADDITIONAL roles, on top of the primary `role` above. `role` is a single FK, so a user who is a Manager cannot also be a Payment Approver without losing "manager" — and with it their access to orders, reports and everything else keyed off that role. This M2M lets a user hold function-specific roles (payment_approver, deposit_creator, …) while keeping the primary role that defines the rest of their
- `users/models.py:264` — All categories assigned to the user. The single `category` FK above is kept as the primary category (always the first selected category) for backward compatibility; `categories` is the full set used for data scoping.
- `users/models.py:316` — -- Role helpers -------------------------------------------------------- A user's roles are the primary FK plus any extra_roles. Every "does this user hold role X" check must go through these, or a grant made via extra_roles would be invisible to half the codebase.

### `users/serializers.py`

- `users/serializers.py:53` — Read-only account flags/timestamps surfaced on the mobile Profile screen. Kept read_only so they can never be set through this serializer.
- `users/serializers.py:192` — M2M — must be popped before create() and set once the row exists.
- `users/serializers.py:288` — Keep the single `category` FK in sync with the first selected one. An empty list must NOT null a `category` that was sent alongside it: this block runs after the field loop above, so doing so silently wiped the category on every update from a client that posts `categories: []`.

### `users/views.py`

- `users/views.py:29` — **`_combo_free_defaults`** — Only keys the caller actually sent are returned, so existing clients that know nothing about combos never blank an already-configured mapping.
- `users/views.py:282` — Assign within the requested category (one of the user's categories); falls back to the primary. Scoping existing rows to this category means assigning for one category never disturbs parties in another.
- `users/views.py:707` — A "+" in the SAP name is what marks a combo pack, but it also catches bundles that are not 1+1 combos and must stay off the Combo Mapping page: * "... COMBO 10 SET" -- multi-set cartons * "... 3 PCS SHRINKED" -- shrink-wrapped multipacks Matched case-insensitively as substrings. 'SHRINK' rather than 'SHRINKED' on purpose: one item is named "... SHRINKED 1 PCS" and another just "SHRINK".
- `users/views.py:736` — Mapped to something SAP no longer lists — surface the raw code so the page can show it as broken rather than silently as "unmapped".
- `users/views.py:1063` — **`LogoutView`** — POST /api/auth/logout/ — blacklist the refresh token so it cannot be reused (server-side invalidation, not just a client-side clear).
- `users/views.py:1084` — Already expired/invalid/blacklisted — logout is idempotent.

---

**609 statements across 18 apps.**
