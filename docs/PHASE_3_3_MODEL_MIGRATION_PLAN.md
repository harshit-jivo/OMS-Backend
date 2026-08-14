# Phase 3.3 — Notification Persistence Migration Plan

> **AUDIT + PLAN ONLY.** No application code, model, migration, or database was
> changed for this task. Every claim is tagged **[EVIDENCE]** (verified in the
> repo, file:line given), **[PROPOSED]** (design recommendation), or
> **[UNCONFIRMED — HUMAN DECISION REQUIRED]**.

---

## 1. Current model

**[EVIDENCE]** `orders/models.py:306-378`.

### `Notification` (`orders/models.py:306`)
| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | (implicit) `AutoField` | no | auto | **inherited** — no explicit PK |
| `user` | `FK(users.User, CASCADE)` `related_name='notifications'` | no | — | recipient |
| `order` | `FK(orders.Order, CASCADE)` `related_name='notifications'` | **no (non-nullable)** | — | the coupling to remove later |
| `message` | `TextField` | no | — | body |
| `is_read` | `BooleanField` | no | `False` | read state |
| `created_at` | `DateTimeField(auto_now_add=True)` | no | now | timestamp |

`Meta`: `db_table='notifications'`, `ordering=['-created_at']`, indexes
`notif_user_created_idx (user,-created_at)`, `notif_user_isread_idx (user,is_read)`.

### `PushToken` (`orders/models.py:327`)
`id = AutoField(primary_key=True)` **explicit**; `user FK(User,CASCADE)`;
`token CharField(255, unique=True)`; `platform CharField(20, blank, default='')`;
`is_active Bool(default=True)`; `created_at`; `updated_at (auto_now)`.
`db_table='push_tokens'`, index `pushtoken_user_active_idx (user,is_active)`. **No business FK** (User only).

### `WebPushSubscription` (`orders/models.py:348`)
`id` implicit `AutoField`; `user FK(User,CASCADE)`; `endpoint TextField(unique=True)`;
`p256dh/auth CharField(255)`; `user_agent CharField(255, blank)`; `is_active`;
`created_at`; `updated_at`. `db_table='web_push_subscriptions'`, index
`webpush_user_active_idx (user,is_active)`. **No business FK** (User only).

## 2. Current database tables

**[EVIDENCE]** Table names are pinned in `db_table` (Meta), so they are stable
regardless of app ownership:

| Model | `db_table` | Business FK | User FK |
|---|---|---|---|
| Notification | `notifications` | `order` → `orders` (`orders`.id) | yes |
| PushToken | `push_tokens` | none | yes |
| WebPushSubscription | `web_push_subscriptions` | none | yes |

Indexes as listed in §1 (all created by prior Orders migrations; **not** recreated by this plan).

## 3. Current PK types

**[EVIDENCE]** All three are **32-bit integer** PKs:
- `PushToken.id` — **explicit** `AutoField`.
- `Notification.id`, `WebPushSubscription.id` — **inherited** `AutoField` (orders sets no `default_auto_field`; `OMS/settings.py:298` `DEFAULT_AUTO_FIELD` note — verified project default is AutoField).

**Preservation:** `notifications/apps.py` already pins
`default_auto_field = 'django.db.models.AutoField'` **[EVIDENCE]**. When the
models are declared in `notifications/`, they inherit **AutoField**, matching the
live columns → **no `ALTER TABLE … TYPE bigint`**. This is the single most
important PK guarantee and it is already in place from Phase 3.1.

> If the app had defaulted to `BigAutoField` (like payments/approvals/core), the
> state move would have emitted an `AlterField` → a real bigint rewrite of three
> tables and every FK pointing at them. That is structurally prevented.

## 4. Current dependencies (blast radius)

**[EVIDENCE]** Every reference to the three models:

| File | Reference |
|---|---|
| `orders/models.py` | definitions; imports `from users.models import User` (`:2`); uses `Order` class directly |
| `orders/notifications.py:31` | `from .models import Notification, PushToken`; creates/updates Notification; queries PushToken |
| `orders/webpush.py` | `WebPushSubscription` queries |
| `orders/serializers.py:2` | imports `Notification`; `NotificationSerializer` |
| `orders/views.py:5` | imports all three; `NotificationListView`, `PushTokenView`, `WebPushSubscriptionView` |
| `orders/admin.py` | imports + registers all three (read-only) |
| `orders/management/commands/prune_push_tokens.py:42` | `from orders.models import PushToken` |
| `orders/management/commands/prune_web_push_subscriptions.py:44` | `from orders.models import WebPushSubscription` |

