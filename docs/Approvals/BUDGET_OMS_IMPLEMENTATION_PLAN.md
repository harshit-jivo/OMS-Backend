# BUDGET — OMS Design and Implementation Plan

**Status:** plan only — no code, no migrations, no SAP/HANA or SQL Server change.
**Revision:** 3 — 2026-09-24. Database simplified to **exactly eight Budget-owned
tables** (§33); every section rewritten around them. Revision 2 corrected the engine
contract (no `key_column`, no read-only role configuration).
**Primary source of truth:** [`BUDGET_OMS_RELEVANT_AUDIT.md`](BUDGET_OMS_RELEVANT_AUDIT.md)
(READY FOR OMS DESIGN; B-1 … B-5 resolved). `audit §n` points there.
**Supporting reference only:** [`BUDGET_LEGACY_AUDIT.md`](BUDGET_LEGACY_AUDIT.md) (its
§19 proposals and §21 questions are superseded by §2 below).
**Engine contract:** [`WORKFLOW_MODULE_INTEGRATION.md`](WORKFLOW_MODULE_INTEGRATION.md).
**Worked precedent:** `backdate/` — `backdate.backdate`, `backdate.backdate_flow`,
`backdate.backdate_action_logs` — and [`BKDT.md`](BKDT.md).

Conventions:

* **LOCKED** — final business/design decision; not reopened here.
* **DESIGN** — decided by this plan; reviewable.
* **SAP** — depends on SAP/HANA objects or behaviour outside OMS.
* OMS company codes `OIL`, `BEVERAGES`, `MART` (`core.companies`). SAP's decision table
  uses branch strings `OIL` / `BEVERAGE` / `MART` — mapping in §16.
* Money `numeric(18,2)` INR; months `date` (first day of month).
* Example values are **illustrative** unless the audit is cited.

---

## 1. Purpose and Scope

**Purpose.** Replace the JSAP Budget module with an OMS module that approves
budget-controlled SAP drafts and manages budget allocations, reproducing the verified
business behaviour and fixing the verified defects, with the smallest database that
holds the useful business data.

**In scope**

| Area | What OMS delivers |
|---|---|
| Budget document approval | SAP draft lines → routed budget documents → staged approval → SAP decision write-back |
| Allocation | budgets, sub-budgets, monthly allocations, allocation increase requests with approval |
| Automation | 5-minute ingestion, 48-hour auto-approval |
| Visibility | approver inbox, document detail, dashboards/reports, routing-exception list |
| Administration | workflows, queries, stages, replacements in the existing engine UI; module settings |

**Out of scope**

* Legacy data migration — **none**; OMS starts empty (LOCKED).
* Any change to SAP/HANA objects or to SQL Server / JSAP (LOCKED; §31 lists the JSAP
  *configuration* step a cut-over needs, done by JSAP administrators).
* Any change to the Workflow Engine (LOCKED).
* Mart budget **document** approval (not ingested today — §30, §39 N-2).

---

## 2. Final Decisions / Locked Rules

| # | Decision | Source |
|---|---|---|
| L-1 | Existing Workflow Engine **unchanged**: configuration only; one user per stage; stage `sequence` is execution order; one approve completes a stage; one reject ends the execution; 0 matches `WorkflowNotConfigured`, 1 selected, >1 `AmbiguousWorkflowSelection`; replacement/delegation unchanged. No Budget-specific engine tables. | audit B-1, §11 |
| L-2 | Budget document approval and allocation approval both use the engine, as two registered modules (`BUDGET`, `BUDGET_ALLOCATION`). | request A |
| L-3 | Budget entries originate from **SAP draft data**, never from user submission. | audit §1 |
| L-4 | Ingestion every **5 minutes**; only `ProcesStat = 'Y'` lines are eligible. | audit §1 |
| L-5 | Ingestion improved from append-only to **upsert with change detection**. | audit I-4 |
| L-6 | Routing selects **exactly one** workflow; 0 → routing exception; >1 → ambiguous exception; no duplicate approval chains; no SQL batch sweep. | audit §2, I-1, I-2 |
| L-7 | Routing dimensions: company · budget head · optional sub-budget · JV vs other · account include/exclude · effective date · configurable business vertical. | audit R-3; request C |
| L-8 | Budget document = one routing slice of one SAP document; identity **(company, SAP ObjType, SAP DocEntry, selected workflow)**. One SAP document may produce several documents. | request D; audit R-4 |
| L-9 | JV source-line identity **Company + BatchNum + TransId + Line_ID**. | audit B-2 |
| L-10 | SAP write-back reproduces JSAP: `tbl_Draft_Approvals`, key **DocEntry + ObjType + LineNum + VisOrder**, `A` final / `V` non-final stage completed / `R` rejection, verification. JV collisions = known inherited SAP/JSAP limitation; no collision-blocking rule; no SAP change. | audit B-5 |
| L-11 | Approval: stage 1 first; sequential; current effective stage user acts; last approval → Approved; actor from JWT only; append-only history; no implicit pull-back. | audit R-5, R-6, I-7, I-8 |
| L-12 | Rejection: current effective stage user; **remarks required**; terminal; SAP `R`; notifications. | audit R-7, I-10, I-13 |
| L-13 | Allocation change amount is an **increase**; `> ₹3,00,000` → approval workflow, final approval `allocated = allocated + increase`; `≤ ₹3,00,000` → recorded, **no workflow**, no allocation change; rejection leaves the allocation; OIL, BEVERAGES, MART allocation workflows. | audit B-3 |
| L-14 | **Keep 48-hour auto-approval**, audited rule, recorded as a **system action**; no named legacy user ids. | audit B-4 |
| L-15 | Notifications through existing OMS infrastructure; recipients from runtime state. | audit I-13 |
| L-16 | Explicit permission keys; backend authorization on every endpoint. | audit I-7 |
| L-17 | Reports from structured OMS data; numeric/date types; one definition of allocated / committed / pending / available / overspend; no SQL-text parsing. | audit §12 |
| L-18 | **No data migration.** | audit |
| L-19 | Coexistence: one system owns each SAP document's decision; rollout per company / budget head by effective date. | request O |
| L-20 | **Exactly eight Budget-owned tables** in schema `budget`: `budgets`, `sub_budgets`, `monthly_allocations`, `sub_budget_monthly_allocations`, `documents`, `allocation_requests`, `budget_flow`, `action_logs`. No staging, line, routing, technical-log or settings tables. | database decision |

---

## 3. Legacy Behaviour to Reproduce

| # | Behaviour | Audit | Plan |
|---|---|---|---|
| P-1 | Entries from SAP drafts on a timer | §1 | §7 |
| P-2 | Eligibility `ProcesStat = 'Y'` (JV: open batch ↔ `Y`) | §1, B-2 evidence §2 | §8 |
| P-3 | Sources: drafts ObjType 14, 15, 18, 19, 59, 60; payment drafts 46; journal vouchers 28 | B-2 evidence §1 | §7.2 |
| P-4 | Routing on company / budget head / sub-budget / JV-or-not / account class / effective date | §2 | §10–§11 |
| P-5 | One SAP document may yield several budget documents with different approvers | §2 | §9 |
| P-6 | Ordered stages, stage 1 first, current stage acts, last approval approves | §3–§5 | §14 |
| P-7 | SAP decision per line `A`/`V`/`R` into `tbl_Draft_Approvals`, verified | §8 | §16 |
| P-8 | ObjType 19 counts negative | R-10 | §27 |
| P-9 | Approver lists: pending for me / approved by me / rejected by me | R-8 | §24 |
| P-10 | Monthly allocations, one per budget-month and per sub-budget-month | §9 | §18 |
| P-11 | Allocation increase; > ₹3,00,000 workflow; ≤ stored without flow | B-3 | §19–§20 |
| P-12 | 48-hour auto-approval with allocation check; exempt stage users never auto-approved | §10, N-4 | §21 |
| P-13 | SAP-computed budget figures shown on lines are informational | §1 | §8 |

---

## 4. Legacy Defects We Intentionally Fix

| # | Legacy defect | OMS behaviour |
|---|---|---|
| F-1 | SQL text run as a batch sweep per user per run | engine per-slice selection with bound key (§10–§11) |
| F-2 | Several templates match one line → duplicate chains | exactly one workflow or an exception (L-6) |
| F-3 | No-match lines silently ignored | `UNROUTED` document row + exception list (§10.3) |
| F-4 | Append-only snapshot | line upsert + content hash + change handling (§8.3) |
| F-5 | Late lines never routed | join pending document; post-decision → revision (§9.4) |
| F-6 | Existence check without ObjType/company | identity L-8 |
| F-7 | Headers without lines | a document always carries its lines in the same row (§9) |
| F-8 | JV source key not unique; header fan-out | L-9 identity; header joined on `BatchNum + TransId` (§7.2) |
| F-9 | Routing depends on a valid approver | routing independent of staffing (§10.4) |
| F-10 | Actor id from request body; no authentication | JWT actor + permission keys (§23, §32) |
| F-11 | History overwritten | append-only `budget.action_logs` (§28) |
| F-12 | Lowest-stage-wins; implicit pull-back | stage authority from engine; no pull-back (§13, §14) |
| F-13 | `R` written on a non-final rejection vote | `R` only when the document is rejected (§15) |
| F-14 | Validity/delegation applied inconsistently | engine replacements everywhere (§13) |
| F-15 | Notify recipients from a config id; none on reject/allocation | runtime recipients; full set (§22) |
| F-16 | Attachments matched by DocEntry only | company + ObjType + DocEntry (§8.5) |
| F-17 | Allocation request shown as a new total while approval adds it | stored and shown as an increase (§19) |
| F-18 | Auto-approval as named users; one error aborts the run; one arbitrary bucket checked | system action; per-document isolation; every bucket (§21) |
| F-19 | Three allocation sources, per-user "approved", SQL-text parsing, text months | one source, per-budget figures, structured data (§27) |
| F-20 | Unbounded technical logging (7 M+ rows) | business events only in `action_logs`; skips logged on change (§21, §28) |

---

## 5. High-Level OMS Architecture

```text
                   ┌──────────────────────── OMS-Backend (Django 5.2 / DRF) ─────────────────────────┐
 SAP HANA          │  budget/  (new app, PostgreSQL schema "budget", 8 tables)                        │
 (OIL, BEVERAGES)  │   ├─ ingestion   5-min job: read SAP → upsert lines inside documents →            │
  drafts, payment ─┼──►│              candidate slices → engine selection → merge / create documents │
  drafts, vouchers │   ├─ documents   approve / reject over budget.documents + budget.budget_flow     │
  attachments ─────┼──►├─ sap         tbl_Draft_Approvals write-back (A/V/R) + verification ─────────┼─► SAP HANA
  (on demand)      │   ├─ allocation  budgets, sub-budgets, monthly allocations, allocation requests  │   tbl_Draft_Approvals
                   │   ├─ auto        10-min auto-approval job (system actor)                          │
                   │   ├─ reports     allocated / committed / pending / available                      │
                   │   └─ api         DRF views, core.permissions.HasKey                               │
                   │  workflow/      existing, unchanged — modules, workflows, queries, stages,        │
                   │                 replacements, select_for_module(), stages_for(),                 │
                   │                 get_stage_assignment(), central condition executor               │
                   │  notifications/ existing — notify(), registry                                     │
                   │  hana/          existing — HANAConnection                                        │
                   │  sap_sync/      existing — APScheduler helpers                                   │
                   │  core/          existing — permission_registry, companies                        │
                   └───────────────────────────────────────────────────────────────────────────────────┘
                                   ▲ REST (JWT)
                     OMS-Frontend (React/Vite) · OMS mobile (Expo, later phase)
```

