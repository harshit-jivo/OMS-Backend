# Notification Framework — Phase 3 Implementation Blueprint

> **Status of this document.** The original historical Phase 3 blueprint does
> **not exist** anywhere in the repository (searched `docs/`, repo root, parent
> dir, all `.md/.txt/.py`, and full git history — no file or commit defines a
> "Phase 3.2" scope). This document is therefore a **reconstruction** from (a)
> direct repository evidence and (b) the approved architecture decisions supplied
> for the reconstruction. It is **not** a recovered original.
>
> Every claim is tagged:
> - **[EVIDENCE]** — verified in the repository (file/line given).
> - **[RECONSTRUCTED]** — inferred from the approved decisions; internally
>   consistent but not proven by an artifact.
> - **[UNCONFIRMED — REQUIRES REVIEW]** — cannot be established from either;
>   needs a human decision before the relevant phase starts.
>
> No application code, model, migration, or registry was created for this task.

---

## 1. Current State

**[EVIDENCE]** The production notification system lives in the `orders` app.

| Concern | Location | Detail |
|---|---|---|
| Notification model | `orders/models.py:306` | `db_table='notifications'`; FK `user` (`related_name='notifications'`); **non-nullable** FK `order` (`related_name='notifications'`); fields `message, is_read, created_at`; indexes `(user,-created_at)`, `(user,is_read)`. **No explicit PK → inherits AutoField.** |
| Push tokens | `orders/models.py:327` | `db_table='push_tokens'`; **explicit** `id = AutoField(primary_key=True)`; `token` unique. |
| Web push subs | `orders/models.py:348` | `db_table='web_push_subscriptions'`; FK `user`; `endpoint` unique. **No explicit PK → inherits AutoField.** |
| Send logic | `orders/notifications.py` | Expo mobile push + fan-out (`deliver_notification_to_many`, line 480). |
| Web push | `orders/webpush.py` | VAPID Web Push, immediate 404/410/401/403 pruning. |
| API | `orders/views.py`, `orders/urls.py` | Endpoints under `/api/orders/notifications/…`. |
| New app | `notifications/` | Phase 3.1 skeleton: `apps.py`, empty `models.py`, `migrations/__init__.py`, `tests.py`. Imports no business module. |

**[EVIDENCE]** New notifications app (`notifications/`): registered in
`INSTALLED_APPS` (`OMS/settings.py:101`), `default_auto_field = AutoField`
(`notifications/apps.py`), `models.py` empty. `makemigrations --check` →
"No changes detected".

**[EVIDENCE]** Orders remains the production source of truth; the new app owns
no behaviour yet.

## 2. Approved Architecture

**[RECONSTRUCTED from approved decisions]** A reusable notification framework
that any business module (Orders, Payments, Deposits, Approvals, Inventory,
Invoices, Users, future) can publish events to **without modifying the
framework**. A business module says *what happened*; the framework decides *who
receives it, what is created, which channel, how it's delivered, and how the
client navigates*.

Channels: Mobile Push + Web Push today; Email, WhatsApp, SMS, Slack, Teams later
— all behind the same dispatcher seam.

## 3. Dependency Direction

**[RECONSTRUCTED]** Mandatory, one-way:

```
Orders ─┐
Payments├─┐
Deposits│ │
…       │ ├──►  notifications/  ──►  ┌ Mobile Push
Approvals─┘                          ├ Web Push
                                     ├ Email (future)
                                     ├ WhatsApp (future)
                                     └ SMS / Slack / Teams (future)
```

`notifications/` must import **none** of: orders, payments, approvals,
inventory, invoices, users. **[EVIDENCE]** Enforced today by
`notifications/tests.py::test_notifications_imports_no_business_module` (AST
scan) and green.

**[EVIDENCE]** The project already inverts this dependency elsewhere: the
approvals engine holds a registry (`approvals/services.py:36 _HOOKS = {}`) and
business modules register **into** it from their own `AppConfig.ready()`
(`payments/apps.py:ready()` → `payments/hooks.py:register()`). The notification
framework will reuse this exact pattern (see §5).

## 4. Data Model Migration Strategy

**[EVIDENCE]** Today `Notification.order` is a non-nullable FK — the model is
Orders-only. **[RECONSTRUCTED]** Target model uses:

- `GenericForeignKey` (`ContentType` + `object_id`) — the project already uses
  this successfully in `approvals.ApprovalRequest` **[EVIDENCE-adjacent: named in
  approved decisions; confirm exact field layout at design time]**.
- **Denormalized** fields for efficient inbox queries (avoid a `ContentType`
  join per row): e.g. `entity_type`, `entity_id`, plus display fields already
  polled today.
- **Company scoping** (`company_id`) — required because company isolation is
  pervasive in Payments/Approvals.

**Two-step, both in the model-migration phase (§12 Phase 3.3):**

1. **Ownership move — state only.** `SeparateDatabaseAndState` transfers the
   three models from `orders` to `notifications` with **zero DDL**: tables
   (`notifications`, `push_tokens`, `web_push_subscriptions`), indexes, PKs, FKs
   and rows are untouched. Safe **only** because the new app keeps
   `AutoField` (see Risk 1).
2. **Schema evolution — additive DDL + backfill.** Add nullable
   `content_type/object_id/entity_*/company_id`; backfill from the existing
   `order` FK; keep `order` as a temporary compatibility column; make the new
   fields authoritative later. `Notification.order` is dropped **only after**
   Orders cutover (§16), never in the same step.

**[UNCONFIRMED — REQUIRES REVIEW]** Exact new field names/types, nullability
timeline, and whether company is derived from the entity or stored directly.

## 5. Event and Registry Strategy

**[EVIDENCE]** Reuse the in-repo pattern, do **not** invent a new one:

- Framework side (mirrors `approvals/services.py`): `notifications/` owns a
  registry (`_HOOKS`/handlers dict) keyed by event type, mapping
  `event → recipient-resolver + notification-builder + channel-policy`.
- Business side (mirrors `payments/apps.py` + `payments/hooks.py`): each module
  registers its event handlers from its own `AppConfig.ready()`. The framework
  imports nothing; modules import the framework.

**[RECONSTRUCTED]** Public API (future, not now):
`notify("PAYMENT_APPROVED", payment, actor=user)` invoked from the business
module inside `transaction.on_commit(...)`.

**[UNCONFIRMED — REQUIRES REVIEW]** Whether event *definitions* (names/enums)
live in the framework (`notifications/constants.py`) or are contributed by each
module at registration. Recommendation: framework owns the *shape*; modules own
their *event names* — confirm at Phase 3.2 design.

## 6. Provider/Channel Strategy

**[RECONSTRUCTED]** A `Channel`/provider interface (e.g. `send(subscription,
payload) -> outcome`) with concrete providers wrapping today's logic
(`orders/notifications.py` Expo, `orders/webpush.py` Web Push) **without changing
their behaviour**. Future providers (Email, WhatsApp, SMS, Slack, Teams) plug in
behind the same interface.

**[RECONSTRUCTED]** WhatsApp must model **provider-approved templates**, not
free-form messages: `provider_template_name` + **ordered variables** + provider
delivery constraints. Do not implement now.

## 7. Transaction Strategy

**[EVIDENCE]** `orders/` uses no `transaction.atomic`; `ATOMIC_REQUESTS` is not
enabled. **[RECONSTRUCTED]** Therefore `transaction.on_commit()` currently fires
**immediately** under autocommit. Event publication should still be written as
`transaction.on_commit(lambda: notify(...))` so it becomes correct automatically
once a module wraps its write in `atomic()`. **This is intended future behaviour,
not a fix for a current rollback bug**, and belongs to the cutover/adoption
phases — not to Phase 3.2.

## 8. Performance Strategy

**[EVIDENCE-supplied]** Volume ≈ **337 notifications/month (~11/day)**.
**[RECONSTRUCTED]** Delivery stays **synchronous**; the dispatcher keeps a seam
for async without mandating a queue. **No** Celery/Redis/RabbitMQ/RQ/Dramatiq/
APScheduler now.

**Explicit future queue triggers** (introduce async only when one is met):
- p95 delivery latency > 500 ms, or
- a single event fans out to > 50 recipients, or
- the first paid/external channel (Email/WhatsApp) is added, or
- another measured production requirement.

## 9. Backward Compatibility

**[RECONSTRUCTED]** Migration is incremental, reversible, backward-compatible,
test-protected, production-safe. Existing mobile binaries **cannot** be
force-updated → all payload changes are **additive** (never remove/rename a key).

