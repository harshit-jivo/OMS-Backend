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

---

## Phase 5 — Operations *(~1 week)*

| # | task |
|---|---|
| 5.1 | Move APScheduler out of the web process into a dedicated worker (or Celery) — today jobs duplicate under multiple workers |
| 5.2 | Structured logging with request IDs; the `audit` middleware already gives a hook |
| 5.3 | Health/readiness endpoints covering Postgres, HANA and Service Layer |
| 5.4 | Error tracking (Sentry or equivalent) |
| 5.5 | Externalise remaining hardcoded hosts (`SAP_DB_HOST`, `JSAP_DB_HOST` have IP defaults in `settings.py`) |

---

## Phase 6 — API contract *(~1 week)*

| # | task |
|---|---|
| 6.1 | Publish the `drf-spectacular` schema and keep it in CI |
| 6.2 | Version the API (`/api/v1/`) before changing response shapes |
| 6.3 | Standardise pagination, filtering, ordering via `django-filter` |
| 6.4 | Deprecation policy for the mobile client — `devices.VersionPolicy` already provides the enforcement mechanism |

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