The engine answers *which workflow and which stages*. Everything else is Budget's,
stored in the eight tables of §33.

---

## 6. Budget Domain Boundaries

| Sub-domain | Owns | Tables |
|---|---|---|
| Master & allocation | budgets, sub-budgets, monthly amounts | `budgets`, `sub_budgets`, `monthly_allocations`, `sub_budget_monthly_allocations` |
| Budget documents | routing slices, their SAP lines (JSON), routing status | `documents` |
| Allocation requests | increase requests | `allocation_requests` |
| Approval runtime | one flow per document and per workflow-backed allocation request | `budget_flow` |
| History | every business event: document, allocation request, allocation, SAP write, auto-approval | `action_logs` |
| Configuration | engine objects (workflows, queries, stages, replacements) + Django settings (§12.3) | none of its own |

Two engine modules (L-2):

| Module | Engine document id passed as `document_id` | Workflow company |
|---|---|---|
| `BUDGET` | `budget.documents.id` | `OIL`, `BEVERAGES` |
| `BUDGET_ALLOCATION` | `budget.allocation_requests.id` | `OIL`, `BEVERAGES`, `MART` |

---

## 7. SAP → OMS Ingestion Architecture

### 7.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Mirror eligible SAP budget lines into Budget documents, detect new / changed / withdrawn lines, route new lines. |
| **Input** | Read-only HANA SELECTs per company schema (`JIVO_OIL_HANADB`, `JIVO_BEVERAGES_HANADB`). |
| **Processing** | (1) read → (2) match each row to an existing document line by source key (`documents.line_keys`) → (3) update changed lines / mark missing lines → (4) group **new** eligible lines into candidate slices (§10.2) → (5) engine selection per candidate → (6) merge into an existing document or keep as a new document (§9) → (7) unmatched/ambiguous candidates stay as exception rows. |
| **Database** | `budget.documents` (lines JSON, routing status), `budget.budget_flow` (new documents), `budget.action_logs`. No staging table. |
| **Workflow** | `select_for_module(module_code='BUDGET', document_id=<candidate documents.id>, company=…)`. |
| **SAP** | Read only. |
| **Notification** | new pending document → stage-1 effective user; new exceptions → configuration owners (hourly digest). |
| **Failure** | One company's read failing does not stop the other. Each SAP document is processed in its own transaction; a failure rolls back that SAP document's changes and they are retried next run. Run outcome goes to the application log and an admin notification on repeated failure (no run table). |

### 7.2 What is read (DESIGN + SAP)

OMS does not `CALL DRAFT_APPROVAL` because it drops `TransId` and fans voucher lines
across headers (audit B-2 evidence §1, §5), so it cannot supply L-9. OMS runs read-only
SELECTs reproducing `DRAFT_APPROVAL`'s selection (live definition, created
2026-08-14), correcting only the JV identity:

| Branch | SAP tables | Selection | OMS correction |
|---|---|---|---|
| Document drafts | `ODRF`/`DRF1` + `OACT` + `OWDD`/`WDD1`/`OUSR` | ObjType ∈ {14, 15, 18, 19, 59, 60}; `WDD1.Status='Y'`; `OWDD.ProcesStat='Y'`; `DocDate ≥ 2025-04-01`; account father ∈ {5610000 … 5690000} or account 5100008; budget ≠ `Sal CF` | none |
| Payment drafts | `OPDF`/`PDF4` + approval tables | ObjType 46; `WDD1.Status ∈ {'Y','P'}` (Oil) / `'Y'` (Beverage), as in the live definition; same account/budget filters | none |
| Journal vouchers | `BTF1` + `OBTF` + `OACT` + `OUSR` | account father in expense set; account not in 5630001, 5630002, 5630005 … 5630016; `Debit <> 0`; `RefDate ≥ 2025-04-01`; budget ≠ `Sal CF`; `ProcesStat` = `Y` if `BtfStatus='O'`, `P` if `'C'` | **select `TransId`; join `OBTF` on `BatchNum + TransId`** |
| Decision filter | `tbl_Draft_Approvals` | exclude lines whose row (on `DocEntry + LineNum + ObjType`) is `ApprovedStatus = 'A'` | reproduced unchanged (§17.3) |

Queries live in `budget/ingestion/sap_queries.py` with a header naming the
`DRAFT_APPROVAL` version mirrored. A gated parity test (§36) compares non-JV keys with
`CALL DRAFT_APPROVAL` (SAP dependency S-3).

### 7.3 Line fields kept (inside `documents.lines`, §33.6)

Source identity (§8.1), write-back key (§16.2), `obj_type`, `object_name`,
`account_code`, `account_name`, `card_code`, `card_name`, `effect_month_code`
(`OcrCode2`), `budget_head` (`OcrCode3`), `sub_budget` (`OcrCode4`), `state_code`
(`OcrCode5`), `profit_code` (`OcrCode` / JV `ProfitCode`), `doc_date` (JV `RefDate`),
`create_date`, `amount`, `signed_amount`, `budget_month`, `line_remarks`, `comments`,
`owner_code`, `approval_code`, `sap_current_month_budget`,
`sap_current_month_posted` (informational, P-13), `sap_update_date`, `proces_stat`,
`content_hash`, `first_seen_at`, `last_seen_at`, `missing_since`.

### 7.4 Schedule

Every 5 minutes via `sap_sync.scheduler` helpers (APScheduler), inside the **Budget
document write lock** (§8.2), so ticks never overlap.

---

## 8. SAP Staging / Source-Line Identity

There is **no staging table** (L-20). SAP lines exist in OMS only inside the Budget
document that holds them (`documents.lines`). A SAP line that is ineligible, or that
belongs to a JSAP-owned document (§31), is simply not stored.

### 8.1 Identity (LOCKED L-9 + DESIGN)

| Source branch | Source key (string, stored per line and in `documents.line_keys`) |
|---|---|
| Journal voucher (28) | `JV:<company>:<BatchNum>:<TransId>:<Line_ID>` |
| Document draft (14, 15, 18, 19, 59, 60) | `DRF:<company>:<ObjType>:<DocEntry>:<LineNum>` |
| Payment draft (46) | `PDF:<company>:46:<DocEntry>:<LineId>` |

The JV key carries **company + BatchNum + TransId + Line_ID** exactly (L-9); the line
also stores the JSAP write-back key separately (§16.2).

### 8.2 One line, one document

`documents.line_keys text[]` holds the source keys of the lines currently in the
document, with a **GIN index**.

**The Budget document write lock — the single serialized write path.** Every operation
that changes `budget.documents.lines`, `budget.documents.line_keys` or
`budget.documents.routing_status` runs inside one PostgreSQL transaction-level advisory
lock, `pg_advisory_xact_lock(<BUDGET_DOCUMENT_LOCK>)` (the scheduled ingestion tick uses
the non-blocking `pg_try_advisory_xact_lock` and skips the tick if it is held). This
applies to:

* SAP ingestion (§7) — new, changed, moved and missing lines, candidate slices, merges;
* re-route of `UNROUTED` / `AMBIGUOUS` rows (§10.3);
* shadow-row cleanup (§37);
* the manual re-route of a document's lines (§24) and any future operation that
  mutates candidate or document line state.

Approve, reject, auto-approve and SAP write-back do not change these three columns and
do not take this lock (they lock the `budget_flow` row, §14).

Before a line is placed in any row, the writer checks
`SELECT 1 FROM budget.documents WHERE line_keys && ARRAY[<key>] AND routing_status <>
'WITHDRAWN'`. Because every writer is serialized by the lock, this guarantees a SAP line
sits in at most one non-withdrawn row. **This is an application-level guarantee** —
serialized writer + `line_keys` check — **not** a PostgreSQL UNIQUE constraint on array
elements, which PostgreSQL cannot express.

### 8.3 Change detection (upsert inside the document)

| Situation | Handling |
|---|---|
| key not found, eligible | new line → its candidate slice (§10.2): added to an existing `UNROUTED`/`AMBIGUOUS` row of the same slice if one exists, else a new `ROUTING` candidate |
| key found, hash unchanged | `last_seen_at` updated in the JSON |
| key found, hash changed, row `UNROUTED`/`AMBIGUOUS` | line updated in place; `LINE_CHANGED` logged; if a routing value changed, the line moves to the slice it now belongs to (`LINE_REMOVED` + `LINE_ADDED`) |
| key found, hash changed, `budget_flow.status = PENDING` | line updated in place; `LINE_CHANGED` in `action_logs` (old/new); if a routing field changed (budget head, sub-budget, account, JV class, vertical) the line is removed (`LINE_REMOVED`) and routed again as a new candidate; current approver notified |
| key found, hash changed, `budget_flow.status = APPROVED`/`REJECTED` | the decided document's line is **not** altered; `CHANGED_AFTER_DECISION` logged on the document and shown on the exceptions list |
| key found, line became ineligible (`P`) while `budget_flow.status = PENDING` | `SOURCE_NOT_ELIGIBLE` logged; shown on the exceptions list; no automatic action |
| key absent from a complete successful read, `budget_flow.status = PENDING` | `missing_since` set; `SOURCE_WITHDRAWN` logged; the flow is not auto-cancelled. Absence caused by the SAP `A` filter after OMS's own approval is expected and ignored |

### 8.4 Eligibility

`proces_stat == 'Y'` and the row passed §7.2. Only eligible lines are stored.

### 8.5 Attachments (no table)

No Budget attachment table and no attachment sync job.

```text
document opened
    ↓
read the attachment rows for that document from SAP
    ↓
build download links with the existing file-server configuration
    ↓
cache the SAP result for 10 minutes in the Django application cache
```

* **SAP source.** The live HANA catalogue (`SYS.PROCEDURES`, read 2026-09-24 during the
  B-2 audit) lists `DRAFT_APPROVAL_ATC1` in `JIVO_OIL_HANADB` only (plus a TEST
  schema); it is the procedure JSAP's attachment job calls (audit §8). It is
  **not** confirmed per company schema. OMS calls the Oil procedure, as JSAP does, and
  filters its result by **company + ObjType + DocEntry** (F-16). Whether that result
  includes Beverage attachments is a **SAP dependency (S-4) until verified**; until then
  Beverage documents show "attachments unavailable" rather than a guess.
* **OMS infrastructure.** Download URLs use the existing file-server base-URL
  configuration that JSAP and OMS already use for SAP attachment files. The OMS
  `attachments` app (which stores OMS-uploaded files) is **not** used for SAP draft
  attachments — they are never copied into OMS.
* **Cache.** Django cache key per company, 10-minute timeout (the legacy job refreshed
  every 2 minutes); a cache miss costs one procedure call.

---

## 9. Budget Document Formation

### 9.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Turn routed candidate slices into approvable documents: one per SAP document per workflow (+ revision). |
| **Input** | Candidate `documents` rows (status `ROUTING`) of one SAP document, each with an engine selection. |
| **Processing** | per candidate: selected workflow W → a `ROUTED` document for (company, ObjType, DocEntry, W) whose `budget_flow.status = PENDING` exists? **merge** lines into it and delete the candidate row; one exists whose `budget_flow.status` is `APPROVED`/`REJECTED`? candidate becomes **revision n+1**; none? candidate **becomes** the document (`routing_status = ROUTED`) and gets a `budget_flow` (`status = PENDING`) at stage 1. |
| **Database** | `documents`, `budget_flow`, `action_logs`. |
| **Workflow** | result of `select_for_module` (stages for a new document); `stages_for(workflow)` when joining an existing document. |
| **SAP** | none. |
| **Notification** | new document → stage-1 effective user; lines added to a document whose `budget_flow.status = PENDING` → its current effective user. |
| **Failure** | candidate creation, selection, merge/creation, flow and logs for one SAP document are one transaction, under the Budget document write lock (§8.2); on error everything for that SAP document rolls back and is retried next run. A document never exists without its lines (they are in the same row) — F-7. |

