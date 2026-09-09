# OMS-Backend architecture

How the system is actually put together — the part a field-by-field model table
cannot tell you. Every structural claim here is grounded in
[`DOMAIN_GRAPH.md`](DOMAIN_GRAPH.md), which is generated from the model
registry, so the numbers are facts rather than impressions.

**98 models · 121 relational edges · 49 crossing app boundaries.**

---

## 1. The system in one picture

```
        React web            React Native (OMS-app, separate repo)
             │                        │
             │            X-Platform header ─┐
             ▼                        ▼      │
      ┌──────────────────────────────────┐   │ HTTP 426 if the build
      │  CorsMiddleware                  │   │ is too old
      │  AuditMiddleware                 │   │
      │  VersionPolicyMiddleware ────────┼───┘
      └───────────────┬──────────────────┘
                      │  JWT (SimpleJWT), 1-day access
             ┌────────▼────────┐
             │   DRF views     │  294 routes
             └────────┬────────┘
                      │
   ┌──────────────────┼───────────────────┬─────────────────┐
   ▼                  ▼                   ▼                 ▼
PostgreSQL      SAP HANA (read)   SAP Service Layer   NIC / Crystal /
order_management  3 company DBs      (write, OData)    JSAP / SMTP / FCM
 public
 payments
 hais
```

Three data planes, and confusing them is the classic failure mode:

| plane | direction | truth about |
|---|---|---|
| PostgreSQL | read/write | OMS's own workflow state |
| SAP HANA | **read only** | master data, stock, pricing, posted documents |
| SAP Service Layer | **write** | creating documents in SAP |

OMS never writes to HANA. Anything that must persist in SAP goes through the
Service Layer, and the result is read back from HANA.

---

## 2. The multi-company rule

**The single most important invariant in the system.**

Jivo runs three SAP companies — Oil, Beverages, Mart — as three separate HANA
databases. `DocEntry` and `DocNum` are *per-company sequences*. The same number
is a different document in each.

Therefore **company travels with every SAP-facing operation**, as a lookup key,
never as a display label. Getting it wrong does not raise an error; it silently
returns or writes the wrong company's data.

It surfaces under several names, all meaning the same thing:

| name | where |
|---|---|
| `category` — OIL / BEVERAGES / MART | `orders`, `users`, party/product assignment |
| `branch` — OIL / BEVERAGE / MART | `invoice.GetPrintReport`, Crystal paths |
| `company` — OIL / BEVERAGES / MART | `payments`, `core.DocumentCounter` |
| `company_db` — the HANA schema name | `payments`, `serviceLayer`, `einvoice` |
| `BPLId` / branch | SAP-side business place |

That five-way naming of one concept is itself a refactor target — but renaming
it is a large, cross-cutting change, and every one of the 149 currently-open
endpoints would need re-testing. Phase 3, not earlier.

---

## 3. Bounded contexts

The 21 apps group into six clusters. This is the shape any extraction or service
split has to respect.

```
┌─ IDENTITY ──────────────────────────────────────────────┐
│  users        User, UserRole, Company, MainGroup, State  │
│  devices      UserDevice, VersionPolicy                  │
│  audit        AuditLog                                   │
└──────────────────────────────────────────────────────────┘
        ▲ 46 of 49 cross-app edges point here
        │
┌─ SELLING ───────────────────┐   ┌─ MASTER DATA ─────────┐
│  orders   Order, OrderItem   │   │  sap_sync  Party,     │
│           Scheme v2 (4)      │◀──│    Product, Branch    │
│           RateApproval       │   │  hana      (queries,  │
│           Parties, Products  │   │             no models)│
│  SKU                         │   │  legal     labels     │
└──────────────────────────────┘   └───────────────────────┘

┌─ MONEY ──────────────────────────────────────────────────┐
│  payments     PaymentReceipt, BankDeposit, SapCallLog     │
│  approvals    generic N-level engine (document-agnostic)  │
│  attachments  files on shared network folders             │
│  core         DocumentCounter (gapless numbering)         │
└───────────────────────────────────────────────────────────┘

┌─ STATUTORY ──────────────────┐   ┌─ WORKFLOW ────────────┐
│  einvoice   IRN, QR           │   │  tracker  8-stage     │
│  ewaybill   EWB               │   │    vendor-invoice     │
│  invoice    history, Crystal  │   │    flow, 15 models    │
│  serviceLayer  SAP client     │   └───────────────────────┘
└───────────────────────────────┘

┌─ PLATFORM ───────────────────────────────────────────────┐
│  notifications  unified fan-out    uilabels  UI strings   │
│  HAIS           asset register (own schema)               │
└───────────────────────────────────────────────────────────┘
```

---

## 4. `users.User` is the hub — and the constraint

**46 of the 49 cross-app relational edges point at `users`.** Every other app
depends on it; it is the one model that cannot be moved.

| from | edges into `users` |
|---|---:|
| `orders` | 18 |
| `tracker` | 9 |
| `approvals` | 7 |
| `invoice` | 4 |
| `payments` | 3 |
| `notifications` | 2 |
| `audit`, `devices`, `attachments` | 1 each |

