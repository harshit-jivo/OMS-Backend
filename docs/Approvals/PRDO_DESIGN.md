# PRDO — Production Order approval in OMS

Design proposal. Driven by the Workflow Engine, modelled on BKDT
(`docs/backend/BKDT.md`). Written after reading the live JSAP `PRDO` schema, the
SAP `OWOR` tables and `SBO_SP_TRANSACTIONNOTIFICATION` — that investigation is
`PRODUCTION_ORDER_JSAP_SAP.md`.

**Decided: SAP is the point of origin.** The planner keeps raising production
orders in SAP B1. OMS replaces JSAP — it syncs the planned orders, routes them
through a configured approval chain, and writes the decision back so SAP can
release them. OMS never creates a production order.

The module is built and running against `TEST_JIVO_OIL_HANADB`. Nothing has
been applied to any live company database — the steps for that are
`PRDO_DEPLOYMENT.md`, which is a plan and not a record.

---

## 1. Companies — how this works without a user/company map

This is the first question to settle, because the obvious answer is wrong.

### 1.1 OMS has no user→company mapping that scopes documents

| Thing | What it is | Scopes documents? |
|---|---|---|
| `users.User.company` → `users.Company` | organisational master, int PK, free-text name | **No.** `core/companies.py` says so explicitly |
| `users.User.category` → `orders.Categories` | another int master, `varchar(255)`, not unique | No |
| `core.companies.COMPANY_CHOICES` | `OIL` / `BEVERAGES` / `MART` | **This is the document company** — but it is a closed string set, not a user attribute |

`payments.user_companies()` returns every company to every user, and its docstring
records that narrowing was removed on purpose: module access is governed by
permission keys, not by which company a user row points at. `backdate` does not
scope by user at all — its `?company=` is a display filter the caller chooses.

So there is no existing mechanism to reuse, and inventing one inside PRDO would
make this module disagree with `payments` and `backdate`.

### 1.2 The stage IS the company scoping

The engine already answers this, and it needs nothing from the user record.

A workflow carries `company` (`ALL` / `OIL` / `BEVERAGES` / `MART`). Its stages
carry the users. So:

```
PRDO_OIL_FG    company = OIL          stage 1 → Preshit
PRDO_OIL_PM    company = OIL          stage 1 → Shahrukh
PRDO_BEV_FG    company = BEVERAGES    stage 1 → (whoever)
```

`flow_service.pending_for(user)` finds the stages whose *effective* user is the
caller today, then filters flows by `current_stage`. It never asks what company
the user belongs to — **a stage only exists inside one company's workflow, so
naming a user on a stage is exactly "this person approves for this company"**.

Consequences worth stating:

- A Beverages approver sees only Beverages work, because no OIL stage names them.
- One person can approve in two companies — name them on a stage in each.
- Adding Beverages later is configuration, not code.
- Moving an approver is editing one stage. Everything already waiting there
  re-routes with nothing migrated (BKDT §8.1).
- A temporary stand-in is a `workflow_user_replacements` row, and works per
  company for free.

Compare JSAP, which needed a separate template, stage, and `jsUserStage` row per
company *and* a `jsUserCompany` table the intake loop walked — the loop that
produced the cross-company leak in `PRODUCTION_ORDER_JSAP_SAP.md` §6.

### 1.3 What this does NOT scope, and you should decide about

**Acting is scoped. Looking is not.** Anyone holding `Production_Order` can see
every company's requests in the list, history and insight views — `?company=` is
a filter, not a boundary. BKDT and payments behave the same way.

If production volumes must not be visible across companies, that needs a real
user→company map, and it should be built once in `core` for every module rather
than inside PRDO. **[UNKNOWN]** — needs a business answer, not a guess.

---

## 2. What OMS owns, and what it does not

```
SAP B1                          OMS                              SAP B1
------                          ---                              ------
planner creates OWOR            [1] sync: OWOR Status='P'
  (Status = 'P')          -->       -> production_order
                                    -> Workflow Engine selects
                                    -> flow at stage 1

                                [2] approve / reject
                                    (stage user, replacements)

                                [3] write the decision back  -->  release gate
                                                                  reads it
```