### 9.2 Identity (LOCKED L-8 + DESIGN)

Partial UNIQUE `(company, obj_type, doc_entry, workflow_id, revision) WHERE
routing_status = 'ROUTED'`. `revision` starts at 1.

### 9.3 Header values

`budget_head`, `sub_budget`, `account_code`, `document_class`, `vertical_value` are the
**routing columns** — homogeneous in a candidate (§10.2); after a merge they keep the
value when all lines agree, else NULL (they are only read by selection, which runs on
candidates). Also `budget_month` (NULL if lines differ — only possible for JV batches
whose vouchers span months), `card_code/name`, `object_name`, `doc_date`,
`total_amount`, `signed_total`.

### 9.4 Late and moved lines (fixes F-5)

| Case | Handling |
|---|---|
| New line routed to W while the document for W has `budget_flow.status = PENDING` | merged; `LINE_ADDED` logged |
| New line routed to W while the document for W has `budget_flow.status = APPROVED`/`REJECTED` | revision n+1 document with its own flow |
| Routing field changed on a line of a document with `budget_flow.status = PENDING` (§8.3) | removed (`LINE_REMOVED`) and re-routed |

**WITHDRAWN — the only withdrawal case.** When every line has left a `ROUTED` document
whose `budget_flow.status = PENDING`, in the same transaction:
`documents.routing_status = WITHDRAWN` **and** `budget_flow.status = WITHDRAWN`;
`WITHDRAWN` logged; SAP untouched. No other withdrawal state exists.

---

## 10. Budget Routing Architecture

### 10.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Select exactly one workflow for each candidate slice. |
| **Input** | a candidate `documents` row. |
| **Processing** | `select_for_module(module_code='BUDGET', document_id=candidate.id, company=candidate.company)`; the engine runs every active, validated `WorkflowQuery` of `BUDGET` for that company and returns 0 / 1 / >1. |
| **Database** | the candidate row: `workflow_id`, `routing_status`, `routing_detail`. |
| **Workflow** | engine selection only (unchanged). |
| **SAP** | none. |
| **Notification** | exception digest (§22). |
| **Failure** | `WorkflowNotConfigured` → `routing_status = UNROUTED`; `AmbiguousWorkflowSelection` → `AMBIGUOUS` (detail lists matched workflows); `InvalidWorkflowConfiguration` / `ConditionExecutionError` → transaction rolled back, retried next run, error logged. |

### 10.2 Candidate slices — why and how (DESIGN)

The engine selects per `document_id`, and its queries read database rows. Since there
is no line table, the unit selected is a **candidate slice**: the new eligible lines of
one SAP document that share **every routing value** —

```
(company, obj_type, doc_entry, budget_head, sub_budget, account_code,
 document_class (JV / NON_JV), vertical_value)
```

Each slice is written as a `documents` row with `routing_status = 'ROUTING'` and its
lines, then selected. Because every routing value is uniform inside the slice, a query
over the row's columns decides exactly as a per-line decision would; slices that select
the same workflow are then merged (§9.1). This preserves P-5 (one SAP document → several
workflow documents) with no line table.

**A new line whose slice already has an exception row.** If an existing `UNROUTED` or
`AMBIGUOUS` row has the same slice values, the new line is **added to that row**
(`LINE_ADDED`) instead of creating a second exception row. The row keeps its
`UNROUTED`/`AMBIGUOUS` status until it is re-routed (§10.3).

**Candidate visibility (same connection, same transaction).** The candidate row is
created inside the routing transaction and is not yet committed when the engine
evaluates it. It is visible to the condition query because the engine evaluates on the
caller's connection within the caller's transaction (inside a savepoint), which is how
`workflow.services.conditions` executes when no separate execution connection is
configured — the final engine architecture:

```text
create candidate documents row (routing_status = ROUTING)
    ↓  same transaction, same database connection
select_for_module('BUDGET', document_id=candidate.id, company=…)
    ↓
WorkflowQuery (… WHERE routing_status = 'ROUTING' …) sees the candidate
    ↓
routing result 0 / 1 / >1
```

No engine change is involved; Budget relies on the engine's existing execution mode.

### 10.3 Outcomes

| Engine result | Row after routing | Visible in |
|---|---|---|
| 1 | `ROUTED` (merged or kept, §9) | inbox |
| 0 | `UNROUTED`, kept as the exception record | exceptions list |
| > 1 | `AMBIGUOUS`, kept as the exception record | exceptions list |

**Re-route lifecycle.** Resolving an exception = fix the engine configuration, then
**Re-route** (single or bulk). Budget workflow queries only evaluate rows with
`routing_status = 'ROUTING'` (§11.1), so re-route is:

```text
UNROUTED / AMBIGUOUS row
    ↓  one transaction, under the Budget document write lock (§8.2)
set routing_status = ROUTING
    ↓
select_for_module('BUDGET', document_id=row.id, company=…)   (selection from scratch)
    ↓
0 matches  → routing_status = UNROUTED
1 match    → ROUTED — merged into / kept as a document exactly as in §9.1 (+ budget_flow)
>1 matches → routing_status = AMBIGUOUS
    ↓
REROUTED logged (with the result)
```

Bulk re-route processes each row in its own transaction, each under the lock.

### 10.4 Staffing independence

Routing never checks whether a stage user is valid today (F-9). A workflow without
active stages raises `InvalidWorkflowConfiguration`, surfaced as an error, not skipped.

### 10.5 Company mapping

`JIVO_OIL_HANADB` → `OIL`, `JIVO_BEVERAGES_HANADB` → `BEVERAGES`, `JIVO_MART_HANADB` →
`MART` (not ingested yet).

---

## 11. Routing Rules / Query Replacement

There is **no Budget routing-rule table** (L-20). A routing rule **is** an engine
`WorkflowQuery` over `budget.documents`, configured in the existing engine UI.

### 11.1 Rule = one condition query

```sql
-- Oil Sales / GT, non-JV, legacy account exclusions, effective from 2026-10-01
SELECT id FROM budget.documents
WHERE routing_status = 'ROUTING'
  AND company = 'OIL'
  AND budget_head IN ('Sales')
  AND sub_budget IN ('GT')
  AND document_class = 'NON_JV'
  AND account_code NOT IN ('5630001','5630006','5630007','5630008','5630009','5630010',
                           '5630011','5630012','5630013','5630014','5630015','5630016','5680011')
  AND first_seen_at >= '2026-10-01'
```