Most are `created_by` / `updated_by` / `acted_by` audit columns. That is
ordinary and fine — but it means **`users` is a shared kernel**, and no service
split can put `User` on the far side of a network boundary without rewriting
every one of those 45 audit columns.

### 4.1 A circular dependency

```
orders  ──18 edges──▶  users
   ▲                     │
   └──── 2 edges ────────┘
        users.User.category   -> orders.Categories  (FK/PROTECT)
        users.User.categories -> orders.Categories  (M2M)
```

`orders.Categories` is a **single-column table** (`category` CharField) holding
OIL / BEVERAGES / MART. It is the company discriminator from §2 — a
company-wide concept — but it lives in `orders` and `users.User` points at it.

That is why the two apps cannot be reasoned about separately. It is also a cheap
fix relative to its cost: move `Categories` into `users` (or better, `core`) and
the cycle disappears. Worth doing early in Phase 3 because everything else in
that phase is easier once the cycle is gone.

### 4.2 `tracker` is more extractable than the graph suggests — but its docs overstate it

`tracker/docs/TRACKER_SYSTEM.md` and the app's own comments say the tracker is
self-contained with **"no FKs into OMS models"**. The graph says it has **9 FKs
into `users`**:

```
tracker.Invoice.created_by         -> users.User (PROTECT)
tracker.Invoice.deleted_by         -> users.User (SET_NULL)
tracker.StageEvent.acted_by        -> users.User (SET_NULL)
tracker.UserStageAccess.user       -> users.User (CASCADE)
tracker.UserStageAccess.assigned_by-> users.User (SET_NULL)
tracker.AlertNotification.user     -> users.User (CASCADE)
tracker.CashVoucher.created_by     -> users.User (SET_NULL)
tracker.PaymentDetail.updated_by   -> users.User (SET_NULL)
tracker.TransporterPayment.created_by -> users.User (SET_NULL)
```

The claim is true in the sense that matters — **zero FKs into `orders`,
`payments`, `sap_sync` or any business model** — and false as written. Tracker
depends only on identity, which is the shared kernel everything depends on.

So: tracker *is* the most extractable app in the codebase. But anyone reading
"no FKs into OMS models" and planning a lift-and-shift will hit nine of them.
Record it accurately.

---

## 5. Two approval systems

There are **two** approval implementations, and they do not behave the same.

| | `approvals` (generic engine) | order flow (in `orders/views.py`) |
|---|---|---|
| Transactions | `@transaction.atomic` on every transition | none |
| Row locking | `select_for_update()` on the row decided | none |
| Location | `approvals/services.py` (751 LOC) | `UpdateOrderStatusView.post`, ~380 LOC |
| Multi-level | yes, configurable ladder | fixed |
| Document types | any (registered via hooks) | orders only |
| Tests | via `payments` (201) | 0 |

`approvals/services.py` opens by naming the defect in the older one:

> `UpdateOrderStatusView.post` (`orders/views.py:2895`) is neither, across ~380
> lines and several dependent writes — so **two approvers acting at once can
> both pass the pending check and double-advance a document**.

The generic engine decouples itself from domain apps with a hook registry
(`register_hooks` in `apps.py:ready()`), so `approvals` never imports
`payments`. Hooks fire **inside** the approval transaction, which is what makes
"a payment cannot be approved without also being queued for SAP" hold.

**Phase 3 action:** move the order flow onto the engine that already exists. Not
a rewrite — a migration onto working code.

---

## 6. How money reaches SAP

The most safety-critical path in the system, and the best-engineered.
`payments/sap_poster.py` — read it before touching anything in `payments`.

```
approval final rung
        │  hook fires inside the approval transaction
        ▼
  post_document()
        │
        ├─ reload under select_for_update()
        │  refuse if sap_doc_entry set or status POSTED   ← the idempotency guarantee
        │
        ├─ status = POSTING_TO_SAP        (visible; blocks a second attempt)
        ├─ SapCallLog row = STARTED       (every call is logged)
        │
        ▼
   Service Layer POST /IncomingPayments
        │
   ┌────┴──────────────┬───────────────────────┐
   ▼                   ▼                       ▼
 2xx + DocEntry    SAP said no            no reply / no key
   │                   │                       │
 POSTED           PENDING_ERROR            SAP_UNKNOWN
   │                   │                       │
 record            reopen the final        block resubmission,
 DocEntry,         approval rung —         reconcile_unknown()
 DocNum,           nothing was             reads SAP to find out
 TransId           committed               what happened
```

Three invariants a refactor must not break:

1. **One OMS receipt → one SAP payment.** Rests entirely on the
   `select_for_update()` reload immediately before the call.
2. **A rejection and a timeout are different states.** A rejection means nothing
   was committed, so the approval reopens and the approver can retry. A timeout
   means the document *may* exist in SAP — so resubmission is blocked and a
   human or `reconcile_unknown()` decides. **Collapsing these two states
   produces duplicate payments.**
3. **A receipt is `DocType 'C'`, a deposit `DocType 'A'` — both are
   `IncomingPayments`.** The ODPS `Deposits` object is not used by this company.

