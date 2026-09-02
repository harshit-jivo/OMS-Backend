# OMS-Backend — codebase reference and phased refactor plan

**Written 2026-08-26.** Two jobs in one document:

1. **Part I–II — what exists.** A complete inventory of the backend, written so
   that a refactor can be checked against it. If a capability is not listed here
   and disappears during the refactor, that is a gap in this document; if it *is*
   listed and disappears, that is a regression.
2. **Part III–V — what to change.** An assessment of the current state and a
   phased plan, ordered so that each phase leaves the system working.

Everything below was verified against the code and, where stated, against the
live database `order_management` on `138.252.101.117`. Nothing here is inferred
from naming alone.

> **Read §7.1 first.** There is an unauthenticated privilege-escalation hole in
> the live API. It is the one item in this document that should not wait for a
> phase plan.

---

# Part I — What the system is

## 1. Overview

OMS is the order-management backend for Jivo, and it is much more than orders. It
is the integration hub between a React web client, a React Native mobile client,
and **SAP Business One** — plus the Indian statutory e-invoicing system (NIC IRN
and e-Way Bill), a Crystal Reports print service, a JSAP budget system, and a
push-notification stack.

| | |
|---|---|
| Framework | Django 5.2.10, Django REST Framework 3.16.1 |
| Python deps | ~120 pinned in `requirements.txt` |
| Auth | JWT via `djangorestframework-simplejwt` 5.5.1 |
| Primary DB | PostgreSQL 16.15, `order_management` @ `138.252.101.117` |
| Secondary DB | SAP HANA (read-only, multi-company), JSAP (MSSQL) |
| Scheduler | APScheduler via `django-apscheduler` |
| Static | WhiteNoise |
| Source size | ~50,600 LOC Python (excl. migrations), 21 local apps |
| Models | 88 across 17 apps |
| API routes | 298 |
| Migrations | 248 on disk, all applied |
| Tests | 420 test functions across 21 files |

### 1.1 The three databases

This is the single most important architectural fact about the system.

```
                    ┌──────────────────────────┐
   React web ──────▶│                          │
   React Native ───▶│      OMS Django API      │
                    │                          │
                    └───┬──────────┬───────────┘
                        │          │
        ┌───────────────┘          └────────────────┐
        ▼                                            ▼
┌─────────────────┐                        ┌──────────────────────┐
│  PostgreSQL     │                        │  SAP HANA (READ)     │
│  order_management│                       │  jivo_oil_hanadb     │
│                 │                        │  jivo_beverages_...  │
│  schemas:       │                        │  jivo_mart_hanadb    │
│   public        │                        └──────────────────────┘
│   payments      │                                    ▲
│   hais          │                        ┌───────────┴──────────┐
└─────────────────┘                        │ SAP Service Layer    │
                                           │ (OData, WRITE)       │
                                           └──────────────────────┘
```

- **PostgreSQL** is OMS's own store. Three schemas: `public` (most things),
  `payments` (the payments module owns 19 tables), `hais` (asset register).
  `search_path` is set to `payments,public` in `DATABASES['default']['OPTIONS']`.
- **SAP HANA** is read-only. Queried directly over `hdbcli` for master data,
  stock, pricing, invoices. **Three separate company databases** — Oil, Beverages,
  Mart — and *the same DocNum/DocEntry means a different document in each*. The
  company is therefore part of every lookup key, never a display label.
- **SAP Service Layer** is the write path (OData). Orders, invoices, payments are
  posted here.

> **Invariant.** Company/branch must travel with every SAP-facing request. Getting
> it wrong does not error — it silently returns or writes against the wrong
> company's data.

---

## 2. Application inventory

Ordered by size. **This table is the "do not lose anything" checklist.**

| app | LOC | models | routes | tests | responsibility |
|---|---:|---:|---:|---:|---|
| `orders` | 11,194 | 25 | 56 | 102 | Orders, quotations, the scheme/discount engine, parties, products, dashboards, push |
| `payments` | 11,098 | 11 | 30 | 201 | Receive Payment, Bank Deposit, cash denominations, SAP posting, receipts |
| `einvoice` | 4,320 | 3 | 21 | 5 | NIC e-Invoice (IRN) generation, QR, cancellation |
| `tracker` | 3,905 | 15 | 24 | 0 | Document tracker — 8-stage vendor-invoice workflow |
| `sap_sync` | 3,734 | 8 | 27 | 14 | Pulls SAP master data into Postgres; sync scheduling |
| `devices` | 2,211 | 2 | 7 | 44 | Device registry, mobile version policy gate |
| `users` | 2,105 | 9 | 26 | **0** | Users, roles, party/product assignment, **login** |
| `hana` | 2,020 | 0 | 23 | 0 | Direct HANA read queries (stock, pricing, costing) |
| `approvals` | 1,699 | 5 | 11 | 0 | Generic multi-level approval engine |
| `invoice` | 1,598 | 4 | 14 | 0 | Invoice history, credit limits, Crystal print proxy |
| `notifications` | 1,588 | 1 | 3 | 54 | Unified notification fan-out |
| `OMS` | 964 | – | 21 | – | Settings, root URLconf, ASGI/WSGI |
| `ewaybill` | 844 | 0 | 12 | 0 | NIC e-Way Bill |
| `serviceLayer` | 729 | 0 | 7 | 0 | SAP Service Layer client/session |
| `HAIS` | 636 | 4 | – | 0 | Hardware asset register (own schema) |
| `audit` | 538 | 1 | – | 0 | Request audit log middleware |
| `attachments` | 417 | 1 | – | 0 | File storage on shared network folders |
| `legal` | 341 | 4 | 8 | 0 | Product label / nutrition data |
| `uilabels` | 316 | 1 | 4 | 0 | Admin-editable UI field labels |
| `core` | 190 | 1 | – | 0 | Shared base model, document-number generator |
| `SKU` | 97 | 1 | 4 | 0 | SKU lookup |

### 2.1 Capability notes worth preserving

Things that are load-bearing and non-obvious. Losing any of these during a
refactor would be a functional regression.

**orders**
- The **scheme engine** (`orders/scheme_engine.py`, 45 dedicated tests) — the
  discount/free-goods rules. Schemes have triggers, benefits and assignments;
  v2 models (`Scheme`, `SchemeTrigger`, `SchemeBenefit`, `SchemeAssignment`)
  coexist with a legacy path. UOM is PCS/BOX only, enforced in migration `0057`.
- **Rate approval** — `OrderRateApproval`, `RateApproverRule`,
  `OrderItemApprovalMapping`. Orders below an approved rate route to an approver.
- **Order flow config** — `OrderFlowConfig` and `PartyOrderFlowConfig` make the
  order lifecycle configurable *per party*.
- **Push** — `PushToken`, `WebPushSubscription`, VAPID web push, plus prune
  commands.
- `orders/views.py` is **5,667 lines**. See §8.1.

**payments**
- Owns its own PostgreSQL schema (19 tables). `SapOutbox`-style posting with
  `SapCallLog` for every SAP call; `reconcile_sap_cancellations` management
  command. Receipt PDF generation. Cash denomination breakdown. The most heavily
  tested app in the codebase (201 tests).

**tracker**
- Self-contained 8-stage document workflow, **no FKs into OMS models** — by
  design, so it can be extracted. Stage config is *data*, not code: names, order,
  thresholds and status choices are admin-editable and logic keys on `Stage.code`.
  **Renaming a stage is safe; changing its code is not.**
- Soft delete everywhere; invoice numbers stay reserved.
- `seed_tracker` management command is idempotent but rewrites all stage rows.
- Stuck-alert scanning + email (`scan_stuck_alerts`, `email_stuck_alerts`).

**einvoice / ewaybill**
- NIC schema 1.1. Token caching, IRN records, QR PNG generation and share to a
  network path, sandbox test harness. `IrnGenerationLog` keeps every attempt.
- The buyer-GSTIN recovery added 2026-08-26 (`normalize_buyer_gstin`) — falls back
  to the BP master addresses when `INV12.BpGSTN` is blank. Documented in
  `docs/NIC_EINVOICE_EWAYBILL_REFERENCE.md` §12.5.

**approvals**
- A *generic* multi-level approval engine (`ApprovalWorkflow` → `ApprovalLevel` →
  `ApprovalLevelApprover` → `ApprovalRequest` → `ApprovalAction`), currently used
  by payments but deliberately document-type agnostic. Reusable — do not couple
  it to payments during a refactor.

**devices**
- `VersionPolicyMiddleware` returns **HTTP 426** to out-of-date mobile clients.
  Keyed off an `X-Platform` header; the web is never gated. Placed after
  `CorsMiddleware` so it cannot break browser preflight.

**uilabels**
- Field labels served to web and mobile at runtime. Changing a label is a data
  edit, not a deploy.

---

## 3. Authentication and authorization — as it stands

### 3.1 Authentication

JWT only. `REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES` is
`JWTAuthentication` and nothing else. Session auth is not used by the API.