**[EVIDENCE]** The additive contract is pinned by tests in
`orders/tests_notifications.py` ("Payload contract" §, ~line 128): exact keys
`notification_id, order_id, screen, notification_type, …`, `screen ==
"notifications"`, and `test_web_payload_adds_only_order_number_and_body`. These
must stay green throughout. Existing `/api/orders/notifications/…` endpoints stay
until a later, separately-gated API phase.

## 10. Testing Strategy

**[EVIDENCE]** Baseline (current repo run):
- `notifications` boundary tests: **4/4** (`notifications/tests.py`).
- Orders notification tests: **46/46** (`orders/tests_notifications.py`).
- `manage.py check`: passes (1 unrelated pre-existing `invoice` warning).
- `makemigrations --check`: "No changes detected".
- Test command: `python manage.py test <app> --settings=OMS.test_settings`
  (in-memory SQLite; migrations disabled — see Risk 2).

**[RECONSTRUCTED]** Each future phase adds tests without weakening these. Patch
the callable **actually used** by the code under test (Risk 3).

## 11. Migration Risks

- **Risk 1 — PK type. [EVIDENCE]** All three tables have integer/AutoField PKs
  (`PushToken` explicit; `Notification`/`WebPushSubscription` inherited from
  Orders' AutoField). A conventionally-created app defaults to `BigAutoField`,
  which would emit `ALTER TABLE … TYPE bigint` on live tables + every FK.
  Mitigated **now** by `notifications/apps.py` pinning `AutoField`. The model
  migration must keep it.
- **Risk 2 — Migration test blind spot. [EVIDENCE]** `OMS/test_settings.py`
  disables migrations (CREATE TABLE from models). The suite proves *model*
  correctness, **not** *migration* correctness. The model-migration phase must
  add: production-dump rehearsal, `sqlmigrate` inspection, and an assertion of
  **no destructive DDL** for the state-only step.
- **Risk 3 — Silent test patching. [EVIDENCE — Phase 3.0 rule]** After
  extraction, don't patch a target that resolves to a compatibility shim; patch
  the object the implementation actually calls.
- **Risk 4 — Orders fan-out logging. [EVIDENCE]**
  `orders/notifications.py:deliver_notification_to_many` (line 480) counts
  `targeted`/`saved` and logs them. The migration must preserve this;
  **`saved < targeted` is the cutover failure signal.**

## 12. Eight-Phase Roadmap

> **[UNCONFIRMED — REQUIRES REVIEW]** The prior investigation reportedly named
> "8 phases, ~6–7 weeks", but that investigation is not in the repo. The mapping
> below is a **reconstruction** consistent with the evidence and the existing
> `3.0/3.1/3.2` sub-numbering. Phase *boundaries* are proposals for review; the
> *completed* phases (1–4) are evidence-backed.

Legend: DB/API/FE(frontend)/Mobile impact, Risk.

### Phase 1 — Stabilization & Payload-Contract Lock — **COMPLETE [EVIDENCE]**
- **Objective:** pin the exact notification payloads + delivery logging so later
  extraction is provably behaviour-preserving.
- **Files:** `orders/tests_notifications.py` (contract tests),
  `orders/notifications.py` (`deliver_notification_to_many` targeted/saved log).
- **DB:** none. **API:** none. **FE:** none. **Mobile:** none.
- **Tests:** payload-contract suite. **Rollback:** n/a (tests). **Risk:** low.
- **Exit:** payload keys + `screen` pinned; fan-out log emits targeted/saved.

### Phase 2 — Architecture Decisions & Audit — **COMPLETE (doc form) [UNCONFIRMED]**
- **Objective:** approve dependency direction, GFK+denormalized+company model,
  registry reuse, sync delivery, additive-payload rule.
- **Artifact:** the "Phase 2 architecture document" is **not present as a file**;
  `docs/notification.md` is the closest system reference. Decisions themselves
  are captured (this blueprint §2–§11).
- **DB/API/FE/Mobile:** none. **Risk:** low (doc). **Exit:** decisions approved.

### Phase 3.0 — Test Safety-Net & Patch-Hardening — **COMPLETE [EVIDENCE]**
- **Objective:** runnable test env without the prod DB; harden patch targets.
- **Files:** `OMS/test_settings.py` (SQLite, skip-migrations). Patch-hardening rule.
- **DB/API/FE/Mobile:** none. **Risk:** low.
- **Exit:** `manage.py test … --settings=OMS.test_settings` runs green.

### Phase 3.1 — Notifications App Boundary — **COMPLETE [EVIDENCE]**
- Commits **`d47af29`**, **`b0c4a0b`**. App created, registered, empty models,
  AutoField, no business imports. **4/4** boundary tests, **46/46** orders tests,
  no migration. **DB/API/FE/Mobile:** none. **Risk:** low. **Exit:** met (§13).

### Phase 3.2 — Framework Foundation — **CANDIDATE / SCOPE REQUIRES CONFIRMATION**
- **Objective [RECONSTRUCTED]:** the framework's internal, behaviour-free
  foundation so later phases have stable seams — **no models, no migration, no
  business coupling**.
- **Candidate scope (see §14):** `notifications/constants.py` (channels + event
  shape), `notifications/registry.py` (`_HOOKS`-style, mirroring approvals),
  wire empty registry from `notifications/apps.py:ready()`, provider/service
  interface stubs, tests extending the current 4.
- **Files:** new files inside `notifications/` only.
- **DB:** none (must stay "No changes detected"). **API:** none. **FE:** none.
  **Mobile:** none.
- **Tests:** extend boundary tests (registry importable, still no business
  imports, still no models). **Rollback:** delete new files (pure addition).
- **Risk:** low–medium (only if scope creeps into 3.3+). **Exit:** foundation
  imports cleanly, zero DB/behaviour change, baseline still green.

### Phase 3.3 — Notification Model Migration — **FUTURE**
- **Objective:** move `Notification`, `PushToken`, `WebPushSubscription` into
  `notifications/` and evolve the schema to GFK + denormalized + company.
- **Files:** `notifications/models.py`, `notifications/migrations/*`,
  `orders/migrations/*` (state-side of the split).
- **DB:** **(a)** `SeparateDatabaseAndState` ownership move — **zero DDL**;
  **(b)** additive columns (`content_type/object_id/entity_*/company`) + backfill;
  `Notification.order` retained as compat, dropped only post-cutover.
  **API:** none. **FE:** none. **Mobile:** none.
- **Tests:** model tests + **migration rehearsal on a restored prod dump** +
  `sqlmigrate` review asserting no destructive DDL in step (a). **Rollback:**
  reverse migrations; step (a) is state-only so trivially reversible; keep dump.
- **Risk:** **HIGH** (the concentration point — Risks 1 & 2). **Exit:** tables/
  PKs/rows identical after (a); additive columns present + backfilled after (b);
  all payloads unchanged.

### Phase 3.4 — Dispatcher, Event API & **Payments Adoption Gate** — **FUTURE**
- **Objective:** implement `notify(event, entity, actor)`, recipient resolver,
  channel dispatch (wrapping existing Expo/WebPush providers), `on_commit`
  publication; **Payments becomes the first real consumer** as the abstraction
  test.
- **Files:** `notifications/` (dispatcher/resolver/providers); `payments/`
  (register handlers via `apps.ready()`/`hooks.py`) — **Orders untouched here**.
- **DB:** none new (uses 3.3 schema). **API:** none (existing endpoints stay).
  **FE:** none. **Mobile:** none (payloads additive only).
- **Tests:** Payments notification tests + framework unit tests; **Gate:** see §17.
- **Rollback:** feature-guard Payments publication; framework additions are inert
  until called. **Risk:** medium. **Exit:** Payments emits notifications with
  **zero edits inside `notifications/`**; Orders tests still 46/46.

### Phase 3.5 — Orders Cutover (**LAST**) — **FUTURE**
- **Objective:** Orders stops owning notification business logic and publishes
  events through the framework instead; drop `Notification.order` after parity.
- **Files:** `orders/notifications.py`, `orders/views.py`, `orders/urls.py`
  (compat shims), `orders/models.py` (`order` FK removal — last).
- **DB:** drop `Notification.order` column (final, additive-safe timeline).
  **API:** endpoints preserved via shims until a separately-gated API phase.
  **FE/Mobile:** unchanged (payloads/contracts preserved).
- **Tests:** full `orders.tests_notifications` (46/46) + fan-out
  `saved == targeted` gate. **Rollback:** shim keeps old path callable; revertable
  until the FK drop, which is the point of no return (gated on green parity).
- **Risk:** **HIGH.** **Exit:** all Orders notifications flow through the
  framework; contract + fan-out tests green; payloads/APIs unchanged.

> **Post-Phase-3 (Phase 4+, out of scope here):** Email, then WhatsApp
> (templates), then SMS/Slack/Teams; optional async queue once a §8 trigger fires;
> optional `/api/notifications/` API + frontend/mobile migration to it.

## 13. Phase 3.1 Status — **COMPLETE**

**[EVIDENCE]** Commits **`d47af29`** (structure) and **`b0c4a0b`** (boundary
tests). App exists + registered; `models.py` empty; no DB change; no
business-module imports; **4/4** notifications tests; **46/46** Orders tests;
`makemigrations --check` passes; Orders/frontend/mobile untouched. Do not
redefine.

## 14. Phase 3.2 Scope

**Status: REQUIRES CONFIRMATION from the reconstructed blueprint.** No repository
artifact assigns a concrete scope to Phase 3.2.

**Proposed candidate scope** (smallest foundation, evidence-aligned, no 3.3+ work):
1. `notifications/constants.py` — channel identifiers + event-shape constants
   (no business names hard-coded).
2. `notifications/registry.py` — a registry mirroring `approvals/services.py`
   `_HOOKS`, empty, with `register(...)`/lookup.
3. `notifications/apps.py::ready()` — wire the (empty) registry; still imports no
   business module.
4. Provider/service **interface** stubs (abstract seam only; existing Expo/
   WebPush logic stays in `orders/` until 3.4).
5. Tests: registry importable + still zero business imports + still zero models +
   `makemigrations --check` clean.

**Explicitly NOT in 3.2:** any model, migration, `GenericForeignKey`,
`ContentType`, company field, working dispatcher, recipient resolver, Payments/
Orders integration, API, or channel implementation.

**[UNCONFIRMED — REQUIRES REVIEW]** Whether all five items belong to 3.2 or some
defer to 3.4. Awaiting approval.

## 15. Model Migration Safety Gate (Phase 3.3)

Do not proceed unless **all** hold:
- `sqlmigrate` of the ownership step shows **no** `CREATE/ALTER/DROP TABLE` and
  **no** `ALTER … TYPE` (state-only).
- `AutoField` preserved on all three tables (no bigint rewrite — Risk 1).
- Rehearsal on a **restored production dump** succeeds and is reversible.
- Row counts + PKs identical pre/post ownership move.
- Additive columns are nullable/backfilled; `Notification.order` retained.
- All payload-contract tests green.

## 16. Orders Cutover Safety Gate (Phase 3.5)

- Orders touched **last**; compatibility shims keep old call sites + endpoints
  working during transition.
- `deliver_notification_to_many` fan-out preserved; **`saved == targeted`** on
  the cutover path (any `saved < targeted` blocks cutover — Risk 4).
- Mobile payloads unchanged (additive only); existing APIs unchanged.
- `Notification.order` dropped **only** after parity is green; that drop is the
  gated point of no return.

## 17. Payments Adoption Gate (Phase 3.4) — **MOST IMPORTANT ACCEPTANCE GATE**

When Payments adopts the framework:

> **If enabling Payments notifications requires ANY edit inside `notifications/`,
> STOP.** The abstraction is wrong. Fix the framework, **re-run Orders tests
> (46/46)**, then continue.

The framework must be reusable without changing itself per module. Re-apply the
same rule when the **third** module (e.g. Deposits/Approvals) is added.

## 18. Future Email / WhatsApp Strategy

**Not now.** Added post-Phase-3 behind the same provider seam, requiring **no**
business-module changes.
- **Email:** first paid/external channel → also a §8 queue trigger.
- **WhatsApp:** provider-approved **templates** only — model
  `provider_template_name` + **ordered variables** + provider delivery
  constraints; no arbitrary free-form business-initiated messages.

## 19. Definition of Done (Phase 3 overall)

- All notification persistence owned by `notifications/`; `notifications/` imports
  no business module (test-enforced).
- Business modules publish via `notify(...)`; adding a module needs **zero** edits
  inside `notifications/` (Payments gate proven, ideally a third module too).
- Orders cut over **last**; `Notification.order` removed only after green parity.
- Payloads/APIs unchanged for existing mobile/web clients throughout (additive
  contract tests green end-to-end).
- No new infra (queue only if a §8 trigger fires); Email/WhatsApp deferred.
- Every phase shipped incrementally, reversibly, test-protected, production-safe.