`core.next_document_number()` allocates receipt/deposit numbers under
`select_for_update()`, daily-scoped per company. Its docstring names what it
replaces: the order-number pattern at `orders/views.py:2518` reads-then-writes
with no lock, so two concurrent creates generate the same number and one 500s.
**That bug is still live in `orders`.**

---

## 7. The scheme engine

`orders/scheme_engine.py` — the discount/free-goods rules, and the model for
what the rest of `orders` should look like: a **pure function**, no writes,
usable from order-create, from the live preview endpoint, and from tests. 45
dedicated tests.

```
resolve_schemes(card_code, category, lines)
   │
   ├─ build_party_context   → card_code, category, state_code, main_group
   ├─ get_candidate_schemes → SchemeAssignment scoping:
   │      ALL < CATEGORY < MAIN_GROUP < STATE < PARTY   (most specific wins)
   │      exclusions are absolute at any scope
   ├─ per line: trigger match (ITEM/SUB_GROUP/VARIETY/BRAND/CATEGORY/ALL)
   ├─ compute_benefit_qty   → (qualifying // per_qty) * free_qty, capped
   ├─ _resolve_conflicts    → priority wins; stackable accumulates
   └─ _apply_pack_factors   → BOX → pieces via the giveaway item's sal_factor2
```

Rules that are not obvious and are easy to destroy:

- **No cross-measure fallback.** A "per 10 boxes" rule reading a piece count
  when `boxes` is blank would give away roughly a carton per box. An
  unmeasurable line simply does not qualify.
- **`qty` vs `qty_pieces`.** Schemes are written in cartons; SAP
  `DocumentLines.Quantity` is *always* pieces. Shipping a BOX benefit
  unconverted under-delivers by the pack size.
- **The pack factor comes from the giveaway item**, not the item that earned it.
- **A benefit with no rule** (`per_qty` and `free_qty` both 0) proposes nothing
  and leaves the quantity to the user. Every scheme migrated from the legacy
  `users.SchemeProduct` looks like this — that is how v2 preserves old behaviour.
- **Three-way category chain** under `strict_category`: party, product line and
  scheme category must all be present and identical.
- **Combos:** `applies_to=FREE_LINE` takes the qualifying quantity from the
  auto-added zero-priced companion line. Companion lines are skipped as triggers
  in their own right, or the same stock counts twice.

Legacy `users.SchemeProduct` still exists alongside v2. `migrate_schemes_v2`
converts it; a scheme with no trigger never fires and needs one typed in.

---

## 8. Statutory: e-invoice

`einvoice` → NIC IRN, `ewaybill` → e-Way Bill. Schema 1.1.

```
invoice in SAP
   │  fetch_invoice_for_irn()   (HANA read, per company)
   ├─ normalize_seller_branch()
   ├─ normalize_buyer_gstin()   ← falls back to CRD1 master addresses
   │                              when INV12.BpGSTN is blank
   ├─ resolve_hsn()
   ▼
mapping.py  → NIC payload
validation.py → pre-submit checks (B2C_NOT_ELIGIBLE etc.)
   ▼
client.py → NIC (token cached)
   ▼
IrnRecord + IrnGenerationLog (every attempt, success or not)
   ▼
qr.py → QR PNG → network share
```

**Validate before sending.** NIC rejections cost money and count against rate
limits, which is why the pre-submit checks exist rather than letting NIC decide.

---

## 9. Unmanaged tables

Several models are `managed = False` — Django will neither create nor migrate
them, and the schema is owned elsewhere. A migration that assumes it controls
these will not do what it appears to.

Notably `orders.Branches` and `sap_sync.Branch` **both map to `branches`**, with
different field sets (`sap_sync.Branch` also has `is_active`, `created_at`,
`updated_at`). Two partial views of one table. One should win — Phase 3.4.

Full list in [`DOMAIN_GRAPH.md`](DOMAIN_GRAPH.md#unmanaged-tables).

---

## 10. What this means for the refactor

Ordered by how much they constrain the plan.

1. **`users` cannot move.** It is the shared kernel; 46 cross-app edges point at
   it. Any service split leaves `User` on the near side.
2. **Break the `users` ↔ `orders` cycle first** by relocating `Categories`.
   Cheap, and everything else in Phase 3 is easier afterwards.
3. **`tracker` is the only genuinely extractable app** — its only outbound
   dependency is identity. If a service split is ever wanted, this is the
   pilot. Its docs' "no FKs into OMS models" needs correcting to "no FKs into
   business models".
4. **`approvals` is already the target architecture.** Do not couple it to
   `payments`. Move `orders` onto it.
5. **`payments` is the reference implementation** — views → services → models,
   own schema, own permissions, 201 tests, correct locking. Phase 3 means
   making the rest look like this, not inventing something new.
6. **`hana` has 23 routes, 0 models and 0 permissions.** It is a raw query layer
   exposed directly to the internet. It is both the worst security offender and
   the easiest to fix — nothing depends on its internals.
7. **The company discriminator has five names.** Unifying it is valuable and
   expensive; do not attempt it before Phase 2 has closed the open endpoints,
   or the blast radius is untestable.