| Legacy dimension (audit §2) | Column on `budget.documents` |
|---|---|
| `BRANCH = 'OIL'` | `company` (also the query's engine `company`) |
| `BUDGET IN (...)` | `budget_head` |
| `SUB_BUDGET IN (...)` / `OR SUB_BUDGET IS NULL` | `sub_budget` |
| `ObjType = 28` / `!= 28` | `document_class` (`JV` / `NON_JV`) |
| `AcctCode IN / NOT IN (...)` | `account_code` |
| `budgetDate > '<cut-off>'` | `first_seen_at` (OMS load time — same semantics, §39 N-7) |
| (new) business vertical | `vertical_value` (§11.4) |

Hard-coded legacy document-number exceptions are not carried into queries; if needed,
an administrator performs an audited manual re-route (§24).

### 11.2 Identity reaches the engine only as `document_id`

Budget passes `documents.id` as `document_id` to `select_for_module()`; every Budget
query projects that key as `id`, the column the engine binds against by default
(integration contract §6, §12). Budget adds nothing to the engine — no registration
fields, no query fields, no extra selection arguments.

### 11.3 Validation and execution

Queries are saved and run only through the engine's existing mechanism: the query-save
validator (SELECT/WITH only, single statement, no DML/DDL, forbidden-schema and function
deny-lists, `validated_at` gate) and the central condition executor
(`workflow.services.conditions`: bound document key, statement timeout, read-only
execution path per `conditions.isolation_mode`). Budget executes no condition SQL of its
own and adds no database role, connection alias or relation list.

### 11.4 Business vertical dimension (configurable, SAP field not yet identified)

`documents.vertical_value` is filled at ingestion from a configured source field:
Django settings `BUDGET_VERTICAL_SOURCE_FIELD` (one of the captured line fields of
§7.3, e.g. `profit_code`, `sub_budget`, `state_code`) and optional
`BUDGET_VERTICAL_VALUE_MAP` (SAP value → vertical label). Until the business confirms
which SAP field carries the vertical (§39 N-1) the setting is empty, `vertical_value`
is NULL, and no query uses it.

### 11.5 Overlap protection

Ambiguity is caught by the engine at selection time (`AMBIGUOUS` rows, §10.3). Before a
query is activated, the Budget configuration page offers a **dry-run**: it re-evaluates
all active `BUDGET` queries plus the draft against the recent `ROUTED` and `UNROUTED`
rows through the engine's evaluation service (read-only, nothing stored) and reports
rows that would match 0 or >1 workflows.

---

## 12. Budget Workflow Configuration

### 12.1 Engine objects (existing UI, unchanged)

| Engine object | Budget usage |
|---|---|
| `WorkflowModule` | `BUDGET`, `BUDGET_ALLOCATION` — code + name, registered from `budget/apps.py` |
| `Workflow` | `BUDGET`: one per routing slice definition (e.g. `OIL_SALES_GT`); `BUDGET_ALLOCATION`: one per company |
| `WorkflowQuery` | `BUDGET`: §11.1. `BUDGET_ALLOCATION`: `SELECT id FROM budget.allocation_requests WHERE status = 'IN_WORKFLOW'` (company from the workflow/query `company` column; the request row is visible to the engine in the same transaction, as in §10.2) |
| `WorkflowStage` | ordered stages, one user each (live JSAP: one stage, a few two — audit §4) |
| `WorkflowUserReplacement` | date-bounded stand-ins |

### 12.2 Re-entering the JSAP configuration

The 52 Oil + 44 Beverage active JSAP budget templates and 3 allocation templates
(audit §4) are re-entered by administrators as workflows + queries + stages. This is
configuration, not migrated data.

### 12.3 Module settings (no settings table)

Django settings (environment-backed), changed by deployment:

| Setting | Default | Used by |
|---|---|---|
| `BUDGET_ALLOCATION_APPROVAL_THRESHOLD` | `300000.00` | §19 |
| `BUDGET_AUTO_APPROVAL_ENABLED` | `False` until rollout step 5 | §21 |
| `BUDGET_AUTO_APPROVAL_HOURS` | `48` | §21 |
| `BUDGET_AUTO_APPROVAL_WARNING_HOURS` | `44` | §21 |
| `BUDGET_AUTO_APPROVAL_EXEMPT_USER_IDS` | `[]` (business input, §39 N-4) | §21 |
| `BUDGET_VERTICAL_SOURCE_FIELD`, `BUDGET_VERTICAL_VALUE_MAP` | empty | §11.4 |
| `BUDGET_CUTOVER` | `{}` (company → budget head → timestamp) | §31 |
| `BUDGET_INGESTION_COMPANIES` | `['OIL', 'BEVERAGES']` | §7 |
| `BUDGET_SHADOW_MODE` | `False` (True during rollout step 3) | §37 |
| `BUDGET_NOTIFICATION_WATCHERS` | `{}` (workflow code → user ids) | §22, §39 N-9 |

---

## 13. Workflow Stage and Approver Resolution

* `budget_flow.current_stage` = engine stage id; `budget_flow.current_user` is a
  denormalised display/filter copy (BKDT pattern) — never the authority.
* Authority: `get_stage_assignment(current_stage_id)` → configured user → today's
  replacement → effective user. The JWT user must equal it **and** hold the approve key
  (`budget/permissions.py::may_act_on`, BKDT pattern).
* A user on several stages acts only on the stage the document is currently at (F-12).
* Replacement honoured in inbox, actions, notifications and the auto-approval exemption
  check (F-14).
* Reassigning a stage in the engine moves every waiting document instantly (no Budget
  write); `current_user` is refreshed whenever Budget resolves the stage.

---

## 14. Approval Lifecycle

### 14.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Record an approval; advance or complete. |
| **Input** | `POST /api/budget/documents/{id}/approve/ {remarks?}`; actor = JWT user. |
| **Processing** | lock `budget_flow` row (`SELECT … FOR UPDATE`) → guard (`budget_flow.status = PENDING`, actor = effective stage user, key) → **non-final**: `APPROVE` log, point at next stage, SAP `V`, `SAP_WRITE_OK` → **final**: SAP `A`, `SAP_WRITE_OK`, `APPROVE` log, `budget_flow.status = APPROVED`. `documents.routing_status` stays `ROUTED`. |
| **Database** | `budget_flow` (`status`, `current_stage`, `current_user`, `stage_entered_at`, `sap_status`, `sap_payload`, `sap_status_text`), `action_logs`. |
| **Workflow** | `stages_for(workflow)` for the next stage. |
| **SAP** | `V` / `A`, verified, synchronous (§16.4). |
| **Notification** | non-final → next effective user; final → chain approvers. On commit. |
| **Failure** | any error (incl. SAP refusal/verification) rolls back; document unchanged; `SAP_WRITE_FAILED` recorded after rollback (§16.6); approver sees the SAP error. |

### 14.2 Rules

* Stage 1 first; sequential; one approval completes a stage (L-1, L-11).
* A decided stage cannot be decided again: `409`.
* No pull-back, no revoke (legacy `Revoke`/`Cancel` unused by the JSAP app).
* Remarks optional on approve.
* `stage_entered_at` set whenever the flow points at a stage (§21).

### 14.3 Bulk approve

`POST /api/budget/documents/bulk-approve/ {ids[], remarks?}`: each document in its own
transaction; response `[{id, ok, status, error}]`.

---

## 15. Rejection Lifecycle

| Aspect | Design |
|---|---|
| **Purpose** | End the document as Rejected with a reason. |
| **Input** | `POST /api/budget/documents/{id}/reject/ {remarks}` — **remarks required**. |
| **Processing** | lock `budget_flow` → guard → SAP `R` → `SAP_WRITE_OK` → `REJECT` log → `budget_flow.status = REJECTED`, `current_stage = NULL`. `documents.routing_status` stays `ROUTED`. |
| **Database** | `budget_flow`, `action_logs`. |
| **Workflow** | current-stage check only. |
| **SAP** | `R` for every line, verified, synchronous; written only here (F-13). |
| **Notification** | earlier-stage approvers of the document + configured watchers. |
| **Failure** | SAP failure rolls back; stays pending; failure recorded (§16.6). |

After rejection: terminal; no automatic resubmission. A later SAP change →
`CHANGED_AFTER_DECISION` (§8.3); a new line → revision document (§9.4). Bulk reject:
per-document transactions with one shared reason.

---

## 16. Final Approval and SAP/HANA Write-Back

### 16.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Tell SAP the decision per line exactly as JSAP does (L-10). |
| **Input** | document + status `A`/`V`/`R` + remarks. |
| **Processing** | build per-line statements from `documents.lines` → one HANA `DO BEGIN … END` block → verify → record. |
| **Database** | `budget_flow.sap_status`, `sap_payload` (keys + values sent), `sap_status_text` (SAP's reply/error verbatim); `SAP_WRITE_OK` in `action_logs`. |
| **Workflow** | none. |
| **SAP** | `HANAConnection` (existing `hana` app). |
| **Notification** | via the calling approve/reject. |
| **Failure** | raise → caller rolls back; failure recorded after rollback (§16.6). |

### 16.2 Statements (reproduced from `jsSyncBudgetToHanaDraftApproval`, audit §8)

Per line, in the company's schema:

1. **Insert if missing** into `"<schema>"."tbl_Draft_Approvals"`, keyed `DocEntry,
   ObjType, LineNum, VisOrder` — values from SAP `ODRF`/`DRF1` for ObjType 13/14/18/19
   documents, otherwise from the line JSON (Branch, DocEntry, ObjectName, ObjType,
   LineNum, VisOrder, AcctCode, AcctName, CardCode, CardName, DocDate, AMOUNT,
   CURRENTMONTH `MM-YYYY`, BUDGET, SUB_BUDGET, EFFECTMONTH).
2. **Update** `ApprovedStatus = <A|V|R>`, `ACOMMENT = <remarks>` on the same key.

Branch strings: `OIL`, `BEVERAGE` (for OMS `BEVERAGES`), `MART`. All values are bound
parameters. The write-back key of each line is stored in the line JSON
(`wb_doc_entry`, `wb_obj_type`, `wb_line_num`, `wb_vis_order`).

### 16.3 Verification

`SELECT COUNT(DISTINCT key)` of the document's keys now carrying the status; fewer than
the document's distinct keys → `HanaVerifyError`. A document always has lines (§9), so
the legacy "no lines → silent success" path cannot occur.

### 16.4 Timing — synchronous with rollback (DESIGN)

As JSAP and the BKDT precedent: the HANA call runs inside the Django transaction; refusal
or failed verification rolls the decision back (transaction held for the round-trip,
driver timeout ≤ 30 s). Outbox is a future option (§39 N-3).

### 16.5 SAP-side meaning of A/V/R

Not documented; **SAP-side behaviour** (S-1). OMS guarantees the write and verification
only.

### 16.6 Recording a failed write without a technical table

On failure the decision transaction rolls back; the service then writes, in a **new**
transaction, one `SAP_WRITE_FAILED` row in `action_logs` (document, attempted status,
keys, SAP error) and updates `budget_flow.sap_status = FAILED` /
`sap_status_text`. Successful writes are recorded as `SAP_WRITE_OK` inside the
decision's own transaction.

---

## 17. Journal Voucher Handling

### 17.1 Identity inside OMS (L-9)

Each JV line is a distinct element of `documents.lines` with source key
`JV:<company>:<BatchNum>:<TransId>:<Line_ID>` and its own header values from **its own
voucher** (`OBTF` on `BatchNum + TransId`) — no memo/date mix-up.

### 17.2 Write-back key (L-10)

`wb_doc_entry = BatchNum`, `wb_obj_type = 28`, `wb_line_num = wb_vis_order = Line_ID`.
`TransId` is not written (SAP's table has no such column).

### 17.3 Known inherited limitation (not an engine defect)

Two vouchers of one batch sharing a `Line_ID` map to the **same**
`tbl_Draft_Approvals` row: the decision for one is what SAP shows for the other, and
SAP's `A`-filter (reproduced in §7.2) hides a colliding line first seen after that row
became `A` — both exactly as in JSAP. OMS adds **no** blocking or special routing. The
document detail marks a line whose write-back key is shared with another OMS line
(computed at read time). Live collisions today involve no eligible line (audit B-5
evidence §4).

### 17.4 Example

Beverage batch 1843, `TransId 1`, `Line_ID 27`, account 5680014, Factory, 0.13
(audit B-2 evidence §5):

| | Value |
|---|---|
| Source key | `JV:BEVERAGES:1843:1:27` |
| Header | `OBTF(1843, TransId 1)` — memo "FACTORY BELOW May-26" |
| Candidate slice | `(BEVERAGES, 28, 1843, Factory, –, 5680014, JV, –)` |
| Routed to | workflow `BEV_FACTORY_JV` (illustrative) |
| Document identity | `(BEVERAGES, 28, 1843, BEV_FACTORY_JV, rev 1)` |
| Write-back key | `DocEntry 1843, ObjType 28, LineNum 27, VisOrder 27` |
| Note | SAP already holds two rows for that key (JSAP fan-out); the UPDATE sets both; verification counts distinct keys and passes |

---

## 18. Allocation Master and Monthly Allocation

### 18.1 Entities

| Table | Fields | Rules |
|---|---|---|
| `budgets` | company, `name` (= SAP `OcrCode3`, e.g. `Sales`), description, `total_amount`, `is_active` | UNIQUE (company, name) |
| `sub_budgets` | `budget_id`, `name` (= `OcrCode4`, e.g. `GT`), description, `is_active` | UNIQUE (budget_id, name) |
| `monthly_allocations` | `budget_id`, `month`, `allocated_amount`, notes | UNIQUE (budget_id, month) |
| `sub_budget_monthly_allocations` | `sub_budget_id`, `month`, `allocated_amount`, notes | UNIQUE (sub_budget_id, month) |

### 18.2 Direct maintenance vs approval-based change

| Action | Key | Effect | Logged as |
|---|---|---|---|
| Create budget / sub-budget | `budget.allocation.manage` | master rows | `BUDGET_CREATED` / `SUB_BUDGET_CREATED` |
| Set a month's allocation (first time) | manage | row created | `ALLOCATION_SET` |
| **Edit** an existing month directly | manage | amount replaced, explicit edit | `ALLOCATION_EDITED` (old/new) |
| **Request an increase** | `budget.allocation.request` | `allocation_requests` row; amount changes only on final approval | `CREATED` (+ `RECORDED_NO_WORKFLOW`) … `INCREASED_BY_REQUEST` |

Validation: month is the 1st; amounts ≥ 0; sub-budget belongs to the budget. No rule
that sub-budgets sum to the budget amount (§39 N-6).

---

## 19. Allocation Change Request Lifecycle

### 19.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Increase a month's allocation, with approval above the threshold (L-13). |
| **Input** | `POST /api/budget/allocation-requests/ {monthly_allocation_id, increase_amount, reason}`; requester = JWT user. |
| **Processing** | validate (allocation exists, amount > 0) → insert `allocation_requests` with `threshold_applied = BUDGET_ALLOCATION_APPROVAL_THRESHOLD` → **amount > threshold**: `status = IN_WORKFLOW`, `select_for_module(module_code='BUDGET_ALLOCATION', document_id=request.id, company=…)` (same transaction), create `budget_flow` (`flow_type = ALLOCATION_REQUEST`, `status = PENDING`) at stage 1 → **amount ≤ threshold**: `status = RECORDED_NO_WORKFLOW`, no `budget_flow`. Logs: `CREATED`, plus `RECORDED_NO_WORKFLOW` in the second case. |
| **Database** | `allocation_requests`, `budget_flow`, `action_logs`. |
| **Workflow** | engine selection for `BUDGET_ALLOCATION`. |
| **SAP** | none. |
| **Notification** | > threshold → stage-1 effective user; ≤ threshold → requester ("recorded; no approval workflow applies"). |
| **Failure** | request + flow + log in one transaction; `WorkflowNotConfigured` for the company rolls the request back with a clear message. |

### 19.2 Semantics (LOCKED)

* `increase_amount` is an increase; the UI shows "Increase by ₹X → ₹(current + X)".
* Strict compare: `> 300000.00` → workflow; `≤ 300000.00` → no workflow.
* `RECORDED_NO_WORKFLOW` changes nothing and cannot be approved — the decided JSAP
  behaviour; the UI says so at submission.
* **Two separate statuses, no overlap.** `allocation_requests.status` is the request
  lifecycle only — exactly `RECORDED_NO_WORKFLOW` or `IN_WORKFLOW`, never an approval
  outcome. The approval outcome of an `IN_WORKFLOW` request is read **only** from its
  `budget_flow.status` (`PENDING` / `APPROVED` / `REJECTED`).

---

## 20. Allocation Approval Workflow

| Aspect | Design |
|---|---|
| **Purpose** | Approve/reject an allocation increase. |
| **Input** | `POST /api/budget/allocation-requests/{id}/approve/ {remarks?}`, `…/reject/ {remarks}`. |
| **Processing** | lock the request's `budget_flow` → guard (`budget_flow.status = PENDING`, effective stage user, `budget.allocation.approve`, company scope) → approve: next stage, or **final**: `UPDATE budget.monthly_allocations SET allocated_amount = allocated_amount + :increase` under row lock, `APPROVE` (subject allocation request) + `INCREASED_BY_REQUEST` (subject monthly allocation, old/new) logs, `budget_flow.status = APPROVED`; reject: `REJECT` log, `budget_flow.status = REJECTED`, allocation untouched. `allocation_requests.status` stays `IN_WORKFLOW`. |
| **Database** | `budget_flow`, `monthly_allocations` (final approve only), `action_logs`. |
| **Workflow** | stages of the company's `BUDGET_ALLOCATION` workflow (live: one stage for each of OIL, BEVERAGES, MART — audit §4). |
| **SAP** | none. |
| **Notification** | next stage / requester (approved with the resulting amount; rejected with reason). |
| **Failure** | one transaction; any error leaves request and allocation unchanged. |

---

## 21. 48-Hour Auto Approval

### 21.1 Flow summary

| Aspect | Design |
|---|---|
| **Purpose** | Reproduce JSAP auto-approval (L-14). |
| **Input** | tick every **10 minutes** (legacy cadence), when `BUDGET_AUTO_APPROVAL_ENABLED`. |
| **Processing** | candidates → per-document eligibility → per-document transaction performing a stage approval as the **system actor**. |
| **Database** | `budget_flow`, `action_logs` (`AUTO_APPROVE`; `AUTO_APPROVE_SKIPPED` only when the skip reason changes; `AUTO_APPROVE_WARNING` once per document-stage). |
| **Workflow** | `stages_for`; `get_stage_assignment` for the exemption check. |
| **SAP** | `V` / `A` exactly as a manual approval (§16). |
| **Notification** | warning before, notice after (§21.5). |
| **Failure** | each document isolated; failure logged; next tick retries (fixes "one error aborts the run"). |

### 21.2 Candidates and timer

`budget_flow.status = PENDING` (document flows) and `now − stage_entered_at ≥
BUDGET_AUTO_APPROVAL_HOURS` (48).

### 21.3 Budget/allocation eligibility (audited rule, every bucket)

For each (budget_month, budget_head) bucket of the document's lines:

```
doc_amount = Σ signed_amount of this document's lines in the bucket
committed  = Σ signed_amount of lines of documents with budget_flow.status = APPROVED, same company + bucket
allocated  = budget.monthly_allocations(company, budget_head, month).allocated_amount, else 0
eligible   = committed + doc_amount <= allocated
```

Eligible only if every bucket passes (legacy checked one arbitrary bucket). "No
allocation counts as 0" reproduced — nothing auto-approves until allocations exist
(rollout prerequisite, §37). Signed amounts (ObjType 19 negative) for consistency with
reports — confirm §39 N-8.

### 21.4 Exemptions

A stage whose **configured** user is in `BUDGET_AUTO_APPROVAL_EXEMPT_USER_IDS` is never
auto-approved (legacy effect of users 68/79/95, audit §10). No user id in code.

### 21.5 Recording and notification

* `AUTO_APPROVE` row: `actor_type = SYSTEM`, `acted_by = NULL`, `on_behalf_of` = the
  stage's configured user, remarks "Auto-approved after 48 hours idle at stage <name>";
  UI shows "System (auto-approval)". SAP `ACOMMENT` "Auto-approved after 48 hours".
* Warning at `BUDGET_AUTO_APPROVAL_WARNING_HOURS` (44 h) to the effective stage user,
  once (`AUTO_APPROVE_WARNING` row marks it sent). Does not change the 48-hour rule.
* After auto-approval: stage user and next stage (or chain on final) notified.

---

## 22. Notifications

`notifications.services.notify`, handlers registered from `budget/hooks.py`, emitted on
commit. Recipients from runtime state (effective stage user) — never configuration ids.

| Event | Recipients | Channel |
|---|---|---|
| `budget.document.pending` | effective user of the current stage | push + in-app |
| `budget.document.changed` | effective user of the current stage | in-app |
| `budget.document.approved` | chain approvers + configured watchers | in-app |
| `budget.document.rejected` | earlier-stage approvers + watchers | push + in-app |
| `budget.document.auto_approve_warning` | effective stage user | push + in-app |
| `budget.document.auto_approved` | the stage user | in-app |
| `budget.allocation.request_pending` | effective stage user | push + in-app |
| `budget.allocation.request_decided` | requester | push + in-app |
| `budget.allocation.request_recorded` | requester | in-app |
| `budget.routing.exceptions_digest` | holders of `budget.config.manage` | in-app, ≤ hourly |
| `budget.pending_reminder` | users with pending items | push, daily |

---

## 23. Permissions / RBAC

Registered in `core/permission_registry.py` under `'budget'`; `HasKey` on every view;
acting additionally requires being the effective stage user.

| Key | Grants |
|---|---|
| `budget.document.view` | documents the user acts on or acted on |
| `budget.document.view_all` | every document of the user's companies |
| `budget.document.approve` | approve/reject as effective stage user |
| `budget.allocation.view` | budgets, sub-budgets, allocations, requests |
| `budget.allocation.manage` | create/edit master and allocations directly |
| `budget.allocation.request` | raise increase requests |
| `budget.allocation.approve` | approve/reject requests as effective stage user |
| `budget.config.manage` | exceptions list, re-route, routing dry-run |
| `budget.reports.view` | dashboards and reports |

Workflows/queries/stages themselves are managed under the engine's existing
`workflow.config.manage`. Company scope applied to every queryset.

---

## 24. API Architecture

Base `/api/budget/`; actor always from JWT.

| Method & path | Key | Purpose |
|---|---|---|
| `GET documents/?tab=pending\|approved\|rejected\|all&month=&company=&budget_head=` | view / view_all | inbox; approved/rejected = my own actions (P-9) |
| `GET documents/{id}/` | view | header, lines, flow timeline (engine stages + `action_logs`), attachments (§8.5), SAP status |
| `POST documents/{id}/approve/`, `reject/` | approve | §14, §15 |
| `POST documents/bulk-approve/`, `bulk-reject/` | approve | per-document results |
| `GET routing-exceptions/?status=UNROUTED\|AMBIGUOUS` | config.manage | `documents` rows in those statuses + `CHANGED_AFTER_DECISION` / `SOURCE_*` log entries |
| `POST routing-exceptions/{id}/reroute/`, `bulk-reroute/` | config.manage | re-route lifecycle of §10.3 (reset to `ROUTING` + selection, under the Budget document write lock) |
| `POST routing-dry-run/` | config.manage | §11.5 |
| `GET/POST/PATCH budgets/`, `sub-budgets/` | allocation.view / manage | master |
| `GET/POST/PATCH monthly-allocations/`, `sub-budget-allocations/` | allocation.view / manage | §18 |
| `GET/POST allocation-requests/` | allocation.view / request | §19 |
| `POST allocation-requests/{id}/approve/`, `reject/` | allocation.approve | §20 |
| `GET reports/summary`, `reports/buckets` | reports.view | §27 |

Errors: `400` validation · `403` permission / not the effective stage user · `404`
scope · `409` stale state · `502` SAP write refused (SAP error text).

---

## 25. Web UI Architecture

OMS-Frontend, consistent with BackDate/Payments pages (newest first, status badges,
result dialog after a decision).

| Page | Content |
|---|---|
| Budget Inbox | Pending / Approved / Rejected / All; document, SAP type, DocEntry, budget head/sub-budget, month, party, amount, stage, age; bulk actions |
| Document Detail | header; lines (SAP reference figures, JV `TransId`, shared-SAP-key marker); flow timeline; attachments; SAP write status; approve/reject dialog (reason required on reject) |
| Routing Exceptions | UNROUTED / AMBIGUOUS rows; changed/withdrawn/ineligible events; re-route |
| Allocations | budgets & sub-budgets; month grid; direct edit |
| Allocation Requests | create (≤ ₹3,00,000 notice), list, detail with flow, approve/reject |
| Dashboard | month × budget head: allocated, committed, pending, available, overspend |

Workflows, queries, stages and replacements are edited in the existing Workflows UI.
Mobile approver screens: later phase (§40).

---

## 26. Background Jobs / Schedulers

`sap_sync.scheduler` helpers; each job under an advisory lock; everything re-derivable
from table state (the job store is not relied on).

| Job | Cadence | Does |
|---|---|---|
| `budget.ingest` | 5 min | §7–§10 for each company in `BUDGET_INGESTION_COMPANIES` |
| `budget.auto_approve` | 10 min | §21 |
| `budget.auto_approve_warning` | 30 min | §21.5 |
| `budget.exception_digest` | hourly | §22 |
| `budget.pending_reminder` | daily 10:00 | §22 |

(No attachment job — attachments are read on demand, §8.5. No housekeeping job — there
are no technical tables to purge.)

---

## 27. Reporting / Dashboard

### 27.1 Definitions (one meaning everywhere)

For company, month `m`, budget head `b` (optionally sub-budget `s`):

| Measure | Definition |
|---|---|
| **allocated** | `monthly_allocations(b, m).allocated_amount` (sub-budget view: `sub_budget_monthly_allocations(s, m)`); 0 if none |
| **committed (approved)** | Σ `signed_amount` of lines in (b[, s], m) of `ROUTED` documents whose `budget_flow.status = APPROVED` |
| **pending** | same where `budget_flow.status = PENDING` |
| **available** | `allocated − committed` (may be negative) |
| **overspend** | `max(0, committed − allocated)` |
| **projected available** | `allocated − committed − pending` (informational) |

`signed_amount = −amount` for ObjType 19. Per budget, never per viewer. SAP's own
`Current_month_Budget` / `Current_month_Posted_Amount` shown as reference only.

### 27.2 Implementation

Aggregates over `jsonb_to_recordset(documents.lines)` joined to `budget_flow` and the
allocation tables, filtered by company/month/budget head. If volume requires, an
expression index or a materialised view is a later optimisation (not a table of
record).

---

## 28. Audit History and Action Logs

One append-only table, `budget.action_logs`, for every business event (BKDT
`backdate_action_logs` pattern generalised to Budget's six subjects). No other history
table exists.

**Canonical action names** (the only names used anywhere in this plan):

| Group | Actions |
|---|---|
| Document / flow | `CREATED`, `LINE_ADDED`, `LINE_CHANGED`, `LINE_REMOVED`, `APPROVE`, `REJECT`, `AUTO_APPROVE`, `AUTO_APPROVE_WARNING`, `AUTO_APPROVE_SKIPPED`, `SAP_WRITE_OK`, `SAP_WRITE_FAILED`, `REROUTED`, `WITHDRAWN`, `SOURCE_WITHDRAWN`, `SOURCE_NOT_ELIGIBLE`, `CHANGED_AFTER_DECISION` |
| Budget / master / allocation | `BUDGET_CREATED`, `SUB_BUDGET_CREATED`, `ALLOCATION_SET`, `ALLOCATION_EDITED`, `RECORDED_NO_WORKFLOW`, `INCREASED_BY_REQUEST` |

**Subjects** (`subject_type` → the one populated subject column):

| `subject_type` | Column | Actions used |
|---|---|---|
| `DOCUMENT` | `document_id` | document/flow group |
| `ALLOCATION_REQUEST` | `allocation_request_id` | `CREATED`, `RECORDED_NO_WORKFLOW`, `APPROVE`, `REJECT` |
| `BUDGET` | `budget_id` | `BUDGET_CREATED` |
| `SUB_BUDGET` | `sub_budget_id` | `SUB_BUDGET_CREATED` |
| `MONTHLY_ALLOCATION` | `monthly_allocation_id` | `ALLOCATION_SET`, `ALLOCATION_EDITED`, `INCREASED_BY_REQUEST` |
| `SUB_BUDGET_MONTHLY_ALLOCATION` | `sub_budget_monthly_allocation_id` | `ALLOCATION_SET`, `ALLOCATION_EDITED` |

Each row: `actor_type` (`USER`/`SYSTEM`), `acted_by` (nullable), `on_behalf_of`
(replacement or system), `stage` (engine stage id, nullable), `remarks`, `action_data`
(JSON: old/new values, SAP keys and reply, skip reason), `acted_at`. No UPDATE/DELETE
grants for the application role on this table.

There is **no `flow` column**: a document has exactly one `budget_flow`, and so does a
workflow-backed allocation request, so a log row reaches its flow through its subject
(`documents` → `budget_flow`, `allocation_requests` → `budget_flow`) — the same reason
`backdate_action_logs` has none.

Deliberately **not** stored: per-run ingestion statistics, every auto-approval
evaluation, successful attachment reads — these go to the application log (rotated by
the existing logging setup), fixing F-20.

---

## 29. Error Handling / Retry / Idempotency

| Risk | Control |
|---|---|
| Double approve / double click | `SELECT … FOR UPDATE` on `budget_flow`, stage check, `lock_version` |
| Overlapping ingestion / re-route / cleanup | Budget document write lock (§8.2); a busy ingestion tick is skipped and logged |
| A line in two documents | serialized write path + `line_keys` check (GIN-indexed) — application-level guarantee (§8.2) |
| Duplicate document | partial UNIQUE `(company, obj_type, doc_entry, workflow_id, revision)` |
| SAP write fails | decision rolls back; `SAP_WRITE_FAILED` recorded in a new transaction |
| SAP write succeeded, DB commit then failed | idempotent statements (insert-if-missing + same UPDATE); retry re-writes and verifies |
| One company's HANA read fails | that company skipped this tick; other proceeds |
| Engine condition error | SAP document's transaction rolled back; retried next tick |
| Auto-approval failure | per document; logged; retried |
| Allocation increase concurrency | `UPDATE … SET allocated_amount = allocated_amount + :x` under row lock |

---

## 30. Multi-Company Behaviour

| Company | Documents | Allocation workflow | Notes |
|---|---|---|---|
| OIL | yes (`JIVO_OIL_HANADB`) | yes | |
| BEVERAGES | yes (`JIVO_BEVERAGES_HANADB`) | yes | SAP branch string `BEVERAGE` |
| MART | not ingested (legacy never processed company 3) | yes (live template, audit §4) | enable = add to `BUDGET_INGESTION_COMPANIES` + queries (§39 N-2) |

Documents never span companies; each company's ingestion, queries, workflows and SAP
writes are independent.

---

## 31. JSAP/OMS Coexistence and SAP Ownership

Rule (L-19): one system writes each SAP document's decision. Unit: company × budget
head, switched at a cut-over timestamp `T` held in `BUDGET_CUTOVER`.

| Step | Where | Action |
|---|---|---|
| 1 | OMS | Budgets, allocations, workflows, queries (with `first_seen_at >= T`) for the unit. |
| 2 | OMS | Ingestion **stores and routes** a SAP line of the unit only if its SAP document was created at or after `T` and its `tbl_Draft_Approvals` row carries no decision; older lines are skipped each run (not stored — no table needed). |
| 3 | JSAP administrators, at `T` | deactivate the matching JSAP templates (`isActive = 0`). JSAP configuration, not performed by this module. |
| 4 | Both | JSAP finishes its own existing documents; OMS never sees them (step 2). |

Rollback of a unit: remove its OMS queries and reactivate the JSAP template; documents
already decided in OMS stay decided.

---

## 32. Security Rules

* JWT on every endpoint; permission key server-side; company scope in every queryset.
* Actor = `request.user`; request bodies carry no user id.
* Approve/reject also require being the effective stage user.
* SAP write-back uses bound parameters; schema chosen from a fixed per-company map;
  credentials from the existing `hana` configuration.
* Routing conditions validated and executed only by the engine's existing validator
  and central condition executor; Budget executes no admin-supplied SQL.
* `action_logs` append-only at grant level.
* Attachment downloads authorised by document visibility.

---

## 33. Database Design Proposal

Exactly **eight** tables in PostgreSQL schema `budget` (`db_table =
'budget"."<name>'`, `CREATE SCHEMA IF NOT EXISTS budget` in `0001_initial`). No
Workflow Engine runtime tables (none exist in the final engine architecture), no
staging, line, routing, technical-log or settings tables. Proposal only — no migration
is created by this plan.

### 33.1 Responsibilities

| # | Table | Responsibility | Replaces (earlier revisions) |
|---|---|---|---|
| 1 | `budget.budgets` | budget master per company (= SAP budget head) | — |
| 2 | `budget.sub_budgets` | sub-budgets of a budget (= SAP sub-budget) | — |
| 3 | `budget.monthly_allocations` | allocated amount per budget per month | — |
| 4 | `budget.sub_budget_monthly_allocations` | allocated amount per sub-budget per month | — |
| 5 | `budget.documents` | a Budget approval document / routing slice **with its SAP lines (JSON)**; also holds candidate and exception slices (`ROUTING`, `UNROUTED`, `AMBIGUOUS`) | `source_line`, `document_line`, `routing_exception`, routing-match view |
| 6 | `budget.allocation_requests` | allocation increase requests (incl. no-workflow ones) | — |
| 7 | `budget.budget_flow` | one approval flow per document and per workflow-backed allocation request; SAP write status | `document_flow`, `allocation_request_flow`, `sap_write` |
| 8 | `budget.action_logs` | append-only history of every business event | `document_action_log`, `allocation_request_action_log`, `allocation_action_log`, `auto_approval_evaluation` |

Removed without replacement table: `source_attachment` (on-demand read, §8.5),
`ingestion_run` (application log), `routing_rule`, `routing_rule_account`,
`routing_dimension` (engine queries + settings, §11), `auto_approval_policy`,
`settings` (Django settings, §12.3).

### 33.2 `budget.budgets`

`id` · `company` · `name` · `description` · `total_amount numeric(18,2)` · `is_active`
· `created_at` · `updated_at` · UNIQUE (`company`, `name`).

### 33.3 `budget.sub_budgets`

`id` · `budget_id` FK → budgets (PROTECT) · `name` · `description` · `is_active` ·
`created_at` · `updated_at` · UNIQUE (`budget_id`, `name`).

### 33.4 `budget.monthly_allocations`

`id` · `budget_id` FK · `month date` CHECK day = 1 · `allocated_amount numeric(18,2)`
CHECK ≥ 0 · `notes` · `created_at` · `updated_at` · UNIQUE (`budget_id`, `month`).

### 33.5 `budget.sub_budget_monthly_allocations`

`id` · `sub_budget_id` FK · `month date` CHECK day = 1 · `allocated_amount` CHECK ≥ 0 ·
`notes` · `created_at` · `updated_at` · UNIQUE (`sub_budget_id`, `month`).

### 33.6 `budget.documents`

A Budget approval document = one routing slice of one SAP document (L-8). One SAP
document may have several rows (one per selected workflow, plus revisions and
exception slices).

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint PK | engine `document_id` |
| `company` | varchar | `OIL` / `BEVERAGES` |
| `obj_type` | smallint | SAP ObjType (28 = JV) |
| `doc_entry` | integer | SAP DocEntry (JV: BatchNum) |
| `object_name`, `card_code`, `card_name`, `doc_date` | | SAP header values |
| `budget_head`, `sub_budget`, `account_code`, `document_class`, `vertical_value` | varchar | routing columns (§10.2); NULL after merge when lines differ |
| `budget_month` | date NULL | first of month; NULL if lines differ |
| `first_seen_at` | timestamptz | earliest line load time (routing effective date) |
| `routing_status` | varchar | `ROUTING` · `ROUTED` · `UNROUTED` · `AMBIGUOUS` · `WITHDRAWN` |
| `routing_detail` | jsonb NULL | matched workflows for `AMBIGUOUS`, engine error text |
| `workflow_id` | FK → `workflow.Workflow` NULL (PROTECT) | selected workflow |
| `revision` | smallint default 1 | supplementary document counter (§9.4) |
| `total_amount`, `signed_total` | numeric(18,2) | Σ lines |
| `lines` | jsonb | array of line objects (§7.3 fields + source key + write-back key) |
| `line_keys` | text[] | source keys of `lines`, GIN-indexed (§8.2) |
| `created_at`, `updated_at` | timestamptz | |

Constraints/indexes: partial UNIQUE (`company`, `obj_type`, `doc_entry`, `workflow_id`,
`revision`) WHERE `routing_status = 'ROUTED'`; GIN (`line_keys`); index (`company`,
`routing_status`); index (`company`, `obj_type`, `doc_entry`); CHECK
`jsonb_array_length(lines) > 0` unless `routing_status = 'WITHDRAWN'`; CHECK
`routing_status IN ('ROUTING','ROUTED','UNROUTED','AMBIGUOUS','WITHDRAWN')`.

`routing_status` is the routing/ingestion lifecycle **only**; the approval lifecycle
is `budget_flow.status`. A `documents` row has a `budget_flow` only once it is `ROUTED`.

**Line uniqueness** across rows is not a database constraint: it is guaranteed by the
serialized Budget document write path plus the `line_keys` check (§8.2), with the GIN
index on `line_keys` for the lookup.

Line object (JSON): `source_key`, `source_kind`, `obj_type`, `doc_entry`, `line_num`,
`vis_order`, `trans_id` (JV), `line_id` (JV), `wb_doc_entry`, `wb_obj_type`,
`wb_line_num`, `wb_vis_order`, business fields of §7.3, `amount`, `signed_amount`,
`budget_month`, `content_hash`, `first_seen_at`, `last_seen_at`, `missing_since`.

### 33.7 `budget.allocation_requests`

`id` · `company` · `monthly_allocation_id` FK · `increase_amount numeric(18,2)` CHECK
> 0 · `threshold_applied numeric(18,2)` · `reason` · `requested_by` FK user ·
`status` — request lifecycle only: CHECK `status IN ('RECORDED_NO_WORKFLOW',
'IN_WORKFLOW')` · `created_at` · `updated_at`. Index (`status`); index
(`monthly_allocation_id`). The approval outcome is **not** stored here — it is
`budget_flow.status` of the request's flow (exists only when `IN_WORKFLOW`).

### 33.8 `budget.budget_flow`

One row per workflow-backed subject (BKDT `backdate_flow` pattern).

| Column | Meaning |
|---|---|
| `id` | |
| `flow_type` | `DOCUMENT` / `ALLOCATION_REQUEST` |
| `document_id` | UNIQUE FK → documents, NULL for allocation flows |
| `allocation_request_id` | UNIQUE FK → allocation_requests, NULL for document flows |
| `status` | `PENDING` / `APPROVED` / `REJECTED` / `WITHDRAWN` |
| `workflow_id` | FK → `workflow.Workflow` (PROTECT) |
| `current_stage` | FK → `workflow.WorkflowStage` NULL — the authority reference |
| `current_user` | FK user NULL — denormalised display/filter copy |
| `total_stage` | stages at creation |
| `stage_entered_at` | when the flow reached `current_stage` (§21) |
| `decided_at` | final decision time |
| `sap_status` | `NOT_REQUIRED` / `OK` / `FAILED` (documents only) |
| `sap_payload` | jsonb — last keys/values sent to SAP |
| `sap_status_text` | SAP reply / error verbatim |
| `lock_version` | optimistic counter |
| `created_at`, `updated_at` | |

CHECK: (`flow_type = 'DOCUMENT'` AND `document_id` IS NOT NULL AND
`allocation_request_id` IS NULL) OR (`flow_type = 'ALLOCATION_REQUEST'` AND
`allocation_request_id` IS NOT NULL AND `document_id` IS NULL). Index (`status`,
`current_stage`), (`flow_type`, `status`).

### 33.9 `budget.action_logs`

| Column | Meaning |
|---|---|
| `id` | |
| `subject_type` | `DOCUMENT` / `ALLOCATION_REQUEST` / `BUDGET` / `SUB_BUDGET` / `MONTHLY_ALLOCATION` / `SUB_BUDGET_MONTHLY_ALLOCATION` |
| `document_id` | FK → documents, NULL |
| `allocation_request_id` | FK → allocation_requests, NULL |
| `budget_id` | FK → budgets, NULL |
| `sub_budget_id` | FK → sub_budgets, NULL |
| `monthly_allocation_id` | FK → monthly_allocations, NULL |
| `sub_budget_monthly_allocation_id` | FK → sub_budget_monthly_allocations, NULL |
| `action` | §28 |
| `actor_type` | `USER` / `SYSTEM` |
| `acted_by` | FK user NULL (NULL for SYSTEM) |
| `on_behalf_of` | FK user NULL |
| `stage` | FK → `workflow.WorkflowStage` NULL |
| `remarks` | text |
| `action_data` | jsonb |
| `acted_at` | timestamptz, indexed |

CHECK (one per subject type, exactly one subject column populated):

```text
(subject_type = 'DOCUMENT'                      AND document_id IS NOT NULL
   AND allocation_request_id IS NULL AND budget_id IS NULL AND sub_budget_id IS NULL
   AND monthly_allocation_id IS NULL AND sub_budget_monthly_allocation_id IS NULL)
OR (subject_type = 'ALLOCATION_REQUEST'         AND allocation_request_id IS NOT NULL AND <the other five> IS NULL)
OR (subject_type = 'BUDGET'                     AND budget_id IS NOT NULL AND <the other five> IS NULL)
OR (subject_type = 'SUB_BUDGET'                 AND sub_budget_id IS NOT NULL AND <the other five> IS NULL)
OR (subject_type = 'MONTHLY_ALLOCATION'         AND monthly_allocation_id IS NOT NULL AND <the other five> IS NULL)
OR (subject_type = 'SUB_BUDGET_MONTHLY_ALLOCATION' AND sub_budget_monthly_allocation_id IS NOT NULL AND <the other five> IS NULL)
```

plus CHECK `action` ∈ the canonical list of §28 and CHECK `actor_type IN ('USER',
'SYSTEM')`. Indexes: (`document_id`, `acted_at`), (`allocation_request_id`,
`acted_at`), (`monthly_allocation_id`, `acted_at`), (`acted_by`, `action`) for
"approved/rejected by me".

---

## 34. Data Flow Diagrams

### 34.1 Ingestion → document

```text
HANA drafts / payment drafts / vouchers (+TransId, header on BatchNum+TransId)
        │ read-only SELECT (5 min, Budget document write lock)
        ▼
match rows to documents.line_keys ──changed──► update line JSON + action_logs (LINE_CHANGED …)
        │ new & eligible
        ▼
group into candidate slices (company, ObjType, DocEntry, budget_head, sub_budget,
                             account_code, JV/NON_JV, vertical)
        │  slice already has an UNROUTED/AMBIGUOUS row? → add line there (LINE_ADDED), stop
        ▼  else documents row routing_status = ROUTING
        │  (same transaction, same connection)
        ▼ select_for_module(BUDGET, document_id = candidate.id)          [engine, unchanged]
   ┌────┼──────────────┐
   0    1              >1
   │    │               │
UNROUTED │           AMBIGUOUS        (rows stay as exception records until re-route)
        ▼
 workflow W: doc for (company, ObjType, DocEntry, W) with budget_flow.status = PENDING?
                 ─ yes → merge lines, delete candidate
             doc with budget_flow.status APPROVED/REJECTED?
                 ─ yes → candidate = revision n+1 (ROUTED) + budget_flow(PENDING)
             none ─► candidate = ROUTED document + budget_flow(PENDING, stage 1)
        ▼
 on_commit: notify stage-1 effective user
```

### 34.2 Decision → SAP

```text
Approver (JWT) ─► approve / reject ─► lock budget_flow ─► guard (effective user + key)
    non-final approve ─► APPROVE log ─► next stage ─► SAP 'V'                    ┐
    final approve     ─► SAP 'A' ─► APPROVE log ─► budget_flow.status APPROVED   ├─ HANA DO BEGIN…END + verify
    reject            ─► SAP 'R' ─► REJECT log  ─► budget_flow.status REJECTED   ┘
         success → SAP_WRITE_OK logged in the same transaction
         failure → ROLLBACK → new txn: SAP_WRITE_FAILED log + budget_flow.sap_status = FAILED
         success → on_commit notifications
```

### 34.3 Allocation increase

```text
POST request (increase X)
  ├─ X ≤ 3,00,000 ─► allocation_requests.status = RECORDED_NO_WORKFLOW (no flow, allocation unchanged)
  │                  ─► CREATED + RECORDED_NO_WORKFLOW logged ─► notify requester
  └─ X > 3,00,000 ─► allocation_requests.status = IN_WORKFLOW
                     ─► select_for_module(BUDGET_ALLOCATION, document_id = request.id)
                     ─► budget_flow(ALLOCATION_REQUEST, status PENDING, stage 1) ─► notify stage user
                         final approve ─► monthly_allocations += X ─► budget_flow.status APPROVED
                                          (APPROVE + INCREASED_BY_REQUEST logged)
                         reject        ─► budget_flow.status REJECTED (allocation unchanged)
```

---

## 35. State Transition Diagrams

### 35.1 `documents.routing_status`

```text
 new slice ─► ROUTING ──engine 1──► ROUTED ──(all lines moved away)──► WITHDRAWN
                 │  ╲                                                  (+ budget_flow WITHDRAWN)
          engine 0   engine >1
                 ▼      ▼
             UNROUTED  AMBIGUOUS ──re-route: set ROUTING, select again──► ROUTING → 0 / 1 / >1
                 └──────┴── new line of the same slice is added here (status unchanged)
   (a ROUTING candidate merged into an existing document is deleted in the same transaction;
    every transition here runs under the Budget document write lock, §8.2)
```

### 35.2 `budget_flow.status` (documents)

```text
            approve non-final (SAP V) / auto-approve non-final
           ┌──────────────┐
           ▼              │
 created ─► PENDING(stage k) ──approve final (SAP A) / auto-approve final──► APPROVED
               ├── reject (SAP R) ─────────────────────────────────────────► REJECTED
               └── all lines removed ───────────────────────────────────────► WITHDRAWN (no SAP)
```

### 35.3 `allocation_requests.status`

```text
 create ─┬─ X ≤ 3,00,000 ─► RECORDED_NO_WORKFLOW   (terminal; no budget_flow)
         └─ X > 3,00,000 ─► IN_WORKFLOW            (terminal lifecycle value; approval outcome
                                                    lives in budget_flow.status:
                                                    PENDING ─final approve─► APPROVED (allocation += X)
                                                            └─reject──────► REJECTED)
```

---

## 36. Testing Strategy

| Layer | Tests |
|---|---|
| Ingestion | source keys per branch; JV `TransId` captured and header joined on `BatchNum + TransId` (2-voucher fixture: no fan-out, correct memo); unchanged/changed/missing lines inside `lines` JSON; `A`-filter; eligibility; advisory lock; cut-over skip |
| Parity (gated, read-only) | OMS non-JV keys vs `CALL DRAFT_APPROVAL`; JV rows equal after removing the procedure's fan-out |
| Candidate slices | grouping by routing values; one SAP document → two workflow documents; merge into pending; revision after decision; moved line; zero-line document impossible |
| Routing | engine 0/1/>1 → `UNROUTED`/`ROUTED`/`AMBIGUOUS`; re-route; dry-run; queries pass the engine validator; vertical setting off/on |
| Line uniqueness | a key never placed in two non-withdrawn documents |
| Approval / rejection | stage order; effective-user guard with replacement; 403/409; final → APPROVED; reason required; SAP `R` only on reject; bulk per-item results |
| SAP write-back | 13/14/18/19 from ODRF/DRF1, others from line JSON; bound parameters; verification failure → rollback + `SAP_WRITE_FAILED` row survives; `BEVERAGE` mapping; JV key; duplicate SAP rows counted once |
| Allocation | increase math; 3,00,000.00 → no workflow, 3,00,000.01 → workflow; rejection leaves amount; direct edit logged; MART selection |
| Auto-approval | 47h59 vs 48h00; every bucket; no allocation = 0; exemption by configured user; SYSTEM actor; skip logged only on change; warning once; per-document isolation |
| Reports | §27.1 incl. ObjType 19 sign and overspend |
| Schema | exactly eight tables; constraints (partial unique, CHECKs, flow type); action-log append-only grants |
| Permissions | endpoint × key matrix; company scope |

PostgreSQL for all tests (jsonb, arrays, partial unique, engine CHECK). HANA via a
recording double except the gated parity test.

---

## 37. Deployment / Rollout Plan

1. Deploy dark (modules registered, jobs off, no keys granted).
2. Configure: budgets, sub-budgets, allocations (≥ current month — auto-approval needs
   them); workflows, queries, stages in the engine; settings (§12.3), exemptions.
3. **Shadow run:** ingestion stores and routes candidate slices but creates no flows
   (`BUDGET_SHADOW_MODE = True`) for one week; compare with JSAP; fix queries until no
   unexpected `UNROUTED`/`AMBIGUOUS`. Shadow rows are deleted before step 4, by a cleanup
   that runs under the Budget document write lock (§8.2).
4. **Pilot unit** (one company × budget head, e.g. OIL `Transprt`) at cut-over `T`
   (§31); documents, approvals and SAP write-back on for that unit.
5. Enable auto-approval for the pilot after one full 48-hour cycle.
6. Expand unit by unit (Oil, then Beverage).
7. Allocation requests live per company once its allocations are in OMS.
8. Mobile approver screens (§40 phase 9).

---

## 38. Known SAP/Legacy Limitations

| # | Limitation | Consequence | OMS stance |
|---|---|---|---|
| K-1 | `tbl_Draft_Approvals` has no `TransId` / no key | colliding JV lines share one SAP decision row | inherited, documented, not blocked; not an engine defect |
| K-2 | SAP-side meaning of `A`/`V`/`R` undocumented | cannot state what SAP enforces | write + verify only |
| K-3 | SAP already holds duplicate rows from JSAP's JV fan-out | UPDATE touches both | verification counts distinct keys |
| K-4 | Ingestion mirrors `DRAFT_APPROVAL` as of 2026-08-14 | SAP-side procedure changes not picked up automatically | parity test |
| K-5 | SAP `A`-filter hides a colliding line once its shared key is `A` | such a line is never ingested (as in JSAP) | reproduced |
| K-6 | Settings live in Django settings (L-20) | threshold/policy changes need a deployment | accepted; audit trail = deployment history |
| K-7 (S-4) | `DRAFT_APPROVAL_ATC1` confirmed in `JIVO_OIL_HANADB` only | Beverage attachment availability unverified | Beverage shows "attachments unavailable" until verified (§8.5) |

---

## 39. Open Non-Blocking Decisions

| # | Question | Default |
|---|---|---|
| N-1 | SAP field for the business vertical | setting empty; `vertical_value` NULL |
| N-2 | Budget-control Mart documents? | not ingested; Mart allocation workflow supported |
| N-3 | Outbox + retry for SAP write-back? | synchronous with rollback |
| N-4 | Initial auto-approval exempt users | business input at rollout |
| N-5 | Approver recall of a decision? | no |
| N-6 | Sub-budget allocations must sum to the budget allocation? | no rule |
| N-7 | Effective date vs first-seen time or SAP document date? | first-seen (`first_seen_at`) |
| N-8 | Auto-approval check with signed amounts? | signed |
| N-9 | Approval/rejection watchers | configured per workflow via settings; empty by default |
| N-10 | Warning lead time | 4 h (44 h) |

---

## 40. Implementation Phases

| Phase | Deliverable | Depends on |
|---|---|---|
| 1 | `budget` app, schema, the eight tables, module registration (`BUDGET`, `BUDGET_ALLOCATION`), permission keys, settings | — |
| 2 | Budgets, sub-budgets, monthly allocations, direct maintenance API/UI, allocation logs | 1 |
| 3 | Allocation requests + `BUDGET_ALLOCATION` flow + approve/reject + notifications | 2 |
| 4 | Ingestion (SAP queries, line JSON upsert, change detection, parity test), on-demand attachments | 1 |
| 5 | Candidate slices, engine selection, merge/revision, routing exceptions, re-route, dry-run | 4 |
| 6 | Document flow, approve/reject, bulk, inbox/detail API + UI | 5 |
| 7 | SAP write-back (A/V/R, verification, failure recording) | 6 |
| 8 | Auto-approval + warnings; reports/dashboard | 3, 7 |
| 9 | Coexistence controls, shadow mode, rollout; mobile approver screens | 7 |

---

## Concrete examples

Values illustrative except where the audit is cited.

### E-1 Oil Sales budget entry

SAP A/P invoice draft (18) DocEntry 57001, OIL: line 0 account 5640004, Sales/GT,
₹42,000; line 1 account 5650001, Sales/GT, ₹8,000.

1. Two candidate slices (accounts differ): `(OIL,18,57001,Sales,GT,5640004,NON_JV)` and
   `(…,5650001,…)`, each a `ROUTING` row.
2. Query of workflow `OIL_SALES_GT` (§11.1) matches both → both select `OIL_SALES_GT`
   (legacy "sales oil gt v5 1", one stage, user A — audit §4).
3. First candidate becomes document `(OIL, 18, 57001, OIL_SALES_GT, rev 1)` with a
   `budget_flow` at stage 1; the second merges into it (`LINE_ADDED`); total ₹50,000;
   user A notified.

### E-2 Oil Production/Supply Chain budget entry

SAP goods issue draft (60) DocEntry 88010, OIL, line 0 budget `Factory`, ₹1,20,000 →
workflow `OIL_FACTORY` (legacy "factory oil v5 1", user B). If the business confirms
the SAP field for "Production/Supply Chain", `BUDGET_VERTICAL_SOURCE_FIELD` is set and
the query adds `AND vertical_value = 'Production'` — no code or table change.

### E-3 Beverage budget entry

SAP A/P invoice draft (18) DocEntry 9501, BEVERAGES, line 0 `Del Bkhp`, ₹15,500 →
workflow `BEV_DEL_BKHP` (legacy "del bhk bev v5 1"). Final approval writes
`Branch 'BEVERAGE'`, key `9501 / 18 / 0 / 0`, `ApprovedStatus 'A'` in
`JIVO_BEVERAGES_HANADB`.

### E-4 Two-stage approval

Workflow `OIL_NPD3`: stage 1 user C, stage 2 user D (legacy two-stage "npd3 extra oil
1/2 v1", audit §4). Document D-77:

| Step | Actor | OMS | SAP |
|---|---|---|---|
| created | system | `routing_status ROUTED`; `budget_flow.status PENDING`, stage 1; `CREATED`; notify C | — |
| C approves | C (JWT) | `APPROVE`, `SAP_WRITE_OK`; stage 2; notify D | `V`, verified |
| D approves | D | `SAP_WRITE_OK`, `APPROVE`; `budget_flow.status APPROVED`; notify chain | `A`, verified |

With replacement D→E (10–15 Oct), E approves on those dates; the log records `acted_by
E`, `on_behalf_of D`.

### E-5 Rejected entry

D-78 at stage 1 (user A). A rejects "No supporting document uploaded" → SAP `R`
verified (`SAP_WRITE_OK`) → `REJECT` log → `budget_flow.status = REJECTED` → notifications. If SAP refuses: rollback, A sees the
SAP error, `SAP_WRITE_FAILED` logged. A later SAP edit → `CHANGED_AFTER_DECISION`; a new
line → revision 2.

### E-6 Final approval

E-4's last step: SAP `A` first inside the transaction; only when every distinct line key
verifies does `budget_flow.status` become `APPROVED` and D-77's lines count as committed for their
month and budget head.

### E-7 Allocation increase above ₹3,00,000

OIL `Factory`, October 2026, allocated ₹10,00,000. User R requests an increase of
₹5,00,000 → `allocation_requests.status = IN_WORKFLOW` → workflow `OIL_ALLOCATION` (legacy "budget allocation oil 1",
user F) → `budget_flow` stage 1 → F approves → `monthly_allocations` 10,00,000 +
5,00,000 = 15,00,000; `budget_flow.status = APPROVED`; `APPROVE` + `INCREASED_BY_REQUEST`
(old/new) logged; R notified. Rejected → `budget_flow.status = REJECTED`, stays 10,00,000.
`allocation_requests.status` remains `IN_WORKFLOW` throughout.

### E-8 Allocation request at or below ₹3,00,000

Same allocation, increase ₹3,00,000 → `allocation_requests.status = RECORDED_NO_WORKFLOW`;
`CREATED` + `RECORDED_NO_WORKFLOW` logged; no `budget_flow`; allocation stays
₹10,00,000; R told at submission and by notification. ₹3,00,000.01 would start the
workflow.

### E-9 48-hour auto-approval

D-90 (OIL, Sales/GT, October, ₹40,000) reached stage 1 (user A, not exempt) on 2 Oct
09:00. October Sales allocation ₹5,00,000; committed ₹4,40,000.

* 4 Oct 05:00 (44 h): warning to A; `AUTO_APPROVE_WARNING` logged.
* 4 Oct 09:00+ tick: 4,40,000 + 40,000 ≤ 5,00,000 → system approval: SAP `A`,
  `AUTO_APPROVE` (`actor_type SYSTEM`, `on_behalf_of A`), ACOMMENT "Auto-approved after
  48 hours"; A notified.
* Had committed been ₹4,70,000: `budget_flow.status` stays `PENDING`; one `AUTO_APPROVE_SKIPPED` (reason
  "limit") logged — not repeated each tick.

### E-10 Journal voucher identity and JSAP-compatible write-back

§17.4: source key `JV:BEVERAGES:1843:1:27`; SAP key `1843 / 28 / 27 / 27`.

---

## Decision classification

| Item | LOCKED DECISION | DESIGN DECISION | SAP DEPENDENCY | FUTURE ENHANCEMENT |
|---|---|---|---|---|
| Engine unchanged, one user per stage, two modules | ✔ L-1, L-2 | | | |
| Exactly eight Budget tables | ✔ L-20 | ✔ column design (§33) | | |
| Entries from SAP drafts, 5 min, `ProcesStat = 'Y'` | ✔ L-3, L-4 | | ✔ source tables | |
| Upsert + change detection inside `documents.lines` | ✔ L-5 | ✔ handling per state | | |
| Read SAP tables instead of `CALL DRAFT_APPROVAL` | | ✔ §7.2 | ✔ S-3 mirror of the procedure | |
| Candidate slices + engine selection + merge | | ✔ §10.2 | | |
| Routing rule = engine query over `budget.documents` | | ✔ §11 | | query builder UI |
| Exactly one workflow; 0 / >1 → exception rows | ✔ L-6 | | | |
| Vertical routing dimension | ✔ requirement | ✔ settings-driven column | ✔ field to identify (N-1) | |
| Document identity + revision | ✔ L-8 | ✔ revision | | |
| JV identity `Company + BatchNum + TransId + Line_ID` | ✔ L-9 | ✔ source-key string | | |
| JV write-back = JSAP key; collision inherited | ✔ L-10 | | ✔ K-1 | SAP adds `TransId` (optional) |
| A / V / R + verification | ✔ L-10 | | ✔ S-1 | |
| Synchronous write with rollback; failure logged after rollback | | ✔ §16.4, §16.6 | | outbox (N-3) |
| No pull-back / recall | ✔ L-11 | | | explicit recall (N-5) |
| Rejection terminal, remarks required | ✔ L-12 | | | |
| Allocation increase, threshold 3,00,000, ≤ no workflow | ✔ L-13 | ✔ threshold in settings | | |
| Auto-approval 48 h, system actor | ✔ L-14 | ✔ per-bucket, exemptions, warning, skip-on-change logging | | |
| Notifications incl. reject/allocation | ✔ L-15 | ✔ event list | | |
| Permission keys | ✔ L-16 | ✔ key set | | |
| Reporting definitions | ✔ L-17 | ✔ formulas, JSON aggregation | | materialised view if needed |
| No migration | ✔ L-18 | | | |
| Coexistence by unit and cut-over `T` | ✔ L-19 | ✔ creation-date ownership | | JSAP template deactivation (JSAP admin) |
| Attachments on demand, no table | | ✔ §8.5 | ✔ `DRAFT_APPROVAL_ATC1` (Oil confirmed; Beverage S-4) | |
| Budget document write lock (single serialized write path) | | ✔ §8.2 | | |
| `allocation_requests.status` = lifecycle only | | ✔ §19.2 | | |
| One action log with six subject types | | ✔ §28, §33.9 | | |
| Mart document approval | | | ✔ N-2 | ✔ |
| Mobile approver screens | | | | ✔ phase 9 |