OMS does not create, edit, cancel or close a production order. Three
responsibilities only: **notice it, decide it, tell SAP.**

---

## 3. Tables — three, same as BKDT

Own PostgreSQL schema (`production`), created by the initial migration, exactly as
`payments`, `HAIS`, `workflow` and `backdate` do.

```
production.production_order              the synced SAP document
production.production_order_flow         where it is now, and the write-back outcome
production.production_order_action_logs  what was decided, by whom, when
```

No task table. A request waits at one stage, the flow holds that stage, the queue
is a filter on it. BKDT §3.4.

### 3.1 `production.production_order`

Not a form submission — **a snapshot of a SAP document**, written by the sync.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint | NOT NULL | PK |
| `company` | varchar(20) | NOT NULL | `OIL` / `BEVERAGES` / `MART`. Exactly one |
| `sap_doc_entry` | integer | NOT NULL | `OWOR.DocEntry` |
| `sap_doc_num` | integer | NOT NULL | `OWOR.DocNum` — what a human quotes |
| `item_code` | varchar(50) | NOT NULL | |
| `item_name` | varchar(200) | NOT NULL | snapshot from `OITM` |
| `item_group` | varchar(50) | blank ok | `U_Sub_Group` snapshot — routing key, §4 |
| `item_series` | integer | NULL | `OITM.Series` snapshot (389 FG / 392 RM / 393 SC) |
| `warehouse` | varchar(20) | NOT NULL | |
| `planned_qty` | decimal(19,6) | NOT NULL | **pieces** — what `OWOR.PlannedQty` means |
| `sal_factor2` | decimal(19,6) | NULL | pack size snapshot, so boxes stay derivable |
| `sal_pack_un` | decimal(19,6) | NULL | volume per piece snapshot |
| `order_type` | varchar(1) | NOT NULL | `S` / `P` / `D` |
| `post_date` | date | NOT NULL | `OWOR.PostDate` |
| `due_date` | date | NULL | |
| `batch_no` | varchar(50) | blank ok | `OWOR.U_BATCH_NO` |
| `mfg_date` | date | NULL | `OWOR.U_MFG` |
| `expiry_date` | date | NULL | `OWOR.U_EXP_DATE` |
| `sap_created_by` | varchar(50) | blank ok | `OUSR.U_NAME` of `OWOR.UserSign` |
| `sap_user_sign` | smallint | NULL | `OWOR.UserSign` — the raw id, §7.2 |
| `remarks` | text | blank ok | `OWOR.Comments` |
| `sap_status` | varchar(1) | NOT NULL | last seen `OWOR.Status` — `P`/`R`/`L`/`C`, §5.3 |
| `synced_at` | timestamptz | NOT NULL | |
| `created_at`, `updated_at` | timestamptz | NOT NULL | |

**There is no `created_by_id`.** Nobody in OMS raised this. The SAP creator is
recorded as a *snapshot* (`sap_created_by`, `sap_user_sign`), not as an FK to
`users_user`, because a SAP login is a different identity namespace from an OMS
user — the same mistake BKDT §12.2 flags about `createdBy`.

**Constraints**

| Name | Rule |
|---|---|
| `production_order_company_valid` | `company IN ('OIL','BEVERAGES','MART')` |
| `production_order_type_valid` | `order_type IN ('S','P','D')` |
| `production_order_sap_status_valid` | `sap_status IN ('P','R','L','C')` |
| `production_order_qty_positive` | `planned_qty > 0` |
| **`production_order_sap_uq`** | **UNIQUE (`company`, `sap_doc_entry`)** |

That last one is the single most important line in this document. `DocEntry`
sequences run **per company**, so `DocEntry` alone is not a key. JSAP's
`jsDocEntry.docEntry` had no company column, which is how 11 OIL orders ended up
recorded against Beverages and one has sat unactionable since April
(`PRODUCTION_ORDER_JSAP_SAP.md` §6). A composite unique makes that
class of bug unrepresentable.