`SIMPLE_JWT` is reasonably configured and deliberately documented:
- access 1 day, refresh 7 days
- `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` — replay protection
- `UPDATE_LAST_LOGIN`, `LEEWAY: 10`
- Access lifetime is long **because the clients do not implement a refresh
  flow**; `/auth/refresh/` exists but is not adopted. Shortening it before the
  clients change would log users out mid-session.

### 3.2 Authorization

Role-based, via a single FK:

```python
User.role       -> UserRole      # the primary role; drives nearly all access
User.extra_roles -> UserRole[]   # additional function roles (M2M)
```

`users/models.py` states the rule: *"Anything resolving 'does this user hold role
X' must check BOTH."* Not every consumer obeys it — see §7.4.

Permission classes exist and are genuinely good in the newer apps:

| module | classes |
|---|---|
| `tracker/permissions.py` | `IsTrackerAdmin`, `IsTrackerUser`, `IsTrackerEntry`, `IsTrackerAP`, `IsTrackerReports`, `IsTrackerAlerts`, all driven by one `ROLE_PAGE_MAP` |
| `payments/permissions.py` | `CanViewPaymentsDashboard`, `CanCreatePayment`, `CanCreateDeposit`, `ReadOrCreatePayment`, `ReadOrCreateDeposit` |
| `approvals` | `IsApprovalAdmin` |
| `users` | `IsAdminRole` |

`tracker/permissions.py` is the model to copy: **one `ROLE_PAGE_MAP`, one
`tracker_pages_for(user)`, every view reads from it.** To change who sees what
you change data, not view code.

### 3.3 The actual distribution

Repo-wide tally of `permission_classes` values:

| value | count |
|---|---:|
| `[IsAuthenticated]` | 70 |
| **`[AllowAny]`** | **60** |
| `[IsAuthenticated, IsApprovalAdmin]` | 14 |
| `[IsTrackerUser]` | 10 |
| `[IsTrackerAdmin]` | 10 |
| `[IsAuthenticated, IsAdminRole]` | 6 |
| everything else (payments/tracker fine-grained) | 12 |

**There is no `DEFAULT_PERMISSION_CLASSES`.** DRF's built-in default is
`AllowAny`, so any view that forgets to declare permissions is public.

### 3.4 The measured result

The counts above are per-declaration. Resolving the **actual URLconf** — every
route, with the permission classes that really apply — gives the true figure.
Full table in [`docs/codebase/API_SURFACE.md`](codebase/API_SURFACE.md).

> ## **149 of 294 routes — 51% of the API — require no authentication.**

| app | routes | open | |
|---|---:|---:|---|
| `hana` | 23 | **23** | ████████████ all |
| `einvoice` | 21 | **21** | ████████████ all |
| `ewaybill` | 12 | **12** | ████████████ all |
| `legal` | 8 | **8** | ████████████ all |
| `SKU` | 4 | **4** | ████████████ all |
| `sap_sync` | 27 | **26** | ███████████· |
| `invoice` | 14 | **12** | ██████████·· |
| `orders` | 55 | **24** | █████······· |
| `users` | 26 | **16** | ███████····· |
| `serviceLayer` | 7 | **3** | █████······· |
| `payments` | 29 | 0 | — |
| `tracker` | 24 | 0 | — |
| `HAIS` | 18 | 0 | — |
| `approvals` | 11 | 0 | — |
| `devices` | 7 | 0 | — |
| `uilabels` | 4 | 0 | — |
| `notifications` | 3 | 0 | — |
| `attachments` | 1 | 0 | — |

The split is not random. **Every app with a `permissions.py` module is fully
locked down; every app without one is wide open.** `payments`, `tracker`,
`approvals`, `devices` and `uilabels` each wrote one and have zero open routes
between them. The refactor target already exists in-tree — it just was never
applied to the older apps.

---

## 4. External integrations

| system | direction | transport | where |
|---|---|---|---|
| SAP HANA (3 company DBs) | read | `hdbcli` | `hana/services/connection.py`, `sap_sync/services/` |
| SAP Service Layer | write | OData/HTTPS | `serviceLayer/`, `payments/`, `orders/` |
| NIC e-Invoice | read/write | HTTPS | `einvoice/` |
| NIC e-Way Bill | read/write | HTTPS | `ewaybill/` |
| Crystal Reports | read | HTTP | `CRYSTAL_URL`, `invoice.views.GetPrintReport` |
| JSAP (budget) | read | MSSQL + HTTP | `invoice/services/jsap_db.py`, `DSR_API_BASE`, `tracker/jsap.py` |
| File upload service | write | HTTP :8013 | attachment workaround for Service Layer `-5002` |
| SMTP | write | SMTP | tracker stuck alerts |
| Web Push (VAPID) | write | HTTPS | `orders/webpush.py` |
| FCM/APNs | write | HTTPS | `devices/`, `notifications/` |