**Implication:** the ownership move must keep every one of these imports working.
The clean way is a **compatibility re-export** in `orders/models.py`
(`from notifications.models import Notification, PushToken, WebPushSubscription`).
That makes `orders` import `notifications` — **allowed** (business → framework).
The reverse must never happen (see §6, §10 crux).

## 5. GenericForeignKey pattern in this project

**[EVIDENCE]** `approvals.ApprovalRequest` (`approvals/models.py:139-190`) is the
proven pattern to reuse:
- `content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)`
- `object_id = models.PositiveBigIntegerField()`
- `document = GenericForeignKey('content_type', 'object_id')`
- **Denormalised** `company CharField(20, choices=CATEGORY_CHOICES, db_index=True)`,
  `amount Decimal`, `document_number CharField(50)` — comment (`:157`): *"Denormalised
  at submit so the inbox can filter and sort without joining to the polymorphic
  target (impossible in one SQL query)."*
- Index `idx_apreq_target (content_type, object_id)`.

`object_id` is `PositiveBigIntegerField` — it holds any positive integer PK
(Order's AutoField values included), so the GFK imposes **no** PK-type change on
target tables. **[EVIDENCE]**

## 6. Proposed target `Notification` model

**[PROPOSED]** Declared in `notifications/models.py`, reusing the approvals
pattern. **String references** (`settings.AUTH_USER_MODEL`, `'orders.Order'`)
keep the framework from *importing* any business module (the AST boundary test
stays green):

| Field | Type | Null | Default | Index | Purpose | Back-compat |
|---|---|---|---|---|---|---|
| `id` | `AutoField` (inherited) | no | auto | PK | preserve int PK | unchanged |
| `user` | `FK(settings.AUTH_USER_MODEL, CASCADE)` | no | — | existing | recipient | unchanged (was `User` class import) |
| `order` | `FK('orders.Order', CASCADE)` | **no → later nullable** | — | existing | **transitional** back-compat | **KEEP in 3.3** (`order_id`, §11) |
| `message` | `TextField` | no | — | — | body | unchanged |
| `is_read` | `BooleanField` | no | `False` | existing | read state | unchanged |
| `created_at` | `DateTimeField(auto_now_add)` | no | now | existing | timestamp | unchanged |
| `content_type` | `FK('contenttypes.ContentType', PROTECT)` | **yes** | null | `(content_type,object_id)` | polymorphic target | additive; null for old rows until backfill |
| `object_id` | `PositiveBigIntegerField` | **yes** | null | ↑ | target PK | additive |
| `entity` | `GenericForeignKey('content_type','object_id')` | n/a | — | — | resolve target | additive |
| `company` | `CharField(20, blank, default='', db_index=True)` | see §7 | `''` | db_index | company scope | additive; **see UNCONFIRMED** |
| `event_type` | `CharField(50, blank, default='', db_index=True)` | yes/blank | `''` | db_index | maps to `constants.EVENT_NAMES` | additive |
| `title` | `CharField(255, blank, default='')` | blank | `''` | — | notification title | additive (payload already has one) |

**Deliberately excluded (keep minimal):** `amount`, `document_number`, `module`,
per-channel delivery status, preferences — not needed by the current inbox query
(`NotificationListView` returns `id, message, is_read, created_at, order_id`).
Add only when a consumer needs them.

**Two migrations, not one** (see §10): (a) state-only ownership move, then
(b) additive columns. Never combined.

## 7. Company scoping — **UNCONFIRMED — HUMAN DECISION REQUIRED**

**[EVIDENCE]** Company is represented **two incompatible ways** in the repo:
- Payments/Approvals: `company = CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)` — the **SAP category string** (`approvals/models.py:158`, `payments/models.py:44/171/454`). Comment: *"the 'company' IS the category, maps 1:1 to a SAP company DB."*
- Users: `User.company = models.ForeignKey(...)` (`users/models.py:215`) — a **Company FK**.

**The unresolved questions:**
1. **Which representation** should `Notification.company` use — the `CharField`
   category (consistent with the approval inbox it will sit beside) or a FK
   (consistent with `User.company`)? These are not interchangeable.
2. **Backfill source.** The existing `notifications` table has **no company
   column and no company data**. An Order has no `CATEGORY_CHOICES` company in
   the same sense. So for the existing rows there is **no obvious safe source**.

**[PROPOSED] safe interim:** `company = CharField(20, blank, default='', db_index=True)`
(matches the approvals convention, nullable-by-blank), **not backfilled** in 3.3
(existing rows stay `''`). New rows populate it when the dispatcher phase resolves
recipients. **This defers the hard decision without blocking the state move.**

> **STOP gate (§6 of the task):** the *authoritative* company representation and
> whether/how to backfill existing rows require a human decision before any
> company-dependent logic is built. The interim blank column is safe; the
> semantics are not yet decided.

## 8. Denormalized fields

**[PROPOSED]** Only what the inbox genuinely needs (mirroring the approvals
rationale — avoid joining the polymorphic target):

| Field | Why | Populated by | Null | If source changes |
|---|---|---|---|---|
| `company` | inbox company isolation without a target join | dispatcher at create (future) | yes (see §7) | snapshot — not re-synced |
| `title` | list display without loading the target | producer at create | blank | snapshot |
| `event_type` | filter/group by event without parsing message text | producer at create | blank | immutable once set |

`amount` / `document_number` (used by approvals) are **not** added — the
notification inbox does not display them today. Denormalized fields are
**snapshots**: if the source object later changes, the notification keeps what it
said at send time (correct — a historical record must not mutate).

## 9. Data migration strategy

**[PROPOSED]** Existing rows (historical **337**; **[UNCONFIRMED-CURRENT]** —
measure read-only, §12) are preserved in place:
- The state move touches **no rows**.
- The additive columns are **nullable/blank**, so existing rows are valid with
  `content_type=NULL, object_id=NULL, company='', event_type='', title=''`.
- **Optional backfill (data migration, reversible):** for existing Order
  notifications set `content_type = ContentType(orders.order)` and
  `object_id = order_id` (both already known from the retained `order` FK). This
  makes old rows GFK-resolvable **without** deleting/duplicating anything. `order`
  FK is **retained** as the compat source and is **not** dropped in 3.3.
- **Never**: delete rows, recreate the table, duplicate rows, or drop `order`.

Preserved for every row: recipient, read state, message, `created_at`, order
reference, `order_id`.

## 10. `SeparateDatabaseAndState` strategy — the crux

**[PROPOSED]** Appropriate and safe **for the ownership move**, because the
physical tables already exist and must not be rebuilt.

- **State operation** (`state_operations`): `DeleteModel` in `orders` state +
  `CreateModel` in `notifications` state for each of the three models. Django's
  *idea* of ownership moves; **no SQL is emitted**.
- **Database operation** (`database_operations`): **empty** — the tables,
  indexes, PKs, FKs and rows are physically untouched.
- **Why tables remain untouched:** `db_table` is pinned identically
  (`notifications`/`push_tokens`/`web_push_subscriptions`), PKs stay `AutoField`
  (§3), and `database_operations=[]` means Django runs no DDL.
- **The dependency crux:** the moved `Notification` still needs an `order` FK and
  a `user` FK. Declared as **string references** — `models.ForeignKey('orders.Order', …)`
  and `models.ForeignKey(settings.AUTH_USER_MODEL, …)` — so `notifications/models.py`
  performs **no `import orders` / `import users`** (AST boundary test stays green).
  The model-graph FK to `orders.Order` is a **transitional coupling**, explicitly
  sanctioned by the blueprint to be removed at the Orders cutover (Phase 3.5), not now.
- **Rollback:** reverse the `SeparateDatabaseAndState` (state-only) → ownership
  returns to `orders`, still zero DDL. Trivially reversible.

> **[UNCONFIRMED — HUMAN DECISION REQUIRED]** Is a *transitional model-graph FK*
> from `notifications` → `orders.Order` (string ref, no Python import) acceptable
> under Rule 1 for phases 3.3–3.5? The blueprint says yes (temporary, removed at
> cutover); confirm before implementing.

## 11. Migration SQL rehearsal

**[PROPOSED]** In a disposable env, prove the state move emits no DDL:
```bash
# ownership move (must show NO SQL / only comments):
python manage.py sqlmigrate notifications 000X_move_models
# additive columns (must be only ADD COLUMN ... NULL + CREATE INDEX):
python manage.py sqlmigrate notifications 000Y_add_gfk_company
```
Assert the first prints **no** `CREATE TABLE`, `DROP TABLE`, `ALTER … TYPE`, or
`ALTER … DROP CONSTRAINT`; the second prints only additive `ADD COLUMN` (nullable)
and `CREATE INDEX`.

## 12. PostgreSQL dump rehearsal

**[PROPOSED]** Mandatory — `OMS/test_settings.py` **disables migrations** (SQLite,
CREATE-from-models), so the SQLite suite proves *model* behaviour, **not**
*migration* correctness. Rehearsal:
```bash
# 1. restore a recent prod dump into a DISPOSABLE db (never prod)
createdb oms_rehearsal && pg_restore -d oms_rehearsal <dump>
# 2. record baseline (READ-ONLY count — the "if safe" measurement)
psql -d oms_rehearsal -c "SELECT count(*) FROM notifications;"
psql -d oms_rehearsal -c "SELECT count(*) FROM push_tokens;"
psql -d oms_rehearsal -c "SELECT count(*) FROM web_push_subscriptions;"
# 3. apply + inspect
python manage.py migrate --database=oms_rehearsal
# 4. verify counts UNCHANGED, sample old rows intact, order_id preserved
psql -d oms_rehearsal -c "SELECT count(*) FROM notifications;"   # == baseline
psql -d oms_rehearsal -c "SELECT id,user_id,order_id,is_read,created_at FROM notifications ORDER BY id LIMIT 5;"
```
Pass criteria: no table drop/recreate, no PK type change, no FK dropped, counts
identical, representative old rows readable, `order_id` present.

## 13. API compatibility

**[EVIDENCE]** The contract is `NotificationSerializer`
(`fields=['id','message','is_read','created_at','order_id']`, `order_id` sourced
from the local column) and the endpoints (`orders/urls.py:41-46`):
`/api/orders/notifications/`, `…/history/`, `…/<pk>/`, `push-token/`,
`web-push/public-key/`, `web-push/subscribe/`.

**[PROPOSED]** Phase 3.3 changes **none** of these. The model move + additive
columns leave the serializer fields, URLs, status codes, pagination, and
`unread_count` byte-identical. `order_id` keeps reading the retained `order`
column. **No API change in 3.3.**

## 14. Mobile compatibility

**[EVIDENCE]** Pinned by `orders/tests_notifications.py` payload-contract tests:
keys `notification_id, order_id, screen, …`, `screen=="notifications"`, additive
web payload. **[PROPOSED]** `order_id` **must not be removed in 3.3** (§16); the
`order` column stays → payload unchanged. Compatibility period: `order_id` is kept
through Phase 3.5 (Orders cutover) **and** at least one mobile release cycle after
the framework is authoritative — **[UNCONFIRMED]** exact number of releases →
product decision.

## 15. Admin compatibility

**[EVIDENCE]** `orders/admin.py` registers all three via `_ReadOnlyAdmin`
(`NotificationAdmin` uses `list_select_related=('user','order')`,
`search_fields=(…, 'order__order_number')`).

**[PROPOSED future — not now]** When ownership moves, either (a) move the admin
registrations to `notifications/admin.py` importing from `notifications.models`,
or (b) keep them in `orders/admin.py` via the compat re-export (§4). The
`order`-based `list_select_related`/`search_fields` keep working while the `order`
FK is retained. No admin change in Phase 3.3 unless the import breaks; if so,
switch the import target only.

## 16. Testing strategy

**[PROPOSED]** SQLite suite (behaviour): model behaviour, GFK resolution (old
Order rows), old-row vs new-row read paths, recipient isolation, `order_id`
compat, PushToken/WebPushSubscription unchanged. **Must run against a restored
PostgreSQL dump** (not SQLite): the migration itself, no-DDL assertion
(`sqlmigrate`), row-count parity, PK-type preservation, index preservation.
Existing `notifications` 13/13 and `orders` 46/46 must stay green throughout;
patch the callable actually used (no shim targets).

## 17. Rollback strategy

**[PROPOSED]**
- **After state move only:** reverse migration → ownership back to `orders`, zero
  DDL, zero data change. Fully reversible.
- **After additive columns:** reverse drops the nullable columns/index (additive,
  so reversible); existing data intact (columns were nullable).
- **After optional backfill:** backfill is reversible (set the new columns back to
  NULL) — but simplest rollback is to reverse the additive migration.
- **After app deploy referencing new fields:** roll back the deploy first, then
  the migrations. Because every step is additive/state-only, **no backup restore
  is required** for rollback; the dump is kept only as a rehearsal/DR safety net.
- **Honest classification:** the state move and additive columns are **fully
  reversible**. The `order` FK removal (Phase 3.5, *not here*) would be the first
  non-trivially-reversible step and is explicitly out of scope.

## 18. Deployment sequence

**[PROPOSED]** (all inside Phase 3.3; Orders cutover is 3.5)
```
1. Declare 3 models in notifications/models.py (string FKs, AutoField).
2. Compat re-export in orders/models.py  →  from notifications.models import …
3. Migration A: SeparateDatabaseAndState ownership move (state only, no DDL).
4. Migration B: additive nullable content_type/object_id/company/event_type/title
   + (content_type,object_id) index.
5. (optional) Migration C: data backfill content_type=Order, object_id=order_id.
6. Rehearse on prod dump (§11,§12) → verify no-DDL + count parity.
7. Deploy. order FK + order_id RETAINED. APIs/payloads unchanged.
```

## 19. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | Accidental `bigint` PK rewrite | HIGH | `AutoField` already pinned (§3); assert via `sqlmigrate` |
| 2 | `notifications` imports a business module | HIGH | string FKs (`'orders.Order'`, `AUTH_USER_MODEL`); AST test stays green |
| 3 | State move emits DDL | HIGH | `database_operations=[]`; identical `db_table`; verified by `sqlmigrate` |
| 4 | Company semantics/backfill wrong | HIGH | **UNCONFIRMED (§7)** — blank interim; defer authoritative decision |
| 5 | `order_id`/payload regression | HIGH | keep `order` FK; contract tests; no serializer/URL change |
| 6 | SQLite suite can't prove migration | MED | mandatory prod-dump rehearsal (§12) |
| 7 | Transitional GFK→orders coupling | MED | **UNCONFIRMED (§10)** — sanctioned by blueprint; confirm |
| 8 | Import breakage across 8 files | MED | compat re-export in `orders/models.py` |

## 20. Exact implementation steps (when approved)

Files that implementation **would** modify/create:
1. `notifications/models.py` — declare `Notification`, `PushToken`,
   `WebPushSubscription` (string FKs, AutoField, GFK + additive fields).
2. `notifications/migrations/0001_*` — `SeparateDatabaseAndState` (state only).
3. `notifications/migrations/0002_*` — additive columns + index (+ optional 0003 backfill).
4. `orders/models.py` — **replace** the three class defs with a compat re-export
   `from notifications.models import Notification, PushToken, WebPushSubscription`.
5. `orders/migrations/000N_*` — the `orders` state side of the split (paired with step 2).
6. `notifications/tests.py` — model/GFK/compat tests; keep boundary + registry green.
7. (only if imports break) `orders/admin.py` import target — otherwise untouched.
No change to `orders/views.py`, `orders/serializers.py`, `orders/urls.py`,
`orders/notifications.py`, `orders/webpush.py`, prune commands (they resolve
through the re-export), frontend, or mobile.

## 21. Stop conditions (per task)

Implementation must **STOP for human approval** because these remain open:
- **Company representation + backfill source is UNDECIDED** (§7) — a §6 STOP trigger.
- **Transitional `notifications → orders.Order` FK acceptance** (§10) — needs confirmation.
- **Prod-dump rehearsal environment** must be available before running the migration (§12).
- **Mobile `order_id` compatibility window** (number of releases) is a product decision (§14).

Everything else (PK preservation, no-DDL state move, additive columns, rollback,
API/payload preservation) is **confirmed safe by evidence**.

---

# Final Report

**1. Is Phase 3.3 safe to implement?** **Conditionally yes.** The mechanics —
state-only ownership move (no DDL), additive nullable columns, retained `order`
FK, `AutoField` preservation, reversible rollback, unchanged API/payloads — are
**evidence-safe**. It is **blocked** only on two decisions (company semantics;
transitional-FK acceptance) and one prerequisite (prod-dump rehearsal env).

**2. Confirmed facts:** table names + `db_table` pinning; all three PKs are
integer `AutoField` (notifications app already pins AutoField); `Notification`
has a non-nullable `order` FK; the approvals GFK pattern
(`content_type`+`object_id PositiveBigInteger`+GFK+denormalized+`(ct,oid)` index);
API contract (`id,message,is_read,created_at,order_id` + 6 endpoints); payload
contract tests; read-only admin; the 8-file blast radius.

**3. Unconfirmed decisions:** (a) company representation `CharField(category)` vs
FK, and existing-row backfill source; (b) acceptance of a transitional
model-graph FK `notifications→orders.Order` (string ref, no import); (c) mobile
`order_id` compatibility window; (d) current live row count (measure read-only in
rehearsal; historical 337).

**4. Proposed target model:** §6.

**5. Proposed migration sequence:** §18.

**6. Expected SQL risks:** §19 (chiefly: prevent bigint rewrite — already
mitigated; ensure state move emits zero DDL — verify via `sqlmigrate`).

**7. Rollback:** §17 — state move + additive columns are **fully reversible**; no
backup restore needed for rollback.

**8. Files implementation would modify:** §20.

**9. Proceed or human approval?** **Human approval required** before
implementation, specifically to resolve §7 (company) and confirm §10 (transitional
FK), and to confirm a production-dump rehearsal environment exists. Do **not**
implement Phase 3.3 until those are signed off.