### 3.2 `production.production_order_flow`

One row per request (UNIQUE `production_order_id`).

| Column | Type | Null | Meaning |
|---|---|---|---|
| `production_order_id` | bigint | NOT NULL | FK, UNIQUE |
| `status` | varchar(10) | NOT NULL | `PENDING` / `APPROVED` / `REJECTED` / `OBSOLETE` |
| `sap_status` | varchar(10) | NULL | NULL / `SUCCESS` / `FAILED` — the write-back |
| `sap_payload` | jsonb | NULL | exactly what was written back |
| `sap_status_text` | text | blank ok | the exact SAP response or error |
| `current_user_id` | integer | NULL | denormalised, **not** the authority |
| `workflow_id` | bigint | NOT NULL | FK → `workflow.workflows`, PROTECT |
| `current_stage` | bigint | NULL | FK → `workflow.workflow_stages`. NULL once finished |
| `total_stage` | smallint | NOT NULL | stage count at submission |
| `created_at`, `updated_at` | timestamptz | NOT NULL | |

`OBSOLETE` is the one status BKDT does not have, and it exists because of §5.3.

### 3.3 `production.production_order_action_logs`

Verbatim from BKDT §3.3 — `action` in `SYNC` / `APPROVE` / `REJECT` / `OBSOLETE`,
plus `acted_by`, `stage_id`, `remarks`, `action_data` jsonb, `acted_at`.
Append-only. Stage name and sequence resolved through `stage_id`, never copied.

`SYNC` replaces BKDT's `CREATE` and carries `acted_by = NULL` — no OMS user did
it. `OBSOLETE` likewise.

---

## 4. Workflow selection

Mirrors JSAP's live split — finished goods vs packaging material:

| Workflow code | Company | Condition | Approver today |
|---|---|---|---|
| `PRDO_OIL_FG` | OIL | `item_code NOT LIKE 'PM%'` | Preshit |
| `PRDO_OIL_PM` | OIL | `item_code LIKE 'PM%'` | Shahrukh |

As `workflow_queries` (BKDT §12 rules: expose `id`, never filter by it):

```sql
-- PRDO_OIL_FG
SELECT id FROM production.production_order
WHERE company = 'OIL' AND item_code NOT LIKE 'PM%'
```

```sql
-- PRDO_OIL_PM
SELECT id FROM production.production_order
WHERE company = 'OIL' AND item_code LIKE 'PM%'
```

Exact translations of JSAP queries 231 and 435. The pair is exhaustive and
non-overlapping for OIL, which matters because the engine treats 0 matches and
>1 matches as errors with no tie-breaking.

**Improve it once it runs.** `PM%` is a naming convention, not a fact about the
item; `OITM.Series` and `U_Sub_Group` are SAP-tagged truth. Both are snapshotted
onto the request so the condition can move onto them with no schema change. Ship
the prefix first so day-one behaviour matches JSAP, then switch and compare.

**Beverages and Mart:** ship OIL only. JSAP's Beverages templates (230, 436) are
inactive, `PlannedProductionOrders` holds zero Beverages rows, and neither
company's notification procedure gates production at all. Adding them later is
one workflow row plus stages.

---

## 5. The sync — the part that has to not die

This is the failure JSAP actually had: the feed into `PlannedProductionOrders`
stopped on 13 Aug 2026 and the approval job kept logging success for 33 days
because an empty source is not an error.

### 5.1 Shape

`python manage.py sync_production_orders [--company OIL] [--dry-run] [--since N]`

Same pattern as `tracker/management/commands/sync_jsap.py` and `sync_sap_saved.py`,
run from a `run_production_sync.bat` under Task Scheduler with the `%~dp0`
self-resolving path the other six already use.

Per company, read `OWOR` where `Status = 'P'`, joined to `OITM` and `OUSR`, then:

- **upsert** on (`company`, `sap_doc_entry`)
- for a *new* row, run `selection.select_for_module('PRDO', document_id, company)`
  and open the flow — in the same transaction, so an unroutable order is not
  stored (BKDT §4)
- for an *existing* row, refresh the snapshot and `sap_status`, and apply §5.3

### 5.2 It must fail loudly

Non-negotiable, and the reason this section exists:

- exit non-zero when the source query returns zero rows **and** the last
  successful sync was more than N hours ago — silence is the symptom, so silence
  must be the alarm
- record `synced_at` per row and a module-level last-run marker; expose both on
  `/api/production/health/`
- an unreachable HANA raises rather than returning an empty list — the same
  `SapUnavailable` distinction `tracker/sap.py` already draws between "SAP has no
  such document" and "we never managed to ask"
- the `.bat` must not end on `echo`, or its exit code is the echo's
  (`PRODUCTION_ORDER_JSAP_SAP.md`, and the tracker `.bat` fix)

### 5.3 Orders that leave Planned before a decision

JSAP had no answer to this, which is why 17 orders have sat Planned since as far
back as Oct 2025.

If a pending request's `OWOR.Status` is no longer `P`, the sync sets
`flow.status = OBSOLETE`, clears `current_stage` and `current_user`, and writes
an `OBSOLETE` log row. The queue stops showing it without anyone having to sweep,
and the history says what happened.

This is also the honest reading: SAP moved on without OMS, and pretending the
approval is still wanted would be a lie.

---

## 6. Lifecycle

```
sync sees OWOR Status='P'
      ↓
production_order upserted   +   SYNC log
      ↓
Workflow Engine selects workflow      ← atomic; unroutable = not stored
      ↓
flow at stage 1
      ↓
approve / reject           one approve completes a stage, one reject ends the flow
      ↓
final approval
      ↓
write the decision back to SAP   ← §7
      ↓
SUCCESS / FAILED
```

Properties carried over from BKDT §4 unchanged: routing commits with the row; on
final approval `current_stage` and `current_user` both become NULL; a SAP failure
does not undo the approval; rejection is terminal.

Rejection terminal means: OMS will not approve that DocEntry. The planner cancels
it in SAP or raises a new order, which the sync picks up as a new request.

---

## 7. Writing the decision back

`production/services/sap.py`, called only from the final-approval path and from
`retry-sap/`. A HANA write, not a Service Layer POST — no document is created.

### 7.1 Where to write it

SAP's gate is in `JIVO_OIL_HANADB.SBO_SP_TRANSACTIONNOTIFICATION`:

```sql
LEFT JOIN "PRODUCTIONORDERSYNC" P ON P."DOCENTRY" = A."DocEntry"
WHERE A."Status" = 'R' AND A."Type" = 'S'
  AND (P."STATUS" != 'A' OR P."STATUS" IS NULL)
  AND O."Series" != 392
  AND A."UserSign" != 33
```

Two options:

**(a) Keep writing `PRODUCTIONORDERSYNC`.** The rule works unchanged; smallest
possible SAP-side change. But its `ID` column currently carries JSAP
`jsDocEntry.id` values (2,681 rows, last written 13 Aug), so OMS ids would
collide. Needs an id strategy — an offset, or clearing the table at cutover
alongside JSAP's retirement.

**(b) Write an OMS-owned table and repoint the rule's join.** One edit to a
22,000-line procedure, but afterwards the two systems share nothing and JSAP's
rows can be left alone as history.

**(b) is cleaner and (a) is safer to cut over with.** Doing (a) at cutover and
(b) once OMS is proven is a reasonable sequence. **[UNKNOWN]** — needs a call.

Either way the payload is small: `DocEntry`, `Status`, `Company`, `RequestId`,
`DecidedBy`, `DecidedAt`, `SyncedAt`. SAP reads only the first two.