**`GetPrintReport`** deserves a note because the frontend merge depended on it:
it maps `OIL`/`BEVERAGE`/`MART` onto Crystal paths `api/billprint`,
`api/billprint/bev`, `api/billprint/mart`, accepts **either** `docNum` (resolving
it against that company's `OINV`) **or** `docEntry`, and produces a sanitised
`'<DocNum> <Party>.pdf'` filename.

---

## 5. Background work

- **APScheduler** (`django-apscheduler`) — in-process scheduling.
- Management commands: `auto_generate_irns`, `backfill_qr_png`,
  `scan_stuck_alerts`, `email_stuck_alerts`, `sync_jsap`, `seed_tracker`,
  `reconcile_sap_cancellations`, `migrate_schemes_v2`, `prune_push_tokens`,
  `prune_web_push_subscriptions`, `generate_vapid_keys`, `create_super_user`.

> In-process APScheduler means jobs run inside a web worker. With more than one
> worker, jobs can run more than once. See §9 Phase 5.

---

## 6. Testing

420 test functions, but concentrated:

| well covered | untested |
|---|---|
| `payments` (201) | **`users` (0)** — including login |
| `notifications` (54) | **`tracker` (0)** — 3,905 LOC, 15 models |
| `orders` scheme engine (45) | `hana`, `invoice`, `approvals`, `legal`, `HAIS`, `uilabels`, `serviceLayer`, `core`, `audit`, `ewaybill` (0) |
| `devices` (44) | `einvoice` (5, thin) |

`OMS/test_settings.py` exists, so there is a test-settings path already.

**The two most security-sensitive apps — `users` and `tracker` — have no tests
at all.** That is the gap that makes the Phase 1 changes below risky to attempt
without Phase 0 first.

---

# Part II — Assessment

## 7. Security

### 7.1 🔴 CRITICAL — unauthenticated privilege escalation

```
POST /api/auth/users/create/
```

`users.views.CreateUserView` declares `permission_classes = [AllowAny]`
([users/views.py:1188-1189](../users/views.py#L1188-L1189)). Its serializer
accepts a `role`:

```python
role = serializers.PrimaryKeyRelatedField(
    queryset=UserRole.objects.all(), required=False, allow_null=True)
```

`queryset=UserRole.objects.all()` — **any** role, including `admin` and
`tracker_admin`. Chained together: **anyone who can reach the API can create
themselves an administrator account without credentials.** The service is
internet-facing (`CSRF_TRUSTED_ORIGINS` lists `https://oms.jivo.in`).

Adjacent, same pattern, also `AllowAny`:

| endpoint | effect |
|---|---|
| `POST /api/auth/users/<id>/delete/` | deactivates any user by ID |
| `GET/PATCH /api/auth/users/<id>/` | reads/edits any user |
| `AssignPartiesView`, `BulkAssignUsersPartiesView`, `RemovePartyAssignmentView` | rewrites who can see which customers |
| `sap_sync.ApproveSalesOrderAPIView`, `ApproveOrderAPIView` | approves orders |
| `SyncAllView`, `SyncProductsView`, `SyncPartiesView` | triggers SAP syncs |
| `CreateSchemeView`, `SchemeDetailView`, `SchemeV2*` | creates/edits discount schemes |
| `DashboardKPIView`, `DashboardChartsView` | business metrics |

**This should be fixed today, ahead of any refactor phase.** The minimal change
is `permission_classes = [IsAuthenticated, IsAdminRole]` on the user-management
views and constraining the `role` queryset. It does not require the plan below.

### 7.2 🔴 Hardcoded secret key, DEBUG forced on

[OMS/settings.py:38-52](../OMS/settings.py#L38-L52):

```python
# SECRET_KEY = config('SECRET_KEY')          # commented out
DEBUG = _parse_bool(config('DEBUG', default='false'), default=False)

SECRET_KEY = 'django-insecure-#im8s6vmxe)=%xl8$ybjl*fu9(+2=5cf^8$=ok8%bx%0f&^t05'
DEBUG = True                                  # overrides the line above
ALLOWED_HOSTS = [..., '*']
```

Three problems on five lines:

1. **`SECRET_KEY` is hardcoded and committed.** It is in git history, so it is
   compromised regardless of what happens next. It signs sessions and — because
   `SIGNING_KEY` defaults to it — **every JWT**. Anyone with the repo can forge a
   valid token for any user.
2. **`DEBUG = True` is set unconditionally**, overriding the correct env-driven
   line 8 lines above. In production this leaks full tracebacks, settings and SQL
   on every error.
3. **`ALLOWED_HOSTS` contains `'*'`**, defeating Host-header validation.

Rotating `SECRET_KEY`/`JWT_SIGNING_KEY` invalidates every issued token, so it
needs a maintenance window — but it must happen.

### 7.3 🟠 CORS wide open

`CORS_ALLOW_ALL_ORIGINS = True` ([settings.py:386](../OMS/settings.py#L386)).
Any website can call the API from a user's browser. The header allowlist below it
is thoughtfully written and commented — the wildcard undoes the benefit.

### 7.4 🟠 `extra_roles` ignored by tracker permissions

`tracker/permissions.py:_role_name()` reads only `user.role`. A user whose
primary role is `manager` and who holds `tracker_admin` in `extra_roles` gets
**no** tracker access. `users/models.py` explicitly says both must be checked.
Either the tracker is a deliberate exception (then say so in code) or it is a bug.

### 7.5 🟠 Hand-escaped SQL against HANA

`hana/services/connection.py` builds 32 f-string SQL blocks with 19 manual
`.replace("'", "''")` escapes. The escaping that exists is correct, but it is
per-call-site and one omission is an injection. `hdbcli` supports `?` parameter
binding; there is no reason to hand-roll this.

### 7.6 🟡 Smaller items

- No throttling (`DEFAULT_THROTTLE_CLASSES` absent) — login is brute-forceable.
- `min_length=6` password policy in `CreateUserSerializer`, weaker than the
  configured `AUTH_PASSWORD_VALIDATORS`, which the serializer bypasses.
- No `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`.
- `.env` holds live DB and SAP credentials; confirm it is not in git history.

---

## 8. Architecture and code health

### 8.1 Monolithic views

| file | LOC |
|---|---:|
| `orders/views.py` | **5,667** |
| `sap_sync/services/sync_service.py` | 1,496 |
| `users/views.py` | 1,267 |
| `payments/views.py` | 1,172 |
| `hana/services/connection.py` | 977 |

`orders/views.py` at 5,667 lines holds 54 view classes and substantial business
logic. Business rules living in views is why the scheme engine had to be pulled
out into `scheme_engine.py` — that extraction is the pattern to repeat.

**`payments` is the reference architecture in this repo**: `views` → `services`
→ `models`, own schema, own permissions module, 201 tests. Refactor targets
should be moved toward what payments already does, not toward something new.

### 8.2 Duplicate model mapping

`orders.Branches` and `sap_sync.Branch` both map to the table `branches`. Both
are `managed=False`, so nothing crashes, but they are two partial views of one
table with different field sets (`sap_sync.Branch` has `is_active`,
`created_at`, `updated_at`; `orders.Branches` does not). One should win.

Similarly `orders.Notification` (`notifications`) and
`notifications.Notification` (`notifications_notification`) are two notification
models. `notifications` is the newer unified one.

### 8.5 A race condition `orders` already knows about

`approvals/services.py` opens by naming a concrete defect in `orders`, as the
reason its own design differs:

> Two things it does that `orders/views.py` does not, and which this module must
> not lose:
> * `@transaction.atomic` on every transition
> * `select_for_update()` on the row being decided
>
> `UpdateOrderStatusView.post` (`orders/views.py:2895`) is neither, across ~380
> lines and several dependent writes — **so two approvers acting at once can both
> pass the pending check and double-advance a document.**

Two approval subsystems now exist side by side: the newer generic engine, which
locks correctly, and the original in-view order flow, which does not. This is
the clearest illustration of why Phase 3 matters — and the fix is not a rewrite,
it is moving the order flow onto the engine that already exists.

`payments/sap_poster.py` shows the same discipline applied to money: the
document is reloaded under `select_for_update()` immediately before the SAP
call, which is what makes "one OMS receipt → one SAP payment" hold. It also
distinguishes a SAP *rejection* (reopen the approval — nothing was committed)
from a SAP *timeout* (`SAP_UNKNOWN`, block resubmission — the document may
exist). **That distinction is a financial invariant.** Collapsing the two states
during a refactor would produce duplicate payments.

### 8.3 Migration hygiene

248 migrations on disk, all applied; graph is clean (one leaf per app, verified).
But:

- **44 orphan rows** in `django_migrations` with no file on disk (`orders` 22,
  `users` 20, `hana` 1, `sap_sync` 1) — residue of earlier history cleanup.
  Harmless, but they mean the ledger no longer describes the tree.
- Several migrations were written blind against a database whose files were
  missing, and two were deliberate re-cuts of the same schema change. Both are
  documented in `docs/BRANCH_MERGE_2026-08-26.md` §6.
- Schema has drifted *ahead* of the tree at least once: `parent_item_code`
  existed on the database before its migration did (see
  `BRANCH_MERGE_2026-08-26.md` §10.2).

### 8.4 Data-layer observations

- No `select_related`/`prefetch_related` audit has been done; with 25 models in
  `orders` and list endpoints over them, N+1 is likely. Not yet measured — do not
  treat this as a finding, treat it as a Phase 4 task.
- `django-filter` and `drf-spectacular` are both installed. `drf-spectacular` is
  in `requirements.txt` but no schema endpoint is wired into `OMS/urls.py` — the
  dependency is present and unused. That is a free win in Phase 2.
- The `payments` schema separation works well and is worth copying for `tracker`,
  which is already FK-isolated and could own a schema cleanly.

---

# Part III — The refactor plan

## 9. Principles

1. **Every phase ends with a working system.** No phase depends on a later one to
   be correct.
2. **Tests before changes.** Phase 0 exists because `users` and `tracker` have
   zero tests and are the first things Phase 1–2 touch.
3. **Behaviour-preserving by default.** Where a refactor must change behaviour
   (permissions being the big one), that is called out and staged behind a
   measurement step, never assumed.
4. **The database is shared and live.** Every schema change needs the
   guarded-DDL treatment (`IF NOT EXISTS` / `SeparateDatabaseAndState`) already
   established in `orders/0056` and `users/0030`, because environments are not in
   sync.
5. **Copy `payments`.** It already demonstrates the target architecture.

---

## Phase 0 — Safety net *(prerequisite, ~1 week)*

Nothing else is safe without this.

| # | task | why |
|---|---|---|
| 0.1 | CI running `pytest` + `makemigrations --check` on every push | Migrations have drifted from the tree twice already |
| 0.2 | Characterisation tests for `users` auth: login, token refresh, user CRUD, role assignment | 0 tests today, and Phase 1 rewrites its permissions |
| 0.3 | Characterisation tests for `tracker` stage transitions | 3,905 LOC, 0 tests, `apply_action()` is the only movement entry point |
| 0.4 | A **staging database** — a `pg_dump` restore of `order_management` | Currently everything is tested against live `.117` |
| | `scripts/make_staging.py` + `OMS/staging_settings.py` + `scripts/scrub_staging.py`. **Blocked on one grant** — see below. | |
| 0.5 | Endpoint inventory: all 298 routes → auth requirement, roles, callers | Phase 1 cannot be done safely without knowing who calls what |
| 0.6 | Wire up `drf-spectacular` at `/api/schema/` | Already a dependency; gives 0.5 for free and documents the API |

**Exit criteria:** CI green; every `users` and `tracker` endpoint has at least one
test; a staging DB exists; the endpoint inventory is complete.

---

## Phase 1 — Critical security *(do 1.1–1.3 immediately, not on a schedule)*

| # | task | risk |
|---|---|---|
| 1.1 | Lock down `CreateUserView`, `DeleteUserView`, `UserDetailView` and the party-assignment views; constrain the `role` queryset so a non-admin cannot assign privileged roles | **Low** — these should never have been public |
| 1.2 | `SECRET_KEY` from env, delete the hardcoded value, remove `DEBUG = True`, drop `'*'` from `ALLOWED_HOSTS` | Medium — needs a deploy with `.env` set correctly |
| 1.3 | Rotate `SECRET_KEY` **and** set a distinct `JWT_SIGNING_KEY` | **High blast radius** — invalidates every token; needs a maintenance window and comms |
| 1.4 | Replace `CORS_ALLOW_ALL_ORIGINS` with an explicit `CORS_ALLOWED_ORIGINS` list | Low — mobile is unaffected, only browsers |
| 1.5 | Add DRF throttling, especially on login | Low |
| 1.6 | Security headers: HSTS, SSL redirect, secure cookies | Low, behind a reverse proxy check |
| 1.7 | Make `CreateUserSerializer` run `AUTH_PASSWORD_VALIDATORS` | Low |

**Note on 1.3.** Because the key is in git history, every JWT ever issued is
forgeable by anyone with repo access. 1.1 and 1.2 reduce exposure; only 1.3 ends
it.

---

## Phase 2 — Permission architecture *(~2 weeks)*

The goal is that **authorization is data, not scattered decorators** — which is
what `tracker/permissions.py` already achieves for one app.

| # | task |
|---|---|
| 2.1 | Set `DEFAULT_PERMISSION_CLASSES = ['rest_framework.permissions.IsAuthenticated']` — flips the default from open to closed |
| 2.2 | Explicitly mark the genuinely public endpoints `AllowAny` (login, refresh, health, public asset view) — the inventory from 0.5 says which |
| 2.3 | Extract the `ROLE_PAGE_MAP` pattern into a project-level `core/permissions.py`; one map from role → capability, one resolver |
| 2.4 | Migrate `hana`, `SKU`, `legal`, `invoice`, `serviceLayer`, `einvoice`, `ewaybill` onto it — these are the apps with no declarations at all |
| 2.5 | Resolve the `extra_roles` question (§7.4) — one rule, applied everywhere |
| 2.6 | Object-level permissions where row ownership matters (a salesman seeing only their parties) |
| 2.7 | Tests asserting **401/403** for every endpoint, generated from the 0.5 inventory |

> 2.1 is the single highest-value change in this document. It converts every
> future forgotten `permission_classes` from a silent hole into a 403.
> It is also the most likely to break a caller, which is why 0.5 and 2.2 come
> first.

---

## Phase 3 — Structural refactor *(~3–4 weeks, incremental)*

Target shape, per app — the one `payments` already has:

```
app/
  models.py          data only
  serializers.py     shape only
  services/          business logic, no HTTP
  views.py           thin: parse -> service -> respond
  permissions.py     app-specific rules
  selectors.py       read queries (optional but useful for the N+1 work)
```

| # | task |
|---|---|
| 3.1 | Split `orders/views.py` (5,667 LOC) by domain: orders, schemes, parties, products, dashboard |
| 3.2 | Move order business rules into `orders/services/` — follow how `scheme_engine.py` was extracted |
| 3.3 | Split `users/views.py`: auth vs user-admin vs assignment |
| 3.4 | Consolidate `orders.Branches` / `sap_sync.Branch` onto one model |
| 3.5 | Retire `orders.Notification` in favour of `notifications.Notification` |
| 3.6 | Unify the SAP client — `hana/`, `sap_sync/services/`, `serviceLayer/` each hold their own connection handling |
| 3.7 | Replace hand-escaped HANA SQL with parameter binding (§7.5) |
| 3.8 | Consistent error envelope and a DRF exception handler — responses currently vary between `{success, message, data}` and bare DRF shapes |

Do these **one file at a time, each behind its own tests**. 3.1 alone is weeks if
attempted as one change; as ten changes it is routine.

---

## Phase 4 — Database and performance *(~2 weeks)*

| # | task |
|---|---|
| 4.1 | Clean the 44 orphan `django_migrations` rows — carefully, on staging first |
| 4.2 | Query audit: `django-debug-toolbar` or `nplusone` on the top 20 endpoints; add `select_related`/`prefetch_related` |
| 4.3 | Index review — the FK-heavy `orders`/`order_items`/`schemes` join paths |
| 4.4 | Move `tracker` into its own schema, as `payments` and `hais` already are |
| 4.5 | Add `db_index` / constraints where invariants are currently enforced only in Python |
| 4.6 | Consider squashing migrations per app once the graph is stable — this is what finally retires 4.1 |
| 4.7 | Connection pooling (`CONN_MAX_AGE`) and a HANA connection-pool review |

### What Phase 4 measurement actually showed

The plan's performance items were written before anyone measured. The database
is **61 MB**, and the tables 4.2/4.3 target are tiny:

| table | rows |
|---|---|
| `sap_party_addresses` | 35,719 |
| `audit_log` | 14,047 |
| `party_product_assignments` | 13,423 |
| **`orders`** | **131** |
| **`order_items`** | **159** |

All 11 unindexed foreign keys are on tables under 88 kB — Postgres will
sequential-scan those faster than it can consult an index, and would ignore one
if added. The tables that *do* have volume are already covered:
`sap_party_addresses` has a `card_code` index, which is the column its endpoint
filters on.

So **4.3 was not done** — adding those indexes would be cargo-culting. **4.4**
(move `tracker` to its own schema) was not done either: 15 tables and a
`search_path` change for organisational tidiness, against invariant #10.
**4.6** (squash) buys nothing at this size.

### 4.1 — the one orphan row that was not inert

Of the 44 orphan `django_migrations` rows, 43 are harmless history. One is not:
**`hana` had zero migration files on disk and an applied `0001_initial` row**.
The day someone added the first model there, Django would write
`0001_initial.py`, find the row already recorded, and **silently skip it** —
the model changes, the table never appears, and it surfaces later as a column
that does not exist.

Fixed by adding `hana/migrations/0001_initial.py` with `operations = []`,
taking the name. **Zero database writes** — the alternative, deleting the row,
destroys the evidence for no gain. The other 43 rows were left alone.

### 4.5 — the tracker HOLD defect, and the constraint that pins it

Found by the Phase 0.3 characterisation tests. A full HOLD (and a rejection
awaiting its reason) was written as a `RECEIVE` row with `entered_at` copied
from the visit and no `exited_at` — so a held stage carried two rows that both
looked like an open visit and **tied on the only column anything ordered by**.
`_open_event` picks with `.order_by('-entered_at').first()`, so which one a
later advance closed was left to the database, and the other was stranded open
at a stage the invoice had left. `tracker/exports.py:_latest_by_stage` had the
same tie, with a strict `>`.

The obvious fix — stamp `exited_at` on notes — would have been **wrong**:
`tracker/views.py` reads `exited_at is None` on a rejection note to mean
"awaiting the written reason", so closing notes would have silently cleared
every pending rejection in the SAP/JSAP two-step.

The fix is a new `EventType.NOTE`. A choices change emits **no SQL**
(`sqlmigrate` prints `-- (no-op)`), so no column was created. `_open_event` and
`_latest_by_stage` now exclude notes and tie-break on `id`.

Backfill required: **none**. Production holds 68 events, and zero note-shaped
rows — no full HOLD or pending rejection has ever been written there. The
defect was latent, not manifest.

The invariant is then enforced rather than merely intended:

```sql
CREATE UNIQUE INDEX tracker_stage_event_one_open_visit_per_stage
  ON tracker_stage_event (invoice_id, stage_id)
  WHERE exited_at IS NULL AND NOT (event_type = 'NOTE');
```

Verified satisfiable against production before adding: 0 `(invoice, stage)`
pairs with more than one open row.

### 4.7 — connection reuse

`CONN_MAX_AGE` was **unset**, i.e. Django's default of 0: a fresh Postgres
connection opened and torn down on every single request. Now 60s with
`CONN_HEALTH_CHECKS = True`, which validates a reused connection at the start
of each request rather than letting a server-side close surface as an
`InterfaceError` on a random query. Safe only since 5.1 moved the scheduler out
of the web process — a background thread in a web worker could otherwise hold a
connection open with no request boundary to close it.

---

## Phase 5 — Operations *(~1 week)*

| # | task |
|---|---|
| 5.1 | Move APScheduler out of the web process into a dedicated worker (or Celery) — ~~today jobs duplicate under multiple workers~~ see the correction below |
| 5.2 | Structured logging with request IDs; the `audit` middleware already gives a hook |
| 5.3 | Health/readiness endpoints covering Postgres, HANA and Service Layer |
| 5.4 | Error tracking (Sentry or equivalent) |
| 5.5 | Externalise remaining hardcoded hosts (`SAP_DB_HOST`, `JSAP_DB_HOST` have IP defaults in `settings.py`) |

### 5.1 — correction to the premise

The line above says jobs duplicate under multiple workers. They did not,
because they never ran at all. `SapSyncConfig.ready()` gated the start on
`os.environ.get('RUN_MAIN') == 'true'`, and `RUN_MAIN` is set by the Django
dev-server **autoreloader** and by nothing else — not waitress, not gunicorn,
not IIS. Under every production server the condition was false.

The database confirms it: 0 rows in `SyncSchedule`, 0 in `django_apscheduler`'s
job and execution tables, and of 372 rows in `sap_sync_logs` not one carries
`triggered_by='scheduled'`. APScheduler has never run a single job in the
lifetime of this system. The bare `except` printing to stdout is why that was
silent.

Two further defects were found alongside it:

* `add_schedule_job` / `refresh_schedules` were reachable only from
  `start_scheduler`. **No view ever called them**, so creating, editing or
  deactivating a schedule through `/api/sap-sync/schedules/` wrote a row and
  changed nothing about what was scheduled.
* `IntervalTrigger(minutes=0)` raises, and `custom_interval_minutes` is an
  `IntegerField` with no validator, so a `CUSTOM` schedule saved with 0 would
  have taken down reconciliation for every other schedule too.

Both readings of 5.1 — "runs twice" and "never runs" — argue for the same fix,
so the task stands as written. Scheduling now lives in
`manage.py run_scheduler`, a dedicated process holding a Postgres **session
advisory lock** so a second copy exits rather than double-running. Schedule
changes reach it by reconciliation from the table every 60s, which is the only
channel that survives a worker restart. See `run_scheduler.bat`.

### 5.3 — what was built

`/api/health/live/` (public, no I/O), `/api/health/ready/` (public, terse) and
`/api/health/detail/` (admin only, carries the error text). Postgres is the
only **critical** dependency: HANA and the Service Layer are **degraded**, so
a SAP outage does not empty the load-balancer pool while orders, tracker,
approvals and payments — all Postgres — keep working. The Service Layer probe
deliberately does not log in, because a login per poll would consume SAP's
concurrent-session licence.

### 5.2 — what was built

`core/request_context.py` holds a request ID in a **`ContextVar`** (not a
thread-local: only a ContextVar survives `async def` views, and its reset token
restores the previous value exactly rather than leaving the last request's ID
in a pooled worker). `core.logging.RequestContextFilter` is attached to the
**handlers**, so all several hundred existing `logger.*` calls — plus Django's
own loggers and third-party libraries — gain correlation without one call site
changing.

`X-Request-ID` is accepted from the caller, **validated against
`[A-Za-z0-9._-]{1,64}`**, and echoed on the response. The validation is a
security control, not tidiness: the value is written verbatim into every log
line of the request, so an unvalidated one is a log-forgery primitive. A
rejected value is replaced, never repaired.

`LOG_FORMAT=json` switches the file handlers to one JSON object per line; text
stays the default because these logs are read directly, by a person, on the
Windows box that produces them.

Found on the way: the hand-maintained app-logger list had dropped **`HAIS`,
`SKU`, `audit` and `notifications`**, so their INFO records fell through to
root (level WARNING) and were discarded. `audit` is the one that mattered — it
logs the failures of the audit trail itself. The list is now derived from
`INSTALLED_APPS`.

### 5.4 — what was built

Sentry, **entirely opt-in**: with `SENTRY_DSN` unset — the default, and what CI
and every developer machine run under — nothing initialises and nothing leaves
the process.

The scrubber is the load-bearing part, because requests here carry SAP Service
Layer credentials, GSTINs, party master data and NIC e-invoice tokens.
`send_default_pii=False` (so no Authorization header, cookies, body or IP), the
query string and request body are **dropped rather than scrubbed**, and
`before_send` redacts by key name and by value shape — JWTs, `B1SESSION=`
cookies and inline `password=` / `"password":` assignments — *before* anything
crosses the network. Verified against the real `SAP_DB_PASSWORD` in four
shapes; all four redacted. Tracing defaults to 0.0 because it samples SQL text
and is billed per event.

`init` catches everything: `sentry_sdk.init` raises `BadDsn` on a malformed
DSN, and it runs at import time in `settings.py`, so a typo in `.env` would
otherwise have stopped the whole deployment from booting.

### 5.5 — done earlier

Completed during the credential removal: `SAP_DB_*` and `JSAP_DB_*` now read
from the environment with no defaults, and `settings.py` raises
`ImproperlyConfigured` on a partial JSAP config.

---

## Phase 6 — API contract *(~1 week)*

| # | task |
|---|---|
| 6.1 | Publish the `drf-spectacular` schema and keep it in CI |
| 6.2 | Version the API (`/api/v1/`) before changing response shapes |
| 6.3 | Standardise pagination, filtering, ordering via `django-filter` |
| 6.4 | Deprecation policy for the mobile client — `devices.VersionPolicy` already provides the enforcement mechanism |

### 6.2 — versioning, added additively

Every API route is now reachable at **both** prefixes:

```
/api/<app>/...        what every existing client calls today — unchanged
/api/v1/<app>/...     the same routes, under an explicit version
```

Same view object behind each, asserted route by route (`assertIs` on the
callback), so the two cannot fork. The versioning had to happen before any
response shape changes and could not break the live web and mobile clients, so
it was added alongside rather than in place of.

The v1 mount is **namespaced**. Including the same patterns twice registers
every route name twice, and `reverse('health-live')` would then resolve to
whichever was registered last — silently, since `reverse` cannot fail here.
With the namespace, `reverse('health-live')` still means the unversioned path
and `reverse('v1:health-live')` the versioned one.

The published OpenAPI schema describes **only** `/api/v1/`
(`core/schema.py:only_versioned_routes`). Describing both would list every
endpoint twice and collide every `operationId` — drf-spectacular resolves those
with numeric suffixes, so the contract would name operations
`orders_submit_create` and `orders_submit_create_2` with nothing to say which
is which.

The route audit in `core/tests.py` normalises the two prefixes to one
canonical path rather than duplicating the allowlist, because two lists would
be free to drift; `core/tests_api_contract.py` separately asserts the two
prefixes carry **identical permission classes**, which the normalised audit by
construction cannot see.

### 6.3 — pagination, opt-in

The problem is real and measured: `GET /api/sap/addresses/` serialises all
**35,719** rows of `sap_sync.PartyAddress` on every call, unfiltered.

The obvious fix is not available. `DEFAULT_PAGINATION_CLASS` turns a JSON array
into an object, and the live clients index straight into the array — it would
break every list endpoint at once, which is the exact class of change 6.2
exists to prevent.

So `core.pagination.OptInPagination` paginates **only when asked**. No `?page=`
or `?page_size=` and the response is byte-for-byte what it is today; a client
that passes `?page=1` gets the envelope and can migrate on its own schedule.
Applied to the three largest list endpoints (`addresses`, `products`,
`parties`). `max_page_size=200` stops the unbounded response being
reintroduced through `?page_size=99999`.

Nothing is truncated — that would be a correctness bug dressed up as a
performance fix. Instead an unpaginated response over 1,000 rows is logged with
its size and the **calling client**, which is the fact that says when
pagination can become the default.

`django-filter` was deliberately not adopted. The filtering these endpoints
need already exists and works; converting it would be a rewrite with no
behaviour change plus a new dependency. `ordering_from`'s allow-list is the
part that mattered for safety, and it now has tests.

### 6.4 — deprecation mechanism

`core/deprecation.py` marks a route deprecated, tells the caller in the
standard fields — `Deprecation`, `Sunset` (RFC 8594) and
`Link: rel="successor-version"` — and logs each call with the platform,
version and build that made it.

Headers, never the body: a new body field would change the response shape of
exactly the endpoints being retired.

Applied first to `orders.NotificationListView`, which unblocks **3.5**. That
item has been stuck on a question nobody could answer — is the old endpoint
still called, and by whom? It now answers itself. **No sunset date is set**,
deliberately: the date belongs to whoever owns the client migration.

The removal sequence is: mark here → read the usage log → set a sunset →
let `devices.VersionPolicy` enforce the client floor (HTTP 426) → delete.

---

## 10. Suggested order

```
NOW      1.1  lock down user-management endpoints
         1.2  SECRET_KEY / DEBUG / ALLOWED_HOSTS

WEEK 1   Phase 0  safety net
WEEK 2   1.3–1.7  remaining security  (needs 0.4 staging)
WEEK 3-4 Phase 2  permission architecture
WEEK 5-8 Phase 3  structural refactor
WEEK 9-10 Phase 4 database
WEEK 11  Phase 5  operations
WEEK 12  Phase 6  API contract
```

Phases 3–6 can overlap once Phase 2 is done. Phases 0–2 must be sequential.

---

## 11. Invariants — things the refactor must not break

A checklist to test against after every phase.

1. **Company/branch travels with every SAP request.** Oil, Beverages and Mart are
   separate HANA databases and the same DocNum is a different document in each.
2. **`Stage.code` is the tracker's key, never `Stage.name`.** Renaming a stage is
   safe; changing its code breaks the flow.
3. **`Stage.order` is unique** — reordering requires parking rows out of range
   first (`seed_tracker` and migration `0014` both do this at offset 900/1000).
4. **Tracker soft-deletes; nothing is destroyed.** Invoice numbers stay reserved.
5. **The tracker never writes to SAP or JSAP.** It mirrors them. Only a human
   decision flows back.
6. **`approvals` is document-type agnostic.** Do not couple it to `payments`.
7. **JWT access lifetime cannot shorten until the clients implement refresh.**
8. **The mobile version gate must stay after `CorsMiddleware`** and must only act
   on requests carrying `X-Platform`.
9. **Guarded DDL on every migration.** Environments are not in sync; assume the
   column may already exist.
10. **`payments`, `hais` — and any new schema — need `search_path` kept correct.**
11. **e-invoice: validate before sending.** NIC rejects cost money and rate limit;
    the `B2C_NOT_ELIGIBLE` pre-check exists for that reason.
12. **`orders.OrderFlowConfig` / `PartyOrderFlowConfig` make the order lifecycle
    per-party configurable.** It is not one hardcoded flow.

---

## 12. Companion reference material

The exact, regenerable detail lives in [`docs/codebase/`](codebase/README.md):

| file | contents |
|---|---|
| [`API_SURFACE.md`](codebase/API_SURFACE.md) | All 294 routes with resolved permissions |
| [`DATA_MODEL.md`](codebase/DATA_MODEL.md) | All 98 models, 1,015 fields |
| [`INVARIANTS.md`](codebase/INVARIANTS.md) | 609 rules the code states about itself, with `file:line` |
| [`DOC_STALENESS.md`](codebase/DOC_STALENESS.md) | Which pre-existing docs have drifted |

All four are **generated from the code**, so they cannot rot the way prose does.
Regenerate them after structural change; §Phase 0 moves the scripts into CI.

### 12.1 A third repository exists

`docs/RELEASE.md` references `OMS-app/` — a **React Native mobile client** that
is not checked out in this workspace and was not examined. It consumes this API.

This is a hard constraint on Phases 2, 3 and 6: **API changes break a client
nobody here can inspect or update in lockstep.** `devices.VersionPolicyMiddleware`
and its HTTP 426 gate exist precisely because old app builds stay in the field.
Get the mobile repo before changing any response shape.

### 12.2 Pre-existing documentation is not trustworthy

The repo already carries ~15,500 lines of `.md`. Measured against the code:

- **2 docs name source files that no longer exist.** `docs/ap-invoice-service-layer.md`
  documents `ap_flow.py`, `ap_post.py` and `ap_attach.py`; the real
  implementation is `serviceLayer/ap_views.py`. It describes a structure that
  was never built.
- **16 docs are more than 30 days behind** the app they describe;
  `README.md` is 100 days behind `orders`.

Use them as hints, never as specification. That is why the four files above are
generated instead of written.

## 13. What this document does not cover

Stated so the gaps are known rather than assumed closed:

- **Not every one of the ~50,600 lines was read.** Routing, models, permissions
  and the security surface were derived *mechanically* and are complete.
  Business logic was read in risk order — `orders/scheme_engine.py`,
  `payments/sap_poster.py`, `approvals/services.py`, `core/models.py`,
  `users/models.py`. The remainder is covered by `INVARIANTS.md` only to the
  extent the code documents itself, which for `payments` (208 statements) and
  `orders` (87) is substantial and for `HAIS` (1) is not.
- **`orders/views.py` has since been read** — findings in
  [`codebase/ORDERS_VIEWS.md`](codebase/ORDERS_VIEWS.md): 7 confirmed defects
  (two of them data-losing), 71 sites keying business logic off mutable status
  *names*, hardcoded status primary keys no migration guarantees, and an
  order-number race that `core.next_document_number()` already solves. The
  dashboard aggregations, Mart flow and quotation views within it were surveyed
  structurally rather than line by line; the three defect patterns found are
  systemic to the file, so more of the same kind are likely there.
- **No runtime profiling.** §8.4's N+1 remark is a hypothesis, not a measurement.
- **No frontend coverage** — that is the companion document,
  `OMS-Frontend/docs/CODEBASE_AND_REFACTOR_PLAN.md`.
- **No infrastructure review** — deployment, reverse proxy, TLS termination and
  backup policy were not examined.
- **`OMS-app` not examined** — see §12.1.

---

## 14. Status — 2026-09-02

This document's own status prose above is pinned to 2026-08-27 and, in several
places, describes work as blocked or not-started that a same-day, later
commit (`38c4e87`) already finished. This section replaces impression with a
line-by-line audit: every numbered item below was checked against the actual
current code — not commit messages, not this document's own earlier prose —
by seven independent read-only passes, one per phase. Nothing was executed
(no `manage.py`, no `pytest`, no live DB/SAP/HANA connection) — this is a
static-code audit, not a rehearsal.

Two blocked items are not defects: **1.3** (rotating `SECRET_KEY` and setting
a distinct `JWT_SIGNING_KEY`) is the operator's own follow-up — the code fully
supports it via env vars, but the working `.env` sets neither, so the app
still runs on the compromised dev fallback key. **4.4** (moving `tracker` to
its own schema) is paused under the operator's standing no-database-
structural-changes constraint, not overlooked — see `Invariant #10` below and
the frontend companion document's equivalent constraint.

### Phase 0 — Safety net: **done**

| # | item | status | evidence |
|---|---|---|---|
| 0.1 | CI: `manage.py test` (this project's pytest) + `makemigrations --check` on every push | **done** | `.github/workflows/ci.yml` — dry-run check + test run + `manage.py check` + schema generate/validate, all on push/PR. |
| 0.2 | Characterisation tests for `users` auth | **done** | `users/tests.py` (443 lines) — login, refresh, user CRUD, role-assignment lockdown, password policy, scoped access, login throttling, `extra_roles` resolution. |
| 0.3 | Characterisation tests for `tracker` stage transitions | **done** | `tracker/tests_stage_transitions.py` (503 lines, ~40 methods) exercises `apply_action` directly — routing, permissions, holds, rejections, locking, terminal stages. |
| 0.4 | Staging database (pg_dump restore) | **done** | `scripts/make_staging.py` + `OMS/staging_settings.py` + `scripts/scrub_staging.py` all exist and are fully implemented, including a `--target-host` flag that is the actual fix for the "blocked on one grant" note — staging lives on a different server, sidestepping the need for `CREATEDB` on the production role. Not rehearsed end-to-end by this audit (that would touch a live-adjacent server), so the one open item is a dry run, not missing code. |
| 0.5 | Endpoint inventory (route → auth/roles) | **done** | `scripts/endpoint_inventory.py` walks the live resolver; output committed at `docs/codebase/API_SURFACE.md`; continuously regression-tested by `core/tests.py::PublicEndpointAllowlistTests`. "Callers" (which frontend paths hit each route) isn't tracked — a minor gap against the item's literal wording, not against its purpose. |
| 0.6 | `drf-spectacular` at `/api/schema/` | **done** | Wired in `OMS/urls.py`; `SERVE_PERMISSIONS` deliberately locked to `IsAuthenticated` (not spectacular's own `AllowAny` default) so the API surface isn't leaked anonymously; `manage.py spectacular --validate` runs in CI. |

Beyond the checklist: `core/tests.py` also carries `AdminGuardTests`,
`EnvExampleTests`, `NoCommittedCredentialsTests` and others — a broader
security-hygiene suite than Phase 0 asked for, effectively covering parts of
Phase 1 ahead of schedule.

### Phase 1 — Critical security: **done except the rotation itself**

| # | item | status | evidence |
|---|---|---|---|
| 1.1 | Lock down user-admin + party-assignment views; block role-escalation | **done** | `IsAuthenticated + IsAdminRole` on `CreateUserView`/`UserDetailView`/`DeleteUserView` and the party-assignment views; role-escalation blocked by a `validate()`-time guard (`assert_may_assign_roles`/`PRIVILEGED_ROLE_NAMES`) rather than a constrained queryset — same outcome, different mechanism than the plan's literal wording. |
| 1.2 | `SECRET_KEY` from env, `DEBUG`/`ALLOWED_HOSTS` fixed | **done** | The old hardcoded key survives only as a DEBUG-mode fallback; production with no `SECRET_KEY` set raises `ImproperlyConfigured` rather than silently using it. `'*'` is appended to `ALLOWED_HOSTS` only `if DEBUG`. |
| 1.3 | Rotate `SECRET_KEY` and set a distinct `JWT_SIGNING_KEY` | **blocked — operator's own task** | The env-var code path exists and is documented in `.env.example`, but the working `.env` sets neither key and still runs `DEBUG=True` — still on the compromised fallback. Not something an automated session should do. |
| 1.4 | Explicit `CORS_ALLOWED_ORIGINS` | **done** | Real allowlist is now the default; `CORS_ALLOW_ALL_ORIGINS` still exists but defaults to `false` and is documented as an emergency escape hatch only. |
| 1.5 | DRF throttling, especially login | **done** | `Anon`/`User` rate throttles project-wide; `LoginView` and the token-refresh view additionally use a scoped `login` throttle (10/min default). |
| 1.6 | Security headers (HSTS, SSL redirect, secure cookies) | **done** | All present under `if not DEBUG`; HSTS seconds intentionally starts at 0 as a staged rollout until HTTPS is confirmed working — a deliberate sequencing choice, not a gap. |
| 1.7 | `CreateUserSerializer` runs `AUTH_PASSWORD_VALIDATORS` | **done** | `validate_password()` now calls Django's real validators before `create()` — similarity, minimum length, common-password and numeric checks all enforced, not just the field's own `min_length=6`. |

### Phase 2 — Permission architecture: **strong, with one real gap**

| # | item | status | evidence |
|---|---|---|---|
| 2.1 | `DEFAULT_PERMISSION_CLASSES = IsAuthenticated` | **done** | Set in `REST_FRAMEWORK` settings; regression-tested (`test_the_default_is_closed`). |
| 2.2 | Public endpoints explicitly `AllowAny` | **done** | Exactly 4 categories (login, refresh, health liveness/readiness, the public e-invoice QR image) — nothing else; a committed allowlist test fails the build if any other route opens up. |
| 2.3 | `ROLE_PAGE_MAP` pattern extracted into `core/permissions.py` | **done, but as a different design than described** | `core/permissions.py` is the single source of truth for role identity/admin-ness, replacing 5 previously-disagreeing local `is_admin` definitions — but its own docstring explicitly argues against building a project-wide role→capability map, confining that pattern to `tracker` on purpose. The plan's literal wording should be read as superseded by this considered decision, not as unfinished. |
| 2.4 | Migrate `hana`, `SKU`, `legal`, `invoice`, `serviceLayer`, `einvoice`, `ewaybill` onto `core/permissions.py` | **done, 2026-09-02** | Every view in `hana` (23 classes), `SKU` (4), `legal` (8), `invoice` (12 that lacked it), `einvoice` (20 `@api_view` functions; the public `irn_qr_png` left `AllowAny` and byte-for-byte untouched) and `ewaybill` (12 functions) now declares `permission_classes` explicitly instead of silently inheriting the Phase 2.1 default, each app carrying a module docstring recording why. `serviceLayer/ap_views.py` needed no change — all 4 of its view classes were already covered by `tracker.permissions.IsTrackerAP`. **Deliberately no new role restrictions were invented:** none of these 7 apps has an existing in-app `IsAdminRole` precedent to extend (unlike `sap_sync`, which had 8 admin-gated views before its own migration), so tightening any of them would have been a guess at intent, not a migration. The borderline cases are named rather than silently decided — `SKU.SKUDetailView`'s DELETE and `legal`'s three `RetrieveUpdateDestroyAPIView`s, all of which permit destructive actions by any authenticated user today exactly as they did before. One genuine bug was fixed in passing: `invoice.branches_for_user()` gated on raw `is_superuser`, so an admin via the `admin` role or `extra_roles` went unrecognised and could be wrongly scoped to their own (possibly empty) categories — now calls `core.permissions.is_admin()`, a pure widening. Verified: the endpoint inventory shows the same 10 open routes as before, nothing newly closed or opened. |
| 2.5 | Resolve `extra_roles` with one rule everywhere | **done** | `tracker_pages_for()` now calls the shared `core.permissions.role_names()`, fixing the exact bug §7.4 described; `devices`, `approvals`, `payments` permissions all import the shared `is_admin` instead of local copies. |
| 2.6 | Object-level permissions (row ownership) | **done** | A user can only view their own party assignments unless admin; `PartyView` filters through `UserPartyAssignment` — the literal "salesman sees only their own parties" example, and it's tested. |
| 2.7 | Tests asserting 401/403 for every endpoint | **partial** | Achieved a different, arguably stronger way: `PublicEndpointAllowlistTests` statically resolves the actually-applied permission class for every live route (not just a grep) and fails if anything is open beyond the allowlist. Live HTTP 401/403 requests exist for only a handful of routes, not the full inventory. |

`core.permissions.IsSelfOrAdmin` is defined with a full usage docstring but
has zero call sites anywhere — dead code; 2.6's actual behaviour is achieved
with inline checks instead.

### Phase 3 — Structural refactor: **mixed, as expected for a 3–4 week incremental phase**

| # | item | status | evidence |
|---|---|---|---|
| 3.1 | Split `orders/views.py` (5,667 LOC) by domain | **done** | Now an 11-module package (`dashboards.py`, `lifecycle.py`, `masters.py`, `queries.py`, `schemes.py`, etc., ~5,481 LOC total), no legacy remnant, `__init__.py` re-exports so nothing else needed to change. |
| 3.2 | Order business rules into `orders/services/` | **done, 2026-09-02** | Both remaining inline blocks were extracted, each as a mechanical statement-for-statement move rather than a rewrite. `UpdateOrderStatusView`'s ~370-line transition body → `apply_order_status_transition()` in new `orders/services/order_status.py`; the view's `post()` now only validates, locks the row (`select_for_update()` inside the same `@transaction.atomic`) and delegates. **9 characterization tests were written first**, asserting today's behaviour including its known quirks, before a line moved (131 → 140 tests, all green before and after). `MartApproveView`/`MartResendSapView`'s SAP-posting bodies → `approve_and_post_to_sap()` / `resend_sales_order_to_sap()` in new `orders/services/mart_posting.py`, along with `_sap_outcome_is_ambiguous()` and `_mart_release_sap_claim()`; 140/140 identical before and after. Nothing about conditions, branch order, side effects, hardcoded status ids (3, 6, 9, `APPROVER_REJECTED_ACTION_ID`) or the 71-sites-key-off-status-*name* coupling was touched — those remain exactly as they were, deliberately, and are still the real §8.1 problem. **Known fragility, pre-existing and not introduced here:** both new service modules import from `orders.views._shared`, so importing either in true isolation (before `orders.views` has loaded) raises `ImportError` through a circular path; it never fires in practice because the URLconf always loads the views package first. |
| 3.3 | Split `users/views.py` | **done** | Now `auth.py` / `accounts.py` / `assignments.py`, matching the plan exactly. |
| 3.4 | Consolidate `orders.Branches` / `sap_sync.Branch` | **done** | `orders.Branches` state-deleted via a no-SQL migration (`sqlmigrate` prints nothing, both models were `managed=False`); `sap_sync.Branch` is the sole surviving model. |
| 3.5 | Retire `orders.Notification` for `notifications.Notification` | **partial — groundwork complete, migration not scheduled** | `orders.Notification` is still the live, actively-written model. Both of its remaining endpoints (`NotificationListView` and `NotificationHistoryView`) now carry `@deprecated` instrumentation (headers + usage logging) pointing at the `notifications` app as successor, consistently. No data or traffic has moved, and none is in progress: migrating `orders.Notification` itself is a deliberate, un-scheduled decision left for the operator, not a coding task this audit advances further. |
| 3.6 | Unify the SAP client | **done as scoped, 2026-09-02 — with two divergences deliberately left unmerged** | New `core/sap_client_base.py` holds the helpers that were genuinely identical: `verify_setting()` and `timeout_setting()` (imported as `_verify`/`_timeout` by `serviceLayer/service.py` and `payments/sap_client.py`) and `base_url()` (imported as `_base` by `einvoice/sap.py` and `payments/sap_client.py`). Each file's own call sites and typed exception (`SapFetchError`, `SapError` with `status_code`/`sap_code`/`payload`, and `serviceLayer`'s bare `Exception`) are unchanged. 223/223 `serviceLayer`+`einvoice`+`payments` tests pass, identical before and after. **Two real divergences were found and NOT force-merged**, because merging them would have been a behaviour change disguised as deduplication: (1) `einvoice/sap.py`'s verify helper reads only `HANA_SSL_VERIFY` and never consults `HANA_SSL_CA_BUNDLE` as the other two do — currently inert because this deployment leaves that setting unset, but a genuine inconsistency in the code as written, and a live one the moment a CA bundle is configured; (2) `serviceLayer/service.py` has no `_base()` at all (it builds its single login URL inline without the `rstrip('/')`), so it was left alone rather than newly wired onto the shared one. **Out of scope by design:** the two direct-DB stacks (`hana/services/connection.py` over `hdbcli`, `sap_sync/services/connection.py` over `pyodbc`) were not merged with these three REST/session-cookie HTTP clients — different protocol, different lifecycle; forcing a shared base across them would glue together two unrelated things rather than remove duplication. The plan's original "unify the SAP client" wording should be read as satisfied by *this* scoping, not by a single universal client. |
| 3.7 | Parameter-bind HANA SQL (§7.5) | **done** | `hana/services/connection.py` — the file §7.5 named — now binds every value-bearing query with `?` placeholders; only allow-listed schema names (never request input) are still interpolated. *Not covered by this fix and worth a future look:* `sap_sync/services/connection.py`'s OPENQUERY building and `serviceLayer/ap_views.py`'s OData `$filter` interpolation are the same injection-risk class and remain untouched. |
| 3.8 | Consistent error envelope + DRF exception handler | **done** | `core.exception_handler.api_exception_handler` is wired in; it additively sets envelope keys via `setdefault` (never overwrites a view's own response shape) and turns previously-unhandled exceptions into JSON 500s instead of Django's HTML error page. |

### Phase 4 — Database and performance: **the decisions hold up; one item was never attempted**

| # | item | status | evidence |
|---|---|---|---|
| 4.1 | Clean the dangerous orphan `django_migrations` row | **done** | `hana/migrations/0001_initial.py` is now a genuine empty migration, closing the one orphan row that could have made Django silently skip `hana`'s real first migration. The other 43 orphan rows are inert history, left alone by design. |
| 4.2 | Query audit (`django-debug-toolbar`/`nplusone`, top 20 endpoints) | **done — measured, not tooled** | No `django-debug-toolbar`/`nplusone` installed (live-endpoint tooling is off-limits per the standing constraints); instead, 16 new tests across `orders`, `sap_sync`, `tracker`, `approvals` (`tests_query_audit.py` in each) hit list/dashboard endpoints via the Django test client against `OMS.test_settings`' in-memory sqlite DB, seeding 3 then 6 related rows and asserting the query count via `CaptureQueriesContext` does NOT scale — the actual N+1 signature. Found and fixed 3 real ones (each confirmed to scale before the fix and stay flat after, by temporarily reverting and re-running): `orders.PartyProductsView` ran one `SapProduct` lookup per party-product assignment (`orders/views/masters.py`, now one batched query); `tracker.InvoiceListSerializer.get_editable` re-ran the `extra_roles` M2M query once per invoice row via `all_role_names()` (`tracker/serializers.py`, now cached per request); `tracker.StuckAlertSerializer.get_notified` called `.select_related('user')` on an already-`prefetch_related`-populated manager, which clones the queryset and silently drops Django's prefetch cache, re-querying per alert (same file, now reads the prefetched `.all()` directly). The other 13 endpoints measured constant, confirming their existing `select_related`/`prefetch_related` — no change needed. One unrelated correctness bug surfaced along the way and was left alone as out of this item's scope: `sap_sync.PartySerializer`'s `addresses` field references a relation migration `0004_...` removed in 2026-02, so `PartyDetailView`/`PartyByCodeView`/`GetPartyByCategoryView` 500 on every real call. |
| 4.3 | New indexes on unindexed FKs | **not done — by measured decision** | DB is 61MB; the largest relevant table has 159 rows. Every candidate table is small enough that Postgres seq-scans faster than an index would. Cargo-culting avoided, not skipped. |
| 4.4 | Move `tracker` to its own schema | **blocked — standing constraint** | 15 tables plus a `search_path` change, which the operator's no-database-structural-changes constraint currently rules out (see `Invariant #10`). Paused pending the operator's go-ahead, not abandoned. |
| 4.5 | The tracker HOLD tie-break defect | **done** | `StageEvent.EventType.NOTE` plus a partial unique constraint (`...one_open_visit_per_stage`) fixes the exact race the doc describes, landed as one no-op migration and one real `AddConstraint`. |
| 4.6 | Squash migrations | **not done — by measured decision, plus a real precondition still missing** | Same "buys nothing at this size" reasoning as 4.3. It doesn't itself require live-DB DDL, but there is still no staging DB rehearsal path exercised (0.4's tooling exists but hasn't been run) to safely rewrite a migration graph against, so this stays deferred either way. |
| 4.7 | Connection reuse (Postgres + HANA) | **partial** | Postgres half done (`CONN_MAX_AGE=60`, `CONN_HEALTH_CHECKS=True`, correctly sequenced after Phase 5.1 moved the scheduler out of the web process). The HANA half was never done: `HANAConnection` still opens and closes a fresh `hdbcli` connection per call, at roughly 49 call sites, with no pooling — and this gap isn't mentioned in the document's own Phase 4 write-up either. |

### Phase 5 — Operations: **done**

| # | item | status | evidence |
|---|---|---|---|
| 5.1 | Dedicated scheduler process, not request-cycle | **done** | `sap_sync/management/commands/run_scheduler.py` uses a Postgres advisory lock so a second copy can't double-run; `apps.py` no longer auto-starts anything. |
| 5.2 | Request-ID / structured logging | **done** | `contextvars`-based request context (not thread-local), validated/echoed `X-Request-ID`, JSON-log opt-in, applied to all app + third-party loggers. |
| 5.3 | Health endpoints | **done** | Liveness/readiness/detail tiers; Postgres is CRITICAL, HANA/Service-Layer/scheduler are DEGRADED-only; an undocumented bonus scheduler probe ties back into 5.1's advisory lock. |
| 5.4 | Error tracking (Sentry, opt-in) | **done** | No-op when `SENTRY_DSN` unset; PII scrubbing before send (headers, sensitive keys, JWTs, session cookies, password fields); trace sampling defaults to 0.0. |
| 5.5 | No hardcoded SAP/JSAP credentials/host | **done** | `SAP_DB_HOST` has no default at all (forces startup failure if unset); JSAP config raises `ImproperlyConfigured` if partially set. |

All five are backed by dedicated test files (`core/tests_health.py`,
`core/tests_logging.py`, `core/tests_error_tracking.py`, and more).

### Phase 6 — API contract: **mostly done, one item half-real**

| # | item | status | evidence |
|---|---|---|---|
| 6.1 | Schema served and CI-validated | **done** | `manage.py spectacular --validate` runs on every push; schema uploaded as a build artifact. |
| 6.2 | Additive `/api/v1/` versioning | **done** | The same URL patterns are mounted at both `/api/` and `/api/v1/` — proven identical (`assertIs` on the resolved callback) rather than just described, via `core/tests_api_contract.py`. |
| 6.3 | Pagination + filtering/ordering standardisation | **done, 2026-09-02** | Pagination was already live (`OptInPagination`, opt-in via `?page=`, capped at 200, logs unbounded ≥1000-row responses) on the three heaviest list endpoints, up to 35,719 rows. The filtering/ordering half is now wired too: `django_filters` added to `INSTALLED_APPS` (it had sat unused in `requirements.txt` since the first commit) and `DjangoFilterBackend` added to `ProductListView`, `PartyListView`, `PartyAddressListView` — **additively**, with `filterset_fields` restricted to columns the views' pre-existing hand-written filters never touch (`sub_group`/`type`/`variety`; `chain`/`country`/`category`; `state`/`city`/`country`/`category`), so no query param can collide with or double-filter against the existing `icontains`/`iexact` logic, which was left completely untouched. Each view now calls the shared `core.pagination.ordering_from` allow-list only when `?ordering=` is actually sent, falling back to its default field on an unlisted or hostile value; with no `ordering` param, `order_by()` is never called at all, so the model's own `Meta.ordering` and the response bytes are unchanged. `devices/admin_views.py`'s duplicate 4-line allow-list check was deleted in favour of the same shared helper, so there is now one implementation rather than two. |
| 6.4 | Deprecation mechanism | **done** | `@deprecated(successor=, sunset=, note=)` sets real `Deprecation`/`Sunset`/`Link` headers and logs usage by client platform/version/build; applied for real to one endpoint (`orders.NotificationListView`) as groundwork for Phase 3.5. |

One thing worth fixing rather than just noting: `docs/codebase/API_SURFACE.md`
(the generated route inventory) predates the 6.2 versioning work and lists no
`/api/v1/` routes at all — nothing in `scripts/` or CI regenerates it
automatically, so §12's own instruction to "regenerate after structural
change" isn't actually wired up yet.

### The punch list, worked the same day

Every actionable gap this audit found — 2.4, 3.2, 3.5's inconsistency, 3.6,
4.2 and 6.3's filtering half — was implemented on 2026-09-02 by seven parallel
agents, each owning a disjoint set of files, each verifying against
`--settings=OMS.test_settings` (in-memory SQLite, every outbound SAP/HANA/push
call mocked — the same invocation CI runs) before and after its change. No
live database, SAP Service Layer, HANA or NIC endpoint was contacted, no
server or scheduler was started, and no schema changed.

Consolidated verification afterward, on the settled tree: **815 tests, OK, 0
failures, 0 errors** (10 skipped, all pre-existing); `manage.py check` reports
the same single pre-existing `invoice.CreditLimitLogs` FK warning it did
before; `makemigrations --check --dry-run` reports **"No changes detected"**,
confirming no model or migration drift.

The per-item rows above record what each one actually did, including the
judgment calls where an agent deliberately did *less* than the plan's literal
wording asked — 2.4 inventing no new role restrictions, 3.6 refusing to merge
two helpers that turned out to genuinely differ, 6.3 leaving the pre-existing
hand-written filters alone. Those restraints are the useful part of the
record; a future reader should not "finish" them without first re-deriving why
they were left.

### What this leaves

1. **1.3 and 4.4** — unchanged, and correctly so. Both are blocked on a human
   action, not on anything code can fix: rotating `SECRET_KEY`/`JWT_SIGNING_KEY`
   is the operator's own task (the code path is ready; the working `.env` sets
   neither, so the app still runs on the compromised dev fallback), and moving
   `tracker` to its own schema is paused under the standing no-database-
   structural-changes constraint.
2. **3.5's actual migration** — both legacy `orders.Notification` endpoints are
   now consistently instrumented with deprecation headers and usage logging, so
   the data exists to make the call. Whether and when to move traffic to
   `notifications.Notification` is an operator decision; nothing is in progress.
3. **A real bug found in passing, not fixed** (out of scope for the item that
   surfaced it, but worth its own task): `sap_sync.PartySerializer` declares an
   `addresses` nested field, but migration `0004_alter_party_options_...`
   removed the `party` FK that relation depended on back in 2026-02 — so
   `PartyDetailView`, `PartyByCodeView` and `GetPartyByCategoryView` raise on
   every real call. No test covered them, which is why it went unnoticed.
4. **Two latent issues recorded above rather than fixed**: `einvoice/sap.py`'s
   TLS verify helper ignoring `HANA_SSL_CA_BUNDLE` (inert only while that
   setting is unset), and the circular-import fragility between
   `orders/services/*` and `orders/views/_shared` (never fires in practice
   because the URLconf loads the views package first).
5. **The §8.1 problem 3.2 did not touch** — 71 sites keying business logic off
   mutable status *names* and hardcoded status ids. The extraction moved that
   code intact rather than fixing it, deliberately: it is its own project, with
   its own risk profile, and it needs the characterization tests 3.2 just added
   as its foundation.