**Both outcomes are written.** `Status` is `'A'` on approval and `'R'` on
rejection. SAP's gate only tests `<> 'A'`, so a rejection could have been left
as no row at all and the order would stay blocked either way — but then "no
row" would mean both *rejected* and *nobody has looked at it*, and nothing
querying the table from inside SAP could tell those apart. Hence
`DecidedBy`/`DecidedAt` rather than `ApprovedBy`/`ApprovedAt`.

The consequences of a failed write are NOT symmetric, and the code says so: a
lost approval leaves an order blocked that should be shipping, while a lost
rejection loses only the record, because absence already blocks. Both are
recorded on the flow and retryable through `retry-sap/`.

Every value binds with `?`; only the schema name is interpolated, from
`Queries._schema_for_branch()`, never from a request (BKDT §11.4).

### 7.2 The `UserSign != 33` exemption

The gate currently exempts user 33 — Gautam Chanana — who created **2,888 of the
3,152** eligible orders. As it stands the rule blocks essentially nothing.

`sap_user_sign` is snapshotted onto the request so OMS can *report* on this: how
many approved orders were exempt anyway, and what the gate would have caught.
OMS cannot fix it — the literal is in SAP's procedure. But it should stop being
invisible.

Deciding whether that exemption stays is a business call and is listed in §11.

### 7.3 Retry

`POST /api/production/requests/<pk>/retry-sap/` — requires the approval key, a
fully approved flow, and refuses one already at `SUCCESS`. BKDT §11.6.

---

## 8. Permissions

Two keys, same reasoning as BKDT — noticing and deciding are different
authorities:

```python
'Production_Order':          'Production Order — view requests',
'Production_Order_Approval': 'Production Order — approve/reject',
```

| Capability | Requirement |
|---|---|
| Open the page, view requests | `Production_Order` |
| Open the approval desk | `Production_Order_Approval` |
| **Approve / reject** | `Production_Order_Approval` **AND** being the current *effective* stage user |
| Configure workflows/stages/replacements | `workflow.config.manage` |

Enforced server-side from `request.user`, never a client-supplied id. JSAP's
production endpoints took the approver id from the body.

---

## 9. Routes

```
GET        /api/production/requests/              ?status=&company=&month=MM-YYYY
GET        /api/production/requests/<pk>/
GET        /api/production/requests/<pk>/history/
POST       /api/production/requests/<pk>/approve/
POST       /api/production/requests/<pk>/reject/
POST       /api/production/requests/<pk>/retry-sap/
GET        /api/production/insights/              ?company=&month=MM-YYYY
GET        /api/production/approvals/queue/       ?company=
GET        /api/production/approvals/history/     ?status=&company=
GET        /api/production/approvals/insights/    ?company=
GET        /api/production/health/                last sync per company, §5.2
```

No `POST /requests/` and no `PATCH` — OMS does not create or edit production
orders. That absence is the design.

---

## 10. Registration

`production/apps.py`, `post_migrate`, exactly as BKDT:

```python
register_module(code='PRDO', name='Production Order')
```

---

## 11. Open questions — do not guess

1. **§7.1** — write to the existing `PRODUCTIONORDERSYNC` (and solve the id
   collision), or an OMS-owned table with the notification rule repointed?
2. **§7.2** — does the `UserSign != 33` exemption stay? As written the approval
   gate does not apply to 92% of production.
3. **§1.3** — should list/history views be scoped per company, or is
   approve-scoping enough? If scoped, it is `core` work, not PRDO work.
4. **§4** — Special and Disassembly orders: in or out? JSAP never touched them
   (702 of the 812 it missed). Raw-material blending at BH-LO, 938 OIL orders, is
   likewise outside every approval today.
5. Are Preshit (FG) and Shahrukh (PM) still the approvers, and does either want a
   second stage?
6. Cutover: run OMS and JSAP in parallel and compare, or switch outright? The
   JSAP feed has been dead since 13 Aug, so there is currently nothing to compare
   against and a month of orders never entered any approval.

Answer 1 and 2 and this is buildable. The rest is configuration.
