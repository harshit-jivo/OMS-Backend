# OMS Generic Workflow Engine — Implementation Plan

**Status:** Plan only. No code, models, or migrations created.
**Revision:** v2 — incorporates the FINAL decisions of 2026-09-12.
**Baseline:** `production` (merged into `JSAP`). `main` is stale and is **not** the baseline.
**Date:** 2026-09-12

---

## 0. Provenance and caveats — read first

**0.1 — The referenced audit document does not exist.**

`docs/audit/JSAP_WORKFLOW_RUNTIME_AUDIT.md` is not present in the working tree, on any of the 13 remote branches of OMS-Backend, or in any commit reachable from any ref (verified with `git log --all --diff-filter=A`). Neither is `docs/architecture/OMS_WORKFLOW_ENGINE.md`. Those paths have never existed on any branch.

Every JSAP claim in §2 was therefore re-derived **directly from JSAP source**, cited to file and line. Nothing in §2 is inherited from the missing audit.

**0.2 — Baseline is `production`; this is settled.**

`production` has been merged into `JSAP` in both OMS-Backend (+358 commits, now `66cdb7e`) and OMS-Frontend (+143 commits, now `6bbe23d`). Both were fast-forwards — the `JSAP` branches had no commits of their own, so `JSAP` is identical to `production`. The apps this plan depends on (`approvals/`, `core/`, `notifications/`, `audit/`, `payments/`, `attachments/`, `sap_sync/`) are all present in the working tree and were verified there.

The stale `main` branch is not the baseline and is not referenced anywhere in this plan except to record that it misled an earlier revision.

**0.3 — Correction inherited from the `main` confusion: the backend is Django 5.2.10.**

`requirements.txt` on the baseline pins `Django==5.2.10`, not 6.0.3. The earlier "the repository is running Django 6.0.3" reading came from `main`. The original architecture assumption of Django 5.2 was therefore correct. This plan targets **Django 5.2.10 / DRF 3.16.1 / PostgreSQL**.

**0.4 — `jsQuery` could not be verified.**

The table `jsQuery` appears nowhere in the JSAP source. The only trace of the query-configuration mechanism is the type name `QueryTableType` in one procedure signature. The column list `(id, query, name, type, company)` is therefore **unverified** — carried on trust from the brief, not confirmed from code. See §19, OD-1.

---

## 1. Executive Summary

We are building a reusable, configuration-driven approval workflow engine as a new Django app (`workflow/`) in OMS-Backend. It separates three concerns that JSAP currently fuses inside per-module stored procedures:

| Concern | Owner |
|---|---|
| What the document *is* | the module's own business table |
| Where the document is in approval | a module-owned `<module>_flow` table |
| What rules govern approval | generic `workflow/` configuration tables |

The engine keeps JSAP's **SQL-query-driven workflow selection** — a deliberate constraint, not a default — but changes *how* those queries execute: from an unparameterised batch sweep over whole business tables into a parameter-bound single-document predicate test (§5, §6). That transformation is the technical core of this plan.

**The approval model is deliberately simple: one stage, one user, one task.** A stage's assigned user approves and the stage completes, or rejects and the workflow is rejected. There is no quorum, no approver collection, and no N-of-M logic anywhere in this design.

Workflow selection is **fail-loud**: zero matches raises `WorkflowNotConfigured`, one match proceeds, more than one raises `AmbiguousWorkflowSelection` and rolls back. There is no priority column and no implicit ordering of any kind.

**Resubmission is a module concept, not an engine concept.** A module owns whether and how often a rejected entry may be resubmitted, records that in its own `flow_logs`, and invokes the engine again on the **same** module-flow row. The engine never creates a second flow row for a document and holds no resubmission policy, state, or counter (§3.1, §4.9.1).

All four conflicts raised in the previous revision are now closed by the final decisions (§19.1). Five genuine open items remain, none of which block the schema.

---

## 2. Existing JSAP Behavior — verified from source only

### 2.1 Configuration tables

| Table | Columns observed | Evidence |
|---|---|---|
| `jsTemplate` | `id`, `company` (INT: `1`→OIL, else BEVERAGE) | `bud.jsExecuteBudgetQueries_Original.sql:71-80` |
| `jsStage` | `id`, `stage` (name), `approvalId` | `BPmasterService.cs:2664-2674` |
| `jsStageTemplate` | `stageId`, `templateId`, `priority` | `BPmasterService.cs:2677-2683` |
| `jsTemplateApproval` | `templateId`, `approvalId` | `BPmasterService.cs:2685-2687` |
| `jsUserStage` | `userId`, `stageId`, `status`, `startTime`, `endDate` | `bud.jsGetBudgetInsightAll_Original.sql:44-58` |
| `jsQuery` | **not found in source** | see §0.4 |

**Finding 2.1a — `jsStageTemplate.priority` is stage ordering, not workflow priority.**

```sql
SELECT @currentStage = stageId FROM jsStageTemplate
WHERE templateId = @templateId AND priority = 1;
```
`bud.jsExecuteBudgetQueries_Original.sql:96-98`

`priority = 1` selects the *first stage* of a template. It is sequence, not selection precedence. This distinction matters because the ban on `priority` concerns **workflow selection only**. This plan uses `sequence` for stage ordering and introduces no selection priority anywhere. The two concepts merely share a name in JSAP.

**Finding 2.1b — in JSAP, stages are global and shared across templates.**

`jsUserStage` keys on `stageId` alone, never `(templateId, stageId)`:

```sql
SELECT COUNT(1) FROM dbo.jsUserStage
WHERE userId = @userId AND stageId = @stageId AND ISNULL(status, 1) = 1;
```
`BPmasterService.cs:2777`

Editing a JSAP stage's assignment therefore affects every template using it. **This plan does not reproduce that.** Stages here belong to exactly one workflow (§4.4), which makes stage edits local and predictable. Recorded as a deliberate departure (§2.3).

**Finding 2.1c — JSAP time-bounds the assignment row itself.**

```sql
WHERE t.company = @company AND ta.approvalId = 5
  AND ( (jus.startTime IS NULL AND jus.endDate IS NULL)
        OR (jus.status = 1 AND GETDATE() BETWEEN jus.startTime AND jus.endDate) )
```
`bud.jsGetBudgetInsightAll_Original.sql:51-58`

An assignment is permanent (both bounds NULL) or active only inside a window. Note also the hardcoded `ta.approvalId = 5`, an unexplained magic constant. **This plan replaces per-assignment windows with a single date-based replacement mapping** (§4.5, §8) — one mechanism instead of two.

**Finding 2.1d — JSAP carries approve and reject quorum counts.**

```csharp
public int approvalRequired { get; set; }
public int rejectionRequired { get; set; }
```
`Models/AuthModels.cs:722-723`

Present across `AdvanceRequestModels`, `BPmasterModels`, `BomModels`, `CreditLimitModels`, `ItemMasterModel`, `PrdoModels`, `QcModels`.

**This is recorded as historical fact only. It is NOT carried into the OMS design.** Per the final decisions, each stage has exactly one user, so both counts are structurally always 1 and the fields do not exist in this plan. See §2.3.

### 2.2 Runtime tables — two different shapes, not one

| Module | Runtime tables | Refs |
|---|---|---|
| Budget | `bud.jsDocEntry` + `bud.jsDocEnrtyDetail` *(sic)* | 13 + 12 |
| BP Master | `BP.jsFlow` + `BP.jsFlowStatus` | 5 + 2 |
| IMC | `imc.jsFlow` | 1 |

```sql
INSERT INTO bud.jsDocEntry (
    docEntry, status, currentStageId, templateId, totalStage, currentSatge, date
) VALUES (@docEntry, 'P', @currentStage, @templateId, @totalStage, 1, @CURRENTMONTH);
```
`bud.jsExecuteBudgetQueries_Original.sql:110-118`

Note `currentSatge` — a live misspelling. Note `totalStage` denormalised into the runtime row, and `date` holding `@CURRENTMONTH` rather than a timestamp.

**PRDO and Credit Limit have no observable flow table.** Their workflow lives entirely in stored procedures absent from this repository — `[PRDO].[jsApproveProductionOrder]`, `[cl].[jsApproveDocument]`, `[cl].[jsGetFlowStatus]`. The C# is a thin pass-through. The claimed `PRDO.jsDocEntry` is **not verifiable here** (zero references). What *is* verifiable: `flowId` is the universal runtime handle across PRDO, Credit Limit and BP Master — a useful signal that a generic flow abstraction fits.

### 2.3 Deliberate departures from JSAP — recorded, not open

| JSAP behavior | OMS decision | Consequence accepted |
|---|---|---|
| Many users per stage (`jsUserStage`) | **exactly one user per stage** | simpler runtime; staffing changes are stage edits |
| Approve/reject quorum counts (2.1d) | **no quorum** | one approve completes a stage; one reject ends the workflow |
| Global stages shared across templates (2.1b) | stage belongs to one workflow | stage edits are local, not cross-template |
| Time-bounded assignment rows (2.1c) | date-based replacement mapping (§8) | cannot express "user X approves only in March" |
| Multiple flows per document (2.3a below) | fail loud on ambiguity | a config collision blocks submission instead of mis-routing |
| Batch sweep selection | synchronous per-document evaluation | documents match at submit or not at all |

These are closed decisions. They are listed so the divergence is on the record, not to reopen them.

### 2.4 How workflow selection actually works — the central finding

`bud.jsExecuteBudgetQueries` takes a table-valued parameter of `(tempId, queryId, query)` rows and, for each:

```sql
SET @query = 'INSERT INTO bud.TempResults (...38 columns..., executionId)
    SELECT ...38 columns..., ''' + CAST(@executionId AS NVARCHAR(36)) + '''
    FROM (' + @query + ') AS QueryResults';
EXEC sp_executesql @query;
```
`bud.jsExecuteBudgetQueries_Original.sql:48-69`

Then it cursors over the result set, creating a flow row per document found:

```sql
DECLARE rec_cursor CURSOR FOR
SELECT DISTINCT DocEntry, Branch, ObjType, CURRENTMONTH
FROM bud.TempResults WHERE flag = 'A' AND ProcesStat = 'Y' AND executionId = @executionId;
```
`bud.jsExecuteBudgetQueries_Original.sql:85-88`

**What this means:**

1. The stored query is a **set selector**, not a per-document condition. It answers "which documents belong in this template's workflow?" The canonical example — `WHERE BRANCH='OIL' AND BUDGET IN ('Factory') AND ProcesStat='Y' AND ObjType != 28` — has no document parameter at all.
2. Selection is a **batch sweep**, not a call at document creation.
3. It is **decoupled from document creation entirely**. No C# caller exists; it is almost certainly a SQL Agent job outside this repository (OD-2).
4. Query text is **concatenated into dynamic SQL** and run via `sp_executesql` with no parameterisation, no validation, no statement whitelist.
5. There is **no transaction**. Three nested cursors, writes to SQL Server and to HANA over linked server `HANA112`, no `BEGIN TRAN`. HANA inserts are string-built per row and failures are swallowed into `bud.jsSyncErrors` via `TRY/CATCH`, leaving the two databases divergent.

**Finding 2.4a — JSAP creates one flow per matching template, not one per document.**

```sql
IF NOT EXISTS (
    SELECT 1 FROM bud.jsDocEntry
    WHERE docEntry = @docEntry AND templateId = @templateId )
```
`bud.jsExecuteBudgetQueries_Original.sql:100-104`

Idempotency is scoped `(docEntry, templateId)`. N matching templates produce N independent flows. JSAP has no concept of selecting exactly one workflow. The OMS answer is fail-loud ambiguity (§6.1) — decided.

### 2.5 The `jsQuery` configuration — exact columns

A second inspection pass located the JSAP query-configuration surface, which the earlier revision could not find. The table is never named in a `FROM` clause in the available source (all access is through stored procedures whose bodies are not in the repository), but its **column set is fully recoverable** from the C# data contracts and the stored-procedure parameter lists that read and write it.

**Write path** — `UserService.GetAddQueryAsync` → `EXEC jsAddQuery`:

```csharp
public class AddQueryModel {
    public string query     { get; set; }   // Models/AuthModels.cs:365
    public string queryName { get; set; }   // :366
    public int    type      { get; set; }   // :367
    public int    company   { get; set; }   // :368
}
```
`Services/Implementation/UserService.cs:1441-1448` passes exactly `@query, @queryName, @type, @company` to `jsAddQuery`.

**Read paths** — `jsGetQueryName` (list) and `jsGetTemplate` (template detail):

```csharp
public class QueryNameModel {          // Models/AuthModels.cs:288-292
    public int    id   { get; set; }
    public string name { get; set; }
}

// OneTemplateDetailModel — the flattened template join, :414-416
public int    queryId   { get; set; }
public string queryName { get; set; }
public string query     { get; set; }
```

**Verified columns:**

| Column | Type | Verified from | Purpose |
|---|---|---|---|
| `id` | `int`, PK identity | `QueryNameModel.id`; `OneTemplateDetailModel.queryId` | surrogate key; the `queryId` carried in the TVP |
| `query` | string — `NVARCHAR(MAX)` by behavior | `AddQueryModel.query`; concatenated into `sp_executesql` (§2.4) | the stored SELECT that selects the candidate document set |
| `name` / `queryName` | string | read returns `name`; write sends `@queryName` | admin-facing label |
| `type` | **ambiguous — see below** | write `int`; read filter `string` | classifier; see below |
| `company` | `int` | `AddQueryModel.company` | company scope |

**`company` is an integer company id, not a string.** It shares the domain of `jsTemplate.company`, which §2.1 verified as `1 → OIL`, otherwise BEVERAGE (`bud.jsExecuteBudgetQueries_Original.sql:71-80`).

**Two things remain genuinely unverifiable and are recorded as such rather than invented:**

1. **The column's own name is either `name` or `queryName`.** The write parameter is `@queryName` (`UserService.cs:1443`) while the read projects `name` (`QueryNameModel`). Since `jsAddQuery` and `jsGetQueryName` are both stored procedures absent from the repository, either could be renaming. Both OMS field names are acceptable; the plan uses `name` (§4.3).

2. **`type`'s SQL type and meaning.** `AddQueryModel.type` is `int`, but the read filter is declared `GetQueryNameAsync(string type, int company)` (`UserService.cs:962`) and passed straight to `EXEC jsGetQueryName @type, @company`. So JSAP writes an integer and filters with a string against the same column — an inconsistency inside JSAP itself, resolvable only by reading the live table or the two procedure bodies. **Its business meaning is not recoverable from the available source at all**: no source file maps a `type` value to anything, and no comparison against a literal `type` value exists anywhere. It most plausibly distinguishes query categories (module or purpose), since `jsGetQueryName(type, company)` is a filtered picker, but that is inference and is not asserted here.

   The plan therefore carries `type` for parity and lets nothing depend on it (§4.3). **Resolving it requires a look at the live `jsQuery` rows or the `jsAddQuery` / `jsGetQueryName` procedure bodies** — a data question, not a design question, and it does not block implementation.

   Also unverifiable, and therefore not added: any `createdBy`, `createdOn`, or `status` column. `AddQueryModel` carries none, so if they exist they are defaulted inside `jsAddQuery`.

**Finding 2.5a — a template has MANY queries, confirmed from the write path.**

```csharp
public class AddTemplateModel {          // Models/AuthModels.cs:242-258
    public string template     { get; set; }
    public int    createdBy    { get; set; }
    public string stageIds     { get; set; }   // CSV
    public string priority     { get; set; }   // CSV, parallel to stageIds
    public string approvalIds  { get; set; }   // CSV
    public int    company      { get; set; }
    public string queries      { get; set; }   // CSV of jsQuery.id
}
```

`jsAddTemplate` receives `@queries` as a **comma-separated list of query ids** (`UserService.cs:874-881`), so the template↔query relationship is many-to-many and lives in a link table, not on `jsQuery`. `jsQuery` has no `templateId` column — which is why the TVP must carry `(tempId, queryId, query)` together (§2.4): the *caller* supplies the pairing.

`UserService.cs:1590-1596` then regroups the flattened `jsGetTemplate` result into `stages[]`, `queries[]`, and `approvals[]` per template, confirming the cardinality at the read end too.

**This validates the OMS design.** `workflows → 1:N workflow_queries` (§4.3) preserves the real JSAP cardinality. The one simplification is that OMS scopes queries to one workflow (FK) rather than sharing them many-to-many across workflows; a query needed by two workflows is duplicated. That is deliberate — shared mutable query rows are how a single edit silently changes several workflows at once.

**Finding 2.5b — JSAP's active flag lives on the template.**

```csharp
public int isActive { get; set; }    // TemplateListModel:398, OneTemplateDetailModel:406
```

`jsTemplate` carries `isActive` (plus `name`, `description`, `createdOn`, `createdBy`, `company`). This is the "unique/active handling that already exists outside this simplified structure." It is recorded here as fact; per the final decisions OMS adds **no** `is_active` to `workflows`, and lifecycle is governed by the presence of query rows (§4.2). Not reopened.

**Finding 2.5c — JSAP already has a query validation procedure.**

```csharp
using (var command = new SqlCommand("EXEC jsValidateQuery @Query", connection))
```
`UserService.cs:1458` (`GetValidateQueryAsync`) and `UserService.cs:1428` (`ExecuteUserQueryAsync`) — two methods with **identical bodies**, both calling `jsValidateQuery` and returning its message. Note that the method named "execute user query" does not execute anything; it validates.

So a validation step is an established part of the JSAP admin workflow, and the OMS validator (§14 Layer 2) replaces an existing concept rather than introducing an unfamiliar one. What `jsValidateQuery` actually checks is unknown — its body is not in the repository — so the OMS validator is specified from requirements, not ported.

---

## 3. Proposed OMS Architecture

```
Module API (POST /api/budget/)
        │
        ▼
  business INSERT  ──────────────┐
        │                        │  one transaction
        ▼                        │
  WorkflowService.start(         │
      module='BUDGET',           │
      document=<obj>,            │
      context={...})             │
        │                        │
        ├── load module config + its workflows' queries
        ├── evaluate each query as a bound single-document predicate  ← §5
        ├── 0 / 1 / >1 handling — fail loud                           ← §6
        ├── create <module>_flow row
        ├── open stage sequence=1
        ├── create ONE workflow_task for the stage's user             ← §7
        └── append SUBMIT to workflow_action
        │                        │
        ▼                        │
     COMMIT ◄───────────────────┘
        │
        ▼  (post-commit only)
  notifications.services.notify(...)
```

Final approval enqueues durable integration work rather than calling SAP inline (§13).

**Configuration shape — final:**

```
workflow_modules
      │
      └── workflows
             ├── workflow_queries        (1:N — many SQL queries per workflow)
             └── workflow_stages         (1:N — each with exactly ONE user_id)

workflow_user_replacements               (standalone, date-based)
```

**Layering.** The generic engine never imports a business module. Modules depend on `workflow/`; `workflow/` depends only on `core/`, `users/`, and `notifications/`. Module flow models subclass an abstract base and supply the concrete FK, so PostgreSQL gets real referential integrity while the engine stays generic.

### 3.1 Responsibility boundary — module vs. engine

This boundary is the governing rule of the architecture. Anything about *what the document is or is allowed to do* belongs to the module; anything about *how approval proceeds* belongs to the engine.

```
┌──────────────────────────────────────────┐
│              BUSINESS MODULE             │
│                                          │
│ Entry creation                           │
│ Entry editing                            │
│ Resubmission rules                       │
│ flow_logs / business history             │
│ Module-specific validation               │
└──────────────────┬───────────────────────┘
                   │
                   │ Start / invoke workflow
                   ▼
┌──────────────────────────────────────────┐
│             WORKFLOW ENGINE              │
│                                          │
│ Workflow selection                       │
│ SQL condition evaluation                 │
│ Stage sequencing                         │
│ Stage user resolution                    │
│ Task creation                            │
│ Approve                                  │
│ Reject                                   │
│ Workflow state                           │
└──────────────────────────────────────────┘
```

**Vocabulary split.** The two layers have distinct verbs, and mixing them is the mistake this section exists to prevent:

| Layer | Owns these concepts |
|---|---|
| Module | `RESUBMIT`, entry edit, who may resubmit, how often, business validity |
| Engine | `START WORKFLOW`, `OPEN STAGE`, `APPROVE`, `REJECT`, `COMPLETE STAGE`, `COMPLETE WORKFLOW` |

**The Workflow Engine must NOT determine:**

- whether an entry is allowed to be resubmitted;
- whether the business entry should reuse its module-flow row;
- how many times a business entry can be resubmitted;
- any module-specific resubmission rule.

**`RESUBMIT` is not an engine concept.** The engine has no resubmission API, no resubmission state, no attempt counter, and no rule that a resubmission produces a new flow row. When a module decides an entry may be resubmitted, it records that in its own history and invokes the engine again; the engine then performs ordinary workflow processing against the current configuration (§4.9.1, §6.2).

**`flow_logs` is module-owned.** Business lifecycle events — `SUBMITTED`, `REJECTED`, `RESUBMITTED`, `APPROVED` and any module-specific event — are recorded by the module, in the module's own history mechanism (§4.9.2). The engine keeps its own append-only record of *engine* actions in `workflow_action` (§4.7); it does not duplicate the module's history, and it adds no generic log for resubmission.

---

## 4. Database Schema

Types are PostgreSQL. Conventions follow the baseline: explicit `db_table`, `TimeStampedModel` from `core/models.py`, `constraints = [...]` over `unique_together`.

**Schema placement — decided: the workflow engine gets its own PostgreSQL schema.**

This follows the precedent already set by `payments`, which lives in its own schema (`payments/migrations/0012_move_to_payments_schema.py` does `ALTER TABLE ... SET SCHEMA`, with `'options': '-c search_path=payments,public'` in settings).

```
PostgreSQL
│
├── public                          (existing application schema)
│      ├── users_user, users_role, …
│      ├── orders_*, invoice_*, audit_log, …
│      └── approval_workflow, approval_request, …   (existing approvals/ engine)
│
├── payments                        (existing, precedent)
│      └── payment_receipt, payment_bank_deposit, …
│
└── workflow                        (NEW — this engine)
       ├── workflow_modules
       ├── workflow_queries
       ├── workflow_stages
       ├── workflow_user_replacements
       ├── workflow_task
       ├── workflow_action
       ├── workflow_outbox              (if built — OD-4)
       └── <module>_flow tables         (see note below)
```

`workflows` is also in this schema; it is omitted from the tree above only to avoid confusion between the schema name `workflow` and the table name `workflows`.

**Schema name: `workflow`** — singular, matching the Django app label and the `payments` precedent of naming the schema after the app.

Notes for whoever writes these migrations:

- `search_path` becomes `workflow,payments,public` (or the engine's models declare `db_table = 'workflow"."<table>'`, which is the more explicit of the two options and avoids a growing global `search_path`). The choice between them is an implementation detail, not an architectural one — but it must be made once and applied uniformly.
- Cross-schema foreign keys are fully supported by PostgreSQL, so `workflow.workflow_stages.user_id → public.users_user.id` is fine and needs no special handling.
- Unlike `payments`, this schema is created empty rather than migrated into, so no `SET SCHEMA` data move is needed — the tables are born there. That makes this strictly simpler than the `payments` precedent.
- **Module flow tables belong to the `workflow` schema**, even though each is declared in its own module app: they are workflow-engine runtime state keyed to a document, not business data. Their FK to the business document crosses schemas, which is supported. An alternative — each module's flow table in that module's own schema — is also defensible; this plan places them in `workflow` so that all workflow runtime state is in one schema and can be backed up, inspected, or permission-scoped as a unit.
- The `workflow_ro` role (§5.2) gets `USAGE` on `workflow` plus `SELECT` on the allow-listed business relations in `public`. It needs no write grant anywhere.

### 4.0.1 Database prerequisites and migration notes

**`btree_gist` extension — required.**

The exclusion constraint on `workflow_user_replacements` (§4.5) combines an equality test on `old_user_id` with an overlap test on a `daterange`. GiST indexes do not support equality on a plain `bigint` without `btree_gist`, so the extension is a hard prerequisite for that constraint.

Confirmed available in the target PostgreSQL environment. The first `workflow` migration must therefore declare it:

```python
from django.contrib.postgres.operations import BtreeGistExtension

class Migration(migrations.Migration):
    operations = [
        BtreeGistExtension(),          # must precede the constraint
        # ... workflow_user_replacements, then its EXCLUDE constraint
    ]
```

Notes for whoever writes that migration:

- `BtreeGistExtension()` issues `CREATE EXTENSION IF NOT EXISTS btree_gist`, which needs superuser or `rds_superuser` **at migrate time only**, not at runtime. Confirm the migrating role has it in each environment.
- It must run before the constraint is added, and it belongs in the app's *first* migration so no ordering question arises later.
- It is a database-wide object: if another app already installed it, `IF NOT EXISTS` makes this a no-op.
- The test database needs it too, which is part of why a real PostgreSQL instance is required for the suite (§18, OD-7).

No other extension is required by this design.

### 4.0.2 Company scope — one configuration for ALL companies

**Requirement.** A workflow or query configuration must apply either to ONE
specific company or to ALL companies, and "ALL" must be **one row**, never a
copy per company. JSAP forces the duplication this removes.

**No precedence.** A company-specific configuration does NOT outrank an ALL
configuration. If both match, that is ambiguity and the engine fails loud
(§6.1). This is a deliberate departure from the existing `approvals` engine —
see the warning at the end of this section.

#### Which company domain — and why not a foreign key

Inspection of the baseline found **three** different things called "company",
and picking the wrong one would silently scope workflows by the wrong concept:

| Candidate | What it actually is | Suitable? |
|---|---|---|
| `users.Company` (int PK, free-text `name`) | **organisational** master for user records; `User.company` FKs it. Never used to scope a document. | **No** — different concept |
| `orders.Categories` (int PK, `category` varchar(255), table `categories`) | relational master behind `User.category`; no unique constraint, no `is_active` | **No** — weak master, and documents don't reference it |
| `'OIL' / 'BEVERAGES' / 'MART'` string enum | the **document** company across `approvals`, `payments`, `orders`, `sap_sync`; matches the SAP company databases (`HANA_COMPANY_DB*`) | **Yes** |

The engine must compare against whatever the **document** carries, and every
document-bearing app in OMS carries the string. `ApprovalRequest.company`,
`PaymentReceipt.company` and `Scheme.category` are all
`CharField(choices=CATEGORY_CHOICES)` over exactly those three values.

So there is **no authoritative integer company master for document scoping** to
put a foreign key on. Adding one would either duplicate the enum as a new
master (forbidden) or bend `users.Company` / `orders.Categories` into a meaning
they do not have — which is precisely the "corrupted FK domain" to avoid. The
domain is therefore a **CHECK-constrained closed string set**, which gives the
same guarantee an FK would (only valid values storable) without inventing a
master.

The enum is currently copy-pasted in five files (`approvals`, `payments`,
`orders`, `sap_sync`, `users`). This plan adds **one canonical definition in
`core`** for the new app to import, and leaves the five existing copies
untouched — the same approach `core.models.TimeStampedModel` and
`core.responses` already take ("fixes that for the new apps without touching
existing tables"). `workflow` must not import `approvals`, so importing the
constant from there is not an option.

#### The representation — two columns, mutually exclusive by CHECK

Applied identically to `workflows` and `workflow_queries`:

| Column | Type | Null | Notes |
|---|---|---|---|
| `company_scope` | `varchar(8)` | NO | `'ALL'` or `'SPECIFIC'` |
| `company` | `varchar(20)` | YES | NULL when `ALL`; a valid company code when `SPECIFIC` |

Constraints:

```sql
CHECK (company_scope IN ('ALL','SPECIFIC'))
CHECK (company IS NULL OR company IN ('OIL','BEVERAGES','MART'))
CHECK (
    (company_scope = 'ALL'      AND company IS NULL)
 OR (company_scope = 'SPECIFIC' AND company IS NOT NULL)
)
```

That third CHECK is the one the requirement asks for directly: **a row cannot
simultaneously mean "specific company" and "ALL".** The two meanings are
structurally exclusive, so there is no ambiguous representation to
misinterpret and no "did someone forget to fill this in?" state.

**Why not the `company = ''` sentinel** that `approvals`, `orders.Scheme` and
`payments` already use (`Q(company=company) | Q(company='')`)? It is the
established convention, and it was the obvious candidate, but it fails the
requirement's own test: `''` cannot be distinguished from "not filled in", so
a row that means ALL is indistinguishable from a row someone left blank by
accident. An explicit `company_scope` makes the intent a stated value rather
than an inferred one, and the CHECK then makes the invalid combination
impossible. The cost is one extra column on two tables.

**Why not a join table** (`workflow_query_companies`, empty set = ALL)? It
would make "ALL" an *implicit* sentinel again (absence of rows), add a join to
the selection hot path, and buy a capability nobody asked for — an arbitrary
subset of companies. The requirement is "one company OR all", not "a list".

#### Where scope lives: BOTH workflow and query

The brief asked this to be decided explicitly rather than assumed. Decision:
**on both, with the query narrowing the workflow.**

* **`workflows.company_scope` is the applicability rule** — "does this
  workflow apply to this document's company at all?" This is the normalized
  home: one workflow, one stated applicability, readable without inspecting
  its children.
* **`workflow_queries.company_scope` narrows within an applicable workflow** —
  it selects *which* of the workflow's conditions are evaluated for that
  company. This preserves real JSAP behaviour: `jsQuery.company` exists
  (§2.5) and a template applying to two companies can legitimately need a
  different condition per company.

Scope on the workflow alone would force one workflow per company whenever the
*condition* differs by company — reintroducing the duplication this feature
exists to remove. Scope on the query alone would make a workflow's
applicability implicit in its children, which is exactly the indirection that
makes JSAP configuration hard to reason about.

**Contradiction is prevented at validation time.** A query scoped to a
specific company inside a workflow scoped to a *different* specific company is
a dead configuration that can never fire. The rule:

```
workflow ALL              -> any query scope allowed
workflow SPECIFIC(X)      -> query must be ALL or SPECIFIC(X)
```

This is a cross-row rule, so it is enforced in the serializer/admin and
covered by tests rather than by a CHECK (a CHECK cannot read the parent row).

### 4.1 `workflow_modules`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `code` | `varchar(30)` | NO | UNIQUE. `BUDGET`, `PRDO`, … |
| `name` | `varchar(100)` | NO | |
| `business_table` | `varchar(120)` | NO | qualified relation, e.g. `budget.budget_draft` |
| `business_key_column` | `varchar(60)` | NO | column a query exposes for document identity |
| `flow_table` | `varchar(120)` | NO | e.g. `workflow.budget_flow` |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints: `UNIQUE(code)`; `CHECK (code = upper(code))`.

**Why the three extra columns.** They answer "how does the engine know which parameter to bind?" declaratively rather than with a per-module branch in Python: `business_key_column` names the bind target, and `business_table` / `flow_table` feed the identifier allow-list in §14, which must state per module exactly which relations a query may touch.

No `priority`. No `is_active`.

### 4.2 `workflows`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `module_id` | `bigint` | NO | FK → `workflow_modules`, `ON DELETE RESTRICT` |
| `code` | `varchar(60)` | NO | |
| `name` | `varchar(120)` | NO | |
| `company_scope` | `varchar(8)` | NO | `'ALL'` or `'SPECIFIC'` — §4.0.2 |
| `company` | `varchar(20)` | YES | NULL iff scope is `ALL` — §4.0.2 |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints: `UNIQUE(module_id, code)`, plus the three company-scope CHECKs
from §4.0.2. Indexes on `module_id` and `(module_id, company_scope, company)` —
the latter serves the selection filter in §6 directly.

`company_scope` is **applicability, not precedence**. A `SPECIFIC` workflow
does not outrank an `ALL` workflow; if both apply and both match, selection
fails loud (§6.1).

**Explicitly absent:** `priority`, `selection_priority`, `is_active`, and any `query`/`query_text` column. Queries live only in `workflow_queries` (§4.3).

**On lifecycle.** No active-state mechanism is invented here. A workflow is reachable only if it has candidate query rows; removing its queries makes it unreachable without touching in-flight flows, which hold a workflow FK and keep running. That is a consequence of the existing structure, not a new status field.

### 4.3 `workflow_queries`

One workflow → many queries. This table is never collapsed into `workflows`.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `workflow_id` | `bigint` | NO | FK → `workflows`, `ON DELETE CASCADE` |
| `name` | `varchar(120)` | NO | JSAP parity |
| `query_text` | `text` | NO | the stored SELECT |
| `type` | `varchar(30)` | NO | JSAP parity — semantics unverified, see §2.5 |
| `company_scope` | `varchar(8)` | NO | `'ALL'` or `'SPECIFIC'` — §4.0.2 |
| `company` | `varchar(20)` | YES | NULL iff scope is `ALL` — §4.0.2 |
| `key_column` | `varchar(60)` | YES | overrides module default when a query aliases its key |
| `validated_at` | `timestamptz` | YES | set by the validator (§14); NULL ⇒ never executed |
| `validation_error` | `text` | NO | default `''` |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints: `UNIQUE(workflow_id, name)`, plus the three company-scope CHECKs
from §4.0.2. Indexes `(workflow_id)`, `(workflow_id, company_scope, company)`.
`CHECK (query_text ~* '^\s*(select|with)\b')` — a cheap structural floor; the real gate is §14.

A query's scope may only **narrow** its workflow's (§4.0.2): under a workflow
scoped `SPECIFIC(X)`, a query must be `ALL` or `SPECIFIC(X)`. Enforced in the
serializer/admin, since a CHECK cannot read the parent row.

**Match semantics.** A workflow matches if **any** of its queries matches (OR). This mirrors JSAP, where each query row independently feeds documents into its template. Crucially, query order is never used to break ties between *workflows* — see §6.1.

### 4.4 `workflow_stages` — exactly one user per stage

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `workflow_id` | `bigint` | NO | FK → `workflows`, `ON DELETE CASCADE` |
| `name` | `varchar(120)` | NO | |
| `sequence` | `smallint` | NO | 1-based **stage execution order only** |
| `user_id` | `bigint` | NO | FK → `users_user`, `ON DELETE RESTRICT` — the single assigned user |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints:
- `UNIQUE(workflow_id, sequence)`
- `CHECK (sequence >= 1)`

Indexes: `(workflow_id, sequence)`, `(user_id)`.

**`sequence` is stage execution order. It is NOT workflow-selection priority** and is never consulted during selection (§6).

**Explicitly absent:** `approval_required`, `rejection_required`, and any approver child table. `user_id` is `NOT NULL`, so a stage always has exactly one configured actor — "no approver configured" is structurally impossible.

`ON DELETE RESTRICT` prevents deleting a user who is a configured stage actor. Deactivation is still possible, which is handled at runtime (§7.3).

**There is no `workflow_stage_approvers` table in this design.**

### 4.5 `workflow_user_replacements`

Exactly the requested fields:

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `old_user_id` | `bigint` | NO | FK → `users_user`, `ON DELETE RESTRICT` |
| `new_user_id` | `bigint` | NO | FK → `users_user`, `ON DELETE RESTRICT` |
| `reason` | `varchar(255)` | NO | default `''` |
| `start_date` | `date` | NO | inclusive |
| `end_date` | `date` | NO | inclusive |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints:
- `CHECK (end_date >= start_date)`
- `CHECK (old_user_id <> new_user_id)`
- `EXCLUDE USING gist (old_user_id WITH =, daterange(start_date, end_date, '[]') WITH &&)` — bars overlapping replacements for the same user.

Index: `(old_user_id, start_date, end_date)`.

**Why the exclusion constraint.** It is a constraint, not a column, and it adds no state. Without it, two overlapping rows for one user would make resolution depend on row order — which would smuggle in exactly the kind of implicit ordering this design forbids. With it, at most one row can ever match a `(user, date)` pair, so resolution is deterministic by construction. It requires the `btree_gist` extension, which is **confirmed available** in the target environment and declared in the first migration (§4.0).

**What the constraint permits and rejects**, for the same `old_user_id`:

```
ALLOWED — adjacent, non-overlapping windows
    15 Sep → 22 Sep   →  User 8
    23 Sep → 30 Sep   →  User 9

REJECTED — overlapping windows
    15 Sep → 22 Sep   →  User 8
    20 Sep → 25 Sep   →  User 9      ❌ 20–22 Sep overlaps
```

Bounds are inclusive on both ends (`'[]'`), so `22 Sep → 23 Sep` is adjacent, not overlapping. Two replacements for *different* `old_user_id` values never conflict, whatever their dates.

The rejection is raised by PostgreSQL, not by application code, so it holds under concurrent inserts. Django surfaces it as an `IntegrityError`, which the admin/serializer layer should translate into a readable "this user already has a replacement covering those dates" message.

This table never touches `users_user`. A replacement changes runtime actor resolution only (§8).

### 4.6 `workflow_task` — one task per stage

The inbox spans modules, so tasks cannot live in per-module tables.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `module_id` | `bigint` | NO | FK → `workflow_modules`, `ON DELETE RESTRICT` |
| `flow_id` | `bigint` | NO | **no FK** — target table varies by module |
| `stage_id` | `bigint` | NO | FK → `workflow_stages`, `ON DELETE RESTRICT` |
| `sequence` | `smallint` | NO | stage sequence, denormalised for inbox queries |
| `stage_user_id` | `bigint` | NO | the stage's **configured** user, copied at open |
| `status` | `varchar(12)` | NO | `PENDING/APPROVED/REJECTED/CANCELLED` |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints:
- `UNIQUE(module_id, flow_id, stage_id) WHERE status = 'PENDING'` — **structurally guarantees at most one *open* task per stage**, and is the idempotency key that makes task creation safe under retry. It is partial on pending so that a flow row re-executed after a rejection (§4.9.1) can re-open a stage without colliding with that stage's terminal task rows, which are kept as history.
- Partial index `(stage_user_id, status) WHERE status = 'PENDING'` — the inbox hot path.
- Index `(module_id, flow_id)`.

**Why `stage_user_id` holds the *configured* user, not the effective one.** The replacement is applied at resolution time, not baked into the row (§8). Storing the configured user is what lets the original user "automatically resume" on an already-open task the moment the replacement window closes — if the effective user were written into the row, resumption would require rewriting live tasks.

`(module_id, flow_id)` is a deliberate soft reference: there is no single flow table to point at, so integrity here is the engine's responsibility, enforced in service code and tested (§18). This is the one place the design knowingly trades a database guarantee for genericity.

**No `round_number`, and no attempt counter of any kind.** Rounds existed only to support restart-after-rejection with quorum, and neither quorum nor engine-owned resubmission exists in this design. When a module re-invokes the engine on the same flow row (§4.9.1), `workflow_action.sequence` already orders every action across every execution, so nothing needs counting. The engine deliberately cannot answer "which attempt is this?" — that is a module question, answered from `flow_logs` (§4.9.2).

### 4.7 `workflow_action` — append-only history

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `module_id` | `bigint` | NO | FK → `workflow_modules` |
| `flow_id` | `bigint` | NO | soft reference, as above |
| `sequence` | `integer` | NO | monotonic per `(module_id, flow_id)` |
| `stage_id` | `bigint` | YES | FK → `workflow_stages`, `ON DELETE SET NULL` |
| `stage_name` | `varchar(120)` | NO | denormalised so history survives config edits |
| `action` | `varchar(12)` | NO | `SUBMIT/APPROVE/REJECT/CANCEL/EXPIRE` |
| `acted_by_user_id` | `bigint` | YES | FK → `users_user`, `ON DELETE SET NULL` — who actually clicked |
| `acted_by_username` | `varchar(150)` | NO | denormalised |
| `on_behalf_of_user_id` | `bigint` | YES | the configured stage user, when a replacement was in effect |
| `remarks` | `text` | NO | default `''` |
| `ip_address` | `inet` | YES | |
| `user_agent` | `varchar(400)` | NO | default `''` |
| `acted_at` | `timestamptz` | NO | `db_index` |

Constraints: `UNIQUE(module_id, flow_id, sequence)`. Index `(module_id, flow_id, acted_at)`.

No `updated_at`, no delete path — append-only. Denormalising `stage_name` and `acted_by_username` follows `approvals/models.py:208-251`, which already stores `level_name` and `approver_username` for the same reason: history must stay readable after configuration changes.

`acted_by_user_id` + `on_behalf_of_user_id` together make every replacement-driven action auditable: who acted, and for whom.

### 4.8 `workflow_outbox`

Deferred pending OD-4 — payments built an outbox and removed it (§13). Proposed shape, for review only:

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `module_id` | `bigint` | NO | FK → `workflow_modules` |
| `flow_id` | `bigint` | NO | soft reference |
| `operation` | `varchar(40)` | NO | e.g. `SAP_POST_BUDGET` |
| `payload` | `jsonb` | NO | |
| `status` | `varchar(12)` | NO | `PENDING/CLAIMED/SUCCESS/FAILED/DLQ` |
| `attempts` | `smallint` | NO | default 0 |
| `next_attempt_at` | `timestamptz` | NO | backoff schedule |
| `claimed_at` | `timestamptz` | YES | |
| `idempotency_key` | `varchar(120)` | NO | |
| `last_error` | `text` | NO | default `''` |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Constraints: `UNIQUE(idempotency_key)`; partial index `(next_attempt_at) WHERE status = 'PENDING'`.

### 4.9 Module flow tables — common fields

Abstract base `WorkflowFlowBase`; each module defines a concrete subclass supplying the document FK.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `bigserial` | NO | PK |
| `document_id` | `bigint` | NO | **concrete FK**, `ON DELETE PROTECT`, defined by the module |
| `workflow_id` | `bigint` | NO | FK → `workflows`, `ON DELETE RESTRICT` |
| `matched_query_id` | `bigint` | YES | FK → `workflow_queries`, `ON DELETE SET NULL` — why this workflow won |
| `status` | `varchar(12)` | NO | `PENDING/APPROVED/REJECTED/CANCELLED` |
| `current_stage_id` | `bigint` | YES | FK → `workflow_stages`, `ON DELETE RESTRICT` |
| `current_sequence` | `smallint` | NO | default 1 |
| `company` | `varchar(20)` | NO | |
| `context_snapshot` | `jsonb` | NO | default `'{}'` — routing inputs and selection explanation only |
| `integration_status` | `varchar(12)` | NO | default `PENDING`; convenience mirror of the outbox |
| `lock_version` | `integer` | NO | default 0 |
| `created_at` / `updated_at` | `timestamptz` | NO | |

Per-module constraints:
- `UNIQUE(document_id)` — one flow row per document, reused across executions. This is the constraint that prevents duplicate simultaneous workflow execution; see §4.9.1.
- `CHECK (current_sequence >= 1)`
- Partial index `(status, current_stage_id) WHERE status = 'PENDING'`

Deliberately **absent**: `total_stages` (derivable; JSAP denormalises it per §2.2 and that is drift worth not inheriting), `current_user_id` (the actor is the current stage's user, resolved through §8 — storing it would duplicate state and break automatic resumption), and `round_number`.

### 4.9.1 Flow uniqueness and workflow execution state

**One module-flow row per business document. It is reused, not replaced.**

The flow row represents *the workflow execution state of a document*, not one submission attempt. A document that is rejected and later resubmitted keeps the **same** flow row:

```
Business Entry (document_id = 456)
       │
       ▼
module_flow row #123            ← created once, reused forever
       │
       ├── first submission      (engine: START → OPEN STAGE → …)
       │
       ├── rejected              (engine: REJECT, flow status = REJECTED)
       │
       └── resubmitted           (module decision — see §4.9.2)
               │
               ▼
         SAME module_flow #123   ← engine re-invoked on the same row
```

**The engine never creates a second flow row for a document, and never creates one because a document was resubmitted.** Resubmission is not an engine concept (§3.1).

**Uniqueness constraint — defined around workflow execution state:**

```
UNIQUE(document_id)        -- plain unique, not partial
```

One document, one flow row, whatever its history. This is the constraint that guarantees a business entry cannot accidentally have two simultaneous workflow executions, and it is expressed on execution state rather than on any notion of attempts.

Combined with the status guard below, the guarantee is:

```
module_flow.status = PENDING   → a workflow execution is in progress
                                 → the engine refuses to start another
module_flow.status in (REJECTED, APPROVED, CANCELLED)
                               → no execution in progress
                                 → the module may invoke the engine again
```

**Engine-side guard.** `start()` loads the flow row (creating it only if the document has none) `FOR UPDATE`, then:

- if `status = 'PENDING'` → raise `WorkflowAlreadyRunning`, change nothing;
- otherwise → begin a new execution on that row.

The row lock plus `UNIQUE(document_id)` make this safe under concurrent calls: two simultaneous invocations serialise, and the second sees `PENDING` and is refused. No application-level check-then-act (§11).

**Beginning an execution on an existing row** re-resolves selection from scratch (§6.2) and sets `workflow_id`, `matched_query_id`, `status = 'PENDING'`, `current_stage_id` to stage 1, and `current_sequence = 1`. `created_at` is preserved; `updated_at` moves.

**History is not lost when the row is reused.** `workflow_action` rows are scoped to `(module_id, flow_id)` and are append-only with a monotonic `sequence`, so successive executions on flow #123 accumulate one continuous engine history:

```
workflow_action  (module_id, flow_id = 123)
  1  SUBMIT      stage 1
  2  APPROVE     stage 1
  3  REJECT      stage 2
  4  SUBMIT      stage 1        ← engine re-invoked after the module resubmitted
  5  APPROVE     stage 1
  6  APPROVE     stage 2
```

Nothing is deleted or overwritten. The engine's own actions remain fully auditable across any number of executions, with no attempt counter and no `round_number` — `sequence` already orders them.

Tasks are likewise retained. Because a re-executed workflow re-opens stages the document has already passed through, the task uniqueness key must be scoped to the *current* execution rather than to all history — so it is partial on pending (§4.6): at most one **pending** task per stage, with any number of terminal ones beneath it. That keeps "exactly one open task per stage" true at every instant while letting a document's task history accumulate across executions.

**What deliberately does not exist:** no new flow row per resubmission; no engine-level resubmission API, state, policy, or counter; no `round_number`; no attempt number; no generic resubmission log; and no reuse of a previous workflow selection (§6.2).

### 4.9.2 Module-owned history — `flow_logs`

Business lifecycle events belong to the module, recorded in the module's own history table:

```
module_flow
     │
     └── flow_logs
            ├── SUBMITTED
            ├── REJECTED
            ├── RESUBMITTED
            ├── APPROVED
            └── other module lifecycle events
```

Example for the same document as above:

```
flow_logs  (module_flow id = 123, document_id = 456)
  1  SUBMITTED
  2  STAGE_APPROVED
  3  REJECTED
  4  RESUBMITTED          ← a module event; the engine has no such action
  5  STAGE_APPROVED
  6  APPROVED
```

**Naming note, stated so it is not mistaken for an existing fact:** `flow_logs` does **not** currently exist anywhere in OMS-Backend. A search of the baseline for `flow_log` / `FlowLog` returns nothing; the nearest existing constructs are `orders.OrderFlowConfig` (configuration, not history) and JSAP's `BP.jsFlowStatus`. So `flow_logs` is the *planned module-side* term from this decision, not a table this plan can point at. Whoever builds the first module should either create it under that name or adopt whatever the module architecture settles on — and the exact event vocabulary follows the module, not this document.

**The engine adds no generic log for resubmission.** `workflow_action` records engine actions only (`SUBMIT`, `APPROVE`, `REJECT`, `CANCEL`, `EXPIRE`); it has no `RESUBMITTED` action and must not gain one. `RESUBMITTED` is a module event in `flow_logs`. The two histories are complementary: `flow_logs` answers "what happened to this entry as a business document", `workflow_action` answers "what the approval engine did".

**Division of responsibility on resubmission:**

```
Resubmission is a module-level business concept.

A module may allow a rejected entry to be resubmitted.
The module may reuse the same module_flow row.
The resubmission/history event is recorded in flow_logs.
The Workflow Engine is invoked again by the module when appropriate.
The Workflow Engine does not own the resubmission policy.
```

### 4.10 Where each field belongs

| Data | Home |
|---|---|
| Document content (amounts, lines, party) | business table — never copied |
| Approval position and status | `<module>_flow` |
| Rules, stages, stage users, queries | generic `workflow_*` config |
| Who owes an action right now | `workflow_task` (exactly one per stage) |
| What happened, immutably | `workflow_action` |
| External side-effect work | `workflow_outbox` |
| Routing inputs + why this workflow won | `<module>_flow.context_snapshot` |

---

## 5. SQL Query Condition Architecture

The requirement is to keep SQL-based conditions while avoiding whole-table scans. JSAP's actual model *is* a whole-table scan (§2.4). Reconciling these is the core technical move.

### 5.0 One common query execution mechanism for all modules

**Decided: a single generic query executor serves every module.** Every migrated module that previously depended on the `jsExecuteBudgetQueries` pattern uses this one component. No module implements its own SQL execution.

```
workflow_queries / migrated query configuration
                ↓
       Generic Query Executor            ← ONE implementation, shared
                ↓
      Validate configured SQL
                ↓
       Safe parameter binding
                ↓
       Read-only execution
                ↓
       Timeout / restrictions
                ↓
          Query Result
                ↓
   Module-specific interpretation        ← the ONLY per-module part
```

**The four concerns are separated, and only the fourth is ever written per module:**

| # | Concern | Where it lives | Per-module? |
|---|---|---|---|
| 1 | **Query configuration** | `workflow_queries` rows (§4.3) — text, company, key column | no — data, not code |
| 2 | **Query validation** | `workflow/validators.py` (§14 Layer 2) | **no** |
| 3 | **Query execution** | `workflow/services/conditions.py` — the executor (§5.1–5.3) | **no** |
| 4 | **Result interpretation** | the calling module | yes |

Concerns 1–3 have exactly one implementation each. Concern 4 is thin by construction: the executor answers a boolean existence question ("is this document in this query's set?"), so for workflow selection there is nothing left to interpret — §6 consumes the booleans directly. A module only writes interpretation code if it needs a query for something beyond selection, and even then it calls the same executor.

**Explicitly forbidden:** any SQL execution path inside `budget`, `prdo`, `credit_limit`, `backdate`, `bp_master`, `item_master`, `imc`, `bom`, `qc`, `advance`, or any future module. JSAP's failure mode was one `jsExecute*Queries` procedure per module, each re-implementing dynamic SQL with its own bugs and its own injection surface (§2.4). Centralising execution means validation, parameter binding, the read-only role, the timeout, and the audit log are written, reviewed, and tested **once**.

**Interface sketch** (shape only — not an implementation):

```
conditions.matches(query, document_key) -> bool
    validate-on-save guarantees query.validated_at is set
    binds document_key as a parameter
    executes on the read-only connection under statement_timeout
    returns existence; raises ConditionExecutionError on SQL error/timeout

conditions.execute(query, params) -> rows
    the same validation/binding/isolation path, for the rare case
    where a module needs rows rather than existence
```

Both entry points share one code path for validation, binding, isolation, timeout, and logging. `matches()` is what §6 uses; `execute()` exists so that a module needing rows still has no reason to open its own cursor.

**During migration** this executor is the replacement for every `jsExecuteBudgetQueries`-equivalent. The sweep semantics change (§2.4 → §5.1) but the execution mechanism is shared from the first module onward, so Budget does not get a bespoke path that PRDO then copies.

### 5.1 The wrapping transformation

JSAP already wraps the stored query as a subquery (`FROM (' + @query + ') AS QueryResults`). We keep that wrapper and add a bound predicate on the document key:

```sql
SELECT 1 FROM ( <query_text> ) AS q WHERE q.<key_column> = %s LIMIT 1
```

`%s` is a real bind parameter via Django's cursor — never interpolated. Properties:

- **Semantics are preserved exactly.** The stored query still defines the candidate set; we only ask whether one document is in it. A document matches here if and only if JSAP's sweep would have included it.
- **Performance is inverted.** PostgreSQL pushes the equality into the subquery, so a query that would have scanned the table becomes an index lookup on the business key.
- **Both JSAP query shapes work unchanged.** A set-selector (`WHERE BRANCH='OIL' AND ...`) gains the document predicate from the wrapper. A query already parameterised (`WHERE id = @id`) still works — the wrapper's predicate is then redundant but harmless. `@id`/`@docentry` → `%s` is a mechanical substitution at import (§16).
- **`LIMIT 1` makes multi-row results a non-issue.** We only ask existence: zero rows ⇒ no match; one or many ⇒ match.

### 5.2 Execution isolation

Queries run on a **dedicated read-only PostgreSQL role and connection alias** (`workflow_ro`), not the app's main connection:

- role is `GRANT SELECT` only, on an explicit allow-list of business tables/views;
- `default_transaction_read_only = on` on the role, so writes fail at the server even if validation is bypassed;
- `statement_timeout` per statement (proposed: 3000 ms);
- connection `autocommit`, outside the caller's transaction, so a slow or failed condition query can never hold locks on the business insert or poison the surrounding transaction.

The read-only connection is the load-bearing control. Everything in §14 is defence in depth on top of it.

### 5.3 Failure semantics

| Situation | Behavior |
|---|---|
| Zero rows | no match; evaluate next query |
| ≥1 row | match |
| SQL error / timeout | **fail the start, roll back**; record `validation_error`, surface to caller |
| `validated_at IS NULL` | never executed; treated as a configuration error |

Failing loudly rather than skipping a broken query is deliberate: silently skipping would route a document down the wrong workflow, which is worse than refusing to submit. JSAP instead swallows errors into `jsSyncErrors` and continues.

---

## 6. Workflow Selection Algorithm

```
WorkflowService.start(module_code, document, context)

 1. module   := workflow_modules[code=module_code]                     (cached)
 2. queries  := workflow_queries q JOIN workflows w ON w.id = q.workflow_id
                  WHERE w.module_id = module.id
                    AND q.validated_at IS NOT NULL
                    -- company scope, on BOTH levels (§4.0.2). No ordering.
                    AND ( w.company_scope = 'ALL'
                          OR (w.company_scope = 'SPECIFIC'
                              AND w.company = :doc_company) )
                    AND ( q.company_scope = 'ALL'
                          OR (q.company_scope = 'SPECIFIC'
                              AND q.company = :doc_company) )        (cached)
 3. key      := getattr(document, module.business_key_column)
 4. matched  := []
    for q in queries:
        if exists( SELECT 1 FROM (q.query_text) AS s
                   WHERE s.<q.key_column or module default> = %s LIMIT 1, [key] ):
            matched.append(q)
 5. workflows_matched := distinct(q.workflow_id for q in matched)
 6. branch on len(workflows_matched):
        0  -> raise WorkflowNotConfigured          (no flow created)
        1  -> proceed
        >1 -> raise AmbiguousWorkflowSelection     (abort, roll back)
 7. create <module>_flow(workflow, matched_query=<the matching query>, ...)
 8. open stage sequence=1
 9. resolve the stage's effective user (§8)
10. create ONE workflow_task
11. append SUBMIT to workflow_action
12. COMMIT
```

Step 4 is bounded: **one indexed existence check per configured query**, typically single digits per module. No business-table scan and no N+1 over documents.

Note step 4 evaluates **every** query rather than stopping at the first match. That is required: stopping early would make the outcome depend on iteration order, which is precisely the implicit ordering this design forbids. Full evaluation is what makes ambiguity detectable.

### 6.1 Exactly one workflow — fail loud, no ordering

```
Execute configured workflow queries
            ↓
Collect matching workflows (distinct)
            ↓
       ┌────┴────┬──────────┐
       0         1          >1
       │         │           │
       ↓         ↓           ↓
WorkflowNot   Select    AmbiguousWorkflow
 Configured   workflow      Selection
                               ↓
                           Rollback
```

**No tiebreak of any kind exists in this design.** Ambiguity is never resolved by workflow `id`, creation order, `sequence`, query `id`, query order, database row order, a default workflow, a hidden priority, **or company specificity**. More than one matching workflow is a configuration error and is raised as one.

That last one is worth stating separately, because it is the natural instinct
and because the existing `approvals` engine does the opposite —
`approvals.services.resolve_workflow` runs
`Q(company=company) | Q(company='')` and then
`.order_by('-company').first()`, deliberately "preferring a company-specific
one over the catch-all". **The workflow engine does not do that.** A
`SPECIFIC` match and an `ALL` match are two matches, and two matches is
`AmbiguousWorkflowSelection`.

#### Worked company-scope examples

Document company = `BEVERAGES`:

| Configuration | Applies? | Outcome |
|---|---|---|
| `Workflow A → SPECIFIC(OIL)` | no | — |
| `Workflow B → SPECIFIC(BEVERAGES)` | yes | |
| `Workflow C → ALL` | yes | |
| | | **2 matches → `AmbiguousWorkflowSelection` + rollback** |

Document company = `MART`, configuration `A → SPECIFIC(OIL)`, `B → ALL`:

| Configuration | Applies? | Outcome |
|---|---|---|
| `Workflow A → SPECIFIC(OIL)` | no | — |
| `Workflow B → ALL` | yes | **1 match → select Workflow B** |

Document company = `OIL`, same configuration:

| Configuration | Applies? | Outcome |
|---|---|---|
| `Workflow A → SPECIFIC(OIL)` | yes | |
| `Workflow B → ALL` | yes | |
| | | **2 matches → `AmbiguousWorkflowSelection`**, NOT a silent pick of A |

The operational consequence is that `ALL` and a `SPECIFIC` override of the
same behaviour cannot coexist for one document. Where a company genuinely
needs different handling, the correct configuration is either narrower
condition queries that are mutually exclusive, or `SPECIFIC` rows for every
company (accepting the duplication for that case only). The admin-side overlap
checker below is what surfaces this before production.

`sequence` on `workflow_stages` is stage execution order and is not read during selection.

**Operational support.** Because ambiguity blocks submission, an admin-side overlap checker should run candidate queries pairwise against recent documents and report collisions before they reach production — making config collisions a build-time problem rather than a runtime one.

### 6.2 Selection when a module re-invokes the engine

When a module resubmits an entry and invokes the engine again (§4.9.1), **selection runs normally and completely**:

```
Module resubmits entry
        ↓
Module logs RESUBMITTED in flow_logs        (module concern)
        ↓
Module invokes Workflow Engine
        ↓
Engine evaluates configured workflow_queries    ← full evaluation, from scratch
        ↓
   0 → WorkflowNotConfigured
   1 → select workflow
  >1 → AmbiguousWorkflowSelection
```

The previous execution's workflow is **not** reused and grants no preference. Specifically, selection must not consult:

- the `workflow_id` from the previous execution;
- workflow id ordering;
- creation order of workflows or queries;
- priority (which does not exist);
- any other hidden or implicit ordering.

The engine re-reads the current configuration and applies §6.1 unchanged. Two consequences follow, and both are intended:

- If configuration changed between executions, a re-invocation may legitimately select a **different** workflow than before. `matched_query_id` on the flow row records which query selected the current execution, so the change is explainable after the fact.
- If configuration has since become ambiguous, a re-invocation raises `AmbiguousWorkflowSelection` even though the first submission succeeded. That is correct: falling back to "whatever ran last time" would be exactly the implicit ordering §6.1 forbids, and would hide a real configuration fault.

---

## 7. Stage Architecture — one stage, one user

```
Workflow
   │
   ├── Stage 1 (sequence=1) → User A
   ├── Stage 2 (sequence=2) → User B
   └── Stage 3 (sequence=3) → User C
```

Each stage has **exactly one** assigned user, stored as `workflow_stages.user_id` (`NOT NULL`). There is no approver collection, no role-based expansion, no grant fallback, and no multiple-approver support anywhere in this design.

### 7.1 Opening a stage

```
Open stage (sequence = N)
      ↓
configured_user := stage.user_id
      ↓
effective_user  := effective_user(configured_user, today)      ← §8
      ↓
create ONE workflow_task(stage_user_id = configured_user, status = PENDING)
      ↓
notify effective_user (post-commit)
```

Exactly one `workflow_task` row is created, enforced by `UNIQUE(module_id, flow_id, stage_id)` (§4.6). There are never sibling tasks to cancel when a stage closes.

### 7.2 Approval and rejection runtime

```
Current Stage
     │
     ▼
Assigned User (effective, per §8)
     │
     ├── APPROVE
     │      ↓
     │   task.status = APPROVED
     │      ↓
     │   Stage Completed
     │      ↓
     │   more stages?  ── yes ──> open next stage (sequence + 1)
     │      │
     │      └── no ──> flow.status = APPROVED ──> enqueue outbox (§13)
     │
     └── REJECT
            ↓
        task.status = REJECTED
            ↓
        flow.status = REJECTED        (this workflow execution ends)
            ↓
        control returns to the module
```

Rejection ends **the workflow execution**, not the document's life. The engine stops: it accepts no further approve/reject on that execution and opens no further stage. What happens next is the module's decision — it may leave the entry rejected, or (if its own rules allow) let the entry be edited, record `RESUBMITTED` in `flow_logs`, and invoke the engine again on the same flow row (§4.9.1, §4.9.2). The engine neither knows nor decides which.

- One approve completes the stage. **No approval count.**
- One reject rejects the whole workflow. **No rejection count.**
- **No quorum, no N-of-M, no partial approval, no pending sibling tasks.**
- Advancing is a single comparison: is there a stage at `sequence + 1`?

### 7.3 Guard: the stage user must be usable

`user_id` is `NOT NULL` and `ON DELETE RESTRICT`, so a stage always names an existing user. Deactivation is still possible, so at the moment a stage opens the engine checks that the **effective** user is active; if not, it raises `StageUserUnavailable` and the transaction rolls back.

This mirrors the existing choice in `approvals/services.py` to surface an unstaffed rung at submit time rather than let a document deadlock days later. Because a stage has exactly one user, this check is the only staffing safeguard in the design — which is the accepted cost of removing role and grant fallbacks (§19.3).

---

## 8. User Replacement Architecture

Resolution is a pure function of `(user_id, date)`:

```python
def effective_user(user_id, on_date):
    r = WorkflowUserReplacement.objects.filter(
            old_user_id=user_id,
            start_date__lte=on_date,
            end_date__gte=on_date,
        ).first()
    return r.new_user_id if r else user_id
```

The exclusion constraint (§4.5) guarantees at most one row matches, so `.first()` is deterministic rather than order-dependent.

### 8.1 Worked example

```
workflow_stages.user_id = 5           ← configuration, never changes

workflow_user_replacements:
    old_user_id = 5
    new_user_id = 8
    start_date  = 15 Sep
    end_date    = 22 Sep

14 Sep  → effective assigned user = 5
18 Sep  → effective assigned user = 8
23 Sep  → effective assigned user = 5     (User 5 automatically resumes)
```

The configured stage user stays 5 throughout. Only the effective runtime assignment changes.

### 8.2 Resolution is dynamic, not baked in

`workflow_task.stage_user_id` stores the **configured** user (5). The effective actor is computed on every read and every action:

- **Inbox** for user *U* = pending tasks where `effective_user(stage_user_id, today) == U`.
- **Authorisation** to act on a task = `effective_user(task.stage_user_id, today) == request.user.id`.

This is what makes "After `end_date` → User 5 automatically resumes" true even for a task that was already open when the window closed. Writing the effective user into the row instead would require rewriting live tasks whenever a window opened or closed — mutating an audit trail, and unable to resume automatically.

It also handles the mid-flight case for free: if a replacement begins while a task is already pending, the task moves to the stand-in on the start date and returns on the end date, with no data change.

### 8.3 Case behavior

| Case | Behavior |
|---|---|
| Before `start_date` | original user |
| During `start_date`…`end_date` (inclusive both ends) | replacement user |
| After `end_date` | original user, automatically |
| Overlapping replacements, same user | **impossible** — barred by the exclusion constraint |
| `old_user == new_user` | **impossible** — barred by CHECK |
| Replacement created after a task exists | task's effective actor changes on the start date; no row rewritten (§8.2) |
| Chained (5→8 while 8→12) | resolved **one hop only** — see below |
| Timezone / date boundary | `timezone.localdate()` in `settings.TIME_ZONE`, evaluated once per operation and passed down, so one operation cannot straddle midnight |
| Audit | `workflow_action.acted_by_user_id` + `on_behalf_of_user_id` record every replacement-driven action |

**Single-hop, not transitive.** Chains resolve one level only. Following chains invites cycles (5→8, 8→5) and makes accountability opaque; one hop is predictable and always terminates. If a stand-in also goes on leave, that is a separate `5→C` row, which the constraint permits provided its window does not overlap.

**The user account is never touched.** No `is_active` flip, no role change, no `users_user` write of any kind. The mechanism affects only workflow actor resolution.

---

## 9. Runtime Flow Architecture

```
Business Entry Created
        ↓
Workflow Engine
        ↓
Load Module Configuration
        ↓
Get document company
        ↓
Load Workflows + their workflow_queries whose company scope applies:
        │     workflow  scope = ALL  or  SPECIFIC(document company)
        └───  query     scope = ALL  or  SPECIFIC(document company)
        ↓
Execute workflow_queries (bound, per-document)
        ↓
Determine matching workflows (distinct)
        ↓
   0 / 1 / >1 handling  ──── 0 ──> WorkflowNotConfigured
        │              └──── >1 ─> AmbiguousWorkflowSelection ──> Rollback
        ↓ (exactly 1)
Create Module Flow
        ↓
Load Stage 1
        ↓
Resolve Stage User  (stage.user_id)
        ↓
Apply User Replacement by Date
        ↓
Create ONE Workflow Task
        ↓
Assigned User Approves / Rejects
        ↓
   ┌───────────────┐
APPROVE         REJECT
   │               │
   ↓               ↓
Stage Complete   Workflow Rejected
   │
   ↓
Next Stage (sequence + 1) — or flow APPROVED if none
```

`WorkflowFlowBase` is abstract; each module supplies the concrete FK:

```python
class BudgetFlow(WorkflowFlowBase):
    document = models.ForeignKey(
        'budget.BudgetDraft', on_delete=models.PROTECT, related_name='flows')

    class Meta:
        db_table = 'budget_flow'
```

The engine operates on the base contract and receives flow instances from the module; it never imports `budget`. A `workflow_modules.flow_table` lookup plus Django's app registry resolves a `(module_id, flow_id)` pair back to a concrete row without a compile-time dependency.

Benefit over one generic runtime table with a `GenericForeignKey`: real FK integrity and `ON DELETE PROTECT` per module, independent migration per module, no cross-module contention on one hot table. The cost is that `workflow_task` / `workflow_action` / `workflow_outbox` hold soft references (§4.6).

---

## 10. Transaction Boundaries

```python
# module view
with transaction.atomic():
    document = BudgetDraft.objects.create(...)      # business insert
    flow = workflow.services.start(                  # selection + flow + task + SUBMIT
        module_code='BUDGET', document=document, context={...})
# COMMIT
transaction.on_commit(lambda: notify(...))           # never inside
```

1. Business insert and `start()` share **one** `transaction.atomic()`. Workflow failure — including `WorkflowNotConfigured`, `AmbiguousWorkflowSelection`, and `StageUserUnavailable` — rolls back the document.
2. Condition queries run on the **separate read-only connection** (§5.2), outside this transaction, so a condition timeout cannot abort the business insert through poisoned transaction state.
3. `approve()` / `reject()` each run in one `transaction.atomic()` that locks the flow row, validates the task, appends the action, updates the task, and opens the next stage or finalises.
4. **No SAP or HTTP call inside any transaction.** Final approval writes `workflow_outbox` and commits; the scheduler picks it up.
5. Notifications fire from `transaction.on_commit` only, so nothing is announced that later rolls back.

---

## 11. Concurrency and Idempotency

| Race | Control |
|---|---|
| Double-click / double submit of an approve | `SELECT ... FOR UPDATE` on the flow row first; the second attempt re-validates and finds the task no longer `PENDING` |
| Double submit of the same document | `UNIQUE(document_id)` on the flow table: a second flow row cannot exist (§4.9.1) |
| Two concurrent `start()` calls on one document | the flow row is taken `FOR UPDATE`; the calls serialise and the second sees `status = 'PENDING'` and raises `WorkflowAlreadyRunning` |
| Module re-invokes the engine while an execution is still running | same guard — `WorkflowAlreadyRunning`; the engine never runs two executions on one flow |
| Duplicate task creation on retry | `UNIQUE(module_id, flow_id, stage_id)` |
| Two schedulers claim one outbox row | `SELECT ... FOR UPDATE SKIP LOCKED`, then a guarded status transition |
| Duplicate SAP post | `UNIQUE(idempotency_key)` plus an `already_posted()` pre-check, following `payments/sap_poster.py:174` |
| Lost update on flow | `lock_version` incremented on every transition |
| History gaps or duplicates | `UNIQUE(module_id, flow_id, sequence)`, sequence computed under the row lock |
| Overlapping replacements | GiST exclusion constraint (§4.5) |

Every control is a database constraint or a row lock. No `if not exists: create()` check-then-act anywhere; `approvals/services.py` already follows the same discipline (`select_for_update()` on every transition).

Note that one-user-per-stage removes a whole class of race entirely: with no sibling tasks, there is no concurrent-approvers-at-one-stage race and no quorum counter to update atomically.

---

## 12. Notifications

Reuse the existing system exactly as `payments/` does — do not build a parallel one.

- `notifications.services.notify(...)` is the single public entry point (`notifications/services/__init__.py:21-23` exports only `notify`).
- `notifications.registry.register(event_name, handler)` inverts the dependency: modules register handlers from their own `apps.py` → `hooks.py`, so `notifications/` imports nothing from them. `register()` raises on duplicate event names.
- Workflow events (`workflow.stage_opened`, `workflow.approved`, `workflow.rejected`) are emitted from `transaction.on_commit`.
- The recipient is the **effective** user (§8). When a replacement is in effect the stand-in is notified; `on_behalf_of_user_id` keeps the trail.

---

## 13. SAP Integration

```
Final approval ──► workflow_outbox(PENDING) ──► COMMIT
                                │
                        APScheduler (existing)
                                │
                 FOR UPDATE SKIP LOCKED ──► claim
                                │
                    SAP Service Layer / HANA
                    ┌───────────┴───────────┐
                 SUCCESS                 FAILURE
                    │                       │
        integration_status=SUCCESS   backoff, attempts++
                                            │
                                     attempts > N ──► DLQ
```

Reuse `sap_sync/scheduler.py` (`add_schedule_job`, `reconcile_schedules`). No Celery, Redis, RabbitMQ, or Kafka.

**Two cautions found during inspection:**

1. **`DjangoJobStore` is commented out** in `sap_sync/scheduler.py:202`. Jobs are in-memory and do not survive a restart. The outbox design is safe from this — durability lives in the table and the scheduler is only a pump — but it should be confirmed rather than assumed.

2. **Payments built an outbox and deleted it.** `SapOutbox` was created in `payments/migrations/0001_initial.py`, altered in `0003`, and dropped by `DeleteModel` in `0005_remove_sapoutbox_content_type_and_more.py`. `SapPostingHistory` was added in `0007` and deleted in `0019`. Today payments posts directly via `sap_poster.post_document()` — reloading under `select_for_update()` immediately before the call, guarding with `already_posted()`, storing `sap_trans_id`, logging to `SapCallLog`, and recovering via `recover_stranded_sap_posts` / `reconcile_sap_cancellations`. No written rationale for the removal exists in the docs. This remains OD-4.

---

## 14. Security — SQL condition execution

Layered, with the database as the last and strongest line.

**Layer 1 — read-only role (primary).** Execution uses `workflow_ro`: `SELECT`-only grants on an explicit allow-list, `default_transaction_read_only = on`, no DDL, no access to other schemas, no `dblink`/FDW. A write attempt fails at the server regardless of what passed validation.

**Layer 2 — parse-time validation** on save, gated by `validated_at`:

- parse `query_text` with `sqlglot` (or `pglast`); reject anything that will not parse;
- require exactly one top-level statement — rejects `;`-chained payloads;
- require the root node to be `SELECT` (or `WITH` resolving to `SELECT`);
- reject any `INSERT/UPDATE/DELETE/MERGE/TRUNCATE/DROP/ALTER/CREATE/GRANT/REVOKE/COPY/CALL/DO` node anywhere in the tree, including subqueries and CTEs;
- walk every referenced relation and require each to be in the module's allow-list (`workflow_modules.business_table` plus an explicit extra-relations list);
- reject `pg_sleep`, `pg_read_file`, `lo_import`, `dblink`, and any function outside a small numeric/date/string whitelist;
- require the declared `key_column` in the projection — otherwise the §5.1 wrapper cannot bind, so the query is unusable by construction.

A query failing validation stores its error and keeps `validated_at` NULL, so step 2 of §6 never selects it.

**Layer 3 — bound parameters.** The document key is always `%s`. Admin text is never concatenated with request data. This removes the specific defect in `bud.jsExecuteBudgetQueries` (`sp_executesql` over a concatenated string).

**Layer 4 — runtime limits.** `statement_timeout = 3000ms`; `LIMIT 1`; autocommit outside the caller's transaction; every execution logged with query id, flow, bound key, duration, outcome.

**Layer 5 — administrative access.** Editing `workflow_queries` is a distinct permission in `core/permission_registry`, separate from ordinary workflow admin, and every change is written to `audit_log` (`audit/models.py:5`). Authoring a condition query is effectively authoring code.

**Residual risk, stated plainly.** Validation is a parser-based allow-list, not a proof; its strength depends on the parser matching PostgreSQL's grammar. That is why Layer 1 exists: even total validation bypass yields only `SELECT` on permitted relations by a role that cannot write. The read-only role is the guarantee; Layers 2–5 reduce how often it gets tested.

---

## 15. Performance

- **Selection cost** is one indexed existence check per configured query for the module — never a table scan (§5.1). This depends on the business key being indexed, which it is as a PK.
- **Config caching.** Modules, workflows, queries and stages are read-mostly; cache per process with invalidation on save.
- **Inbox.** Partial index `(stage_user_id, status) WHERE status = 'PENDING'`. Because the effective actor is computed by date (§8.2), the inbox query joins `workflow_user_replacements` on `old_user_id` for the current date; that table is small and indexed on `(old_user_id, start_date, end_date)`. The join replaces what would otherwise be a live-task rewrite on every leave window.
- **Flow lookups.** Partial index `(status, current_stage_id) WHERE status = 'PENDING'`.
- **Lock scope.** Only the single flow row, only for one transition. Condition queries hold no locks in the caller's transaction (§5.2).
- **Outbox drain.** `FOR UPDATE SKIP LOCKED` over a partial index on `(next_attempt_at) WHERE status = 'PENDING'`.
- **One task per stage** keeps `workflow_task` roughly `stages × documents` rather than `approvers × stages × documents`.
- **What JSAP does that we avoid:** three nested cursors per execution, a full write of every matched row into `bud.TempResults` before any decision, and per-row string-built HANA inserts over a linked server.

---

## 16. Migration Strategy

Module by module, no big bang. `approvals/` keeps serving PAYMENT and DEPOSIT untouched throughout.

```
Phase 0  workflow/ app + config tables + TestFlow harness      ← no business module touched
Phase 1  Budget: budget app + budget_flow + query import       ← first real module
Phase 2  PRDO, then Credit Limit
Phase 3  Backdate, BP Master, Item Master, IMC, BOM, QC, Advance
Phase 4  (later, separate decision) migrate PAYMENT/DEPOSIT off approvals/
```

**Coexistence.** `approvals/` and `workflow/` share no tables and no code. Both run indefinitely. Nothing in this plan modifies `approvals/`.

**Query import.** JSAP query text moves into `workflow_queries` with a mechanical `@id`/`@docentry` → `%s` substitution, then must pass §14 validation before it can execute. Expect some queries to fail — that is the mechanism working, and each failure needs a human decision rather than reflexively widening the allow-list.

**Stage/approver import needs a human decision per stage.** JSAP stages may have several users in `jsUserStage` (§2.1b) while OMS stages take exactly one. There is no automatic way to collapse a multi-user JSAP stage into a single OMS user, so each such stage must be mapped deliberately during migration. Where a JSAP stage genuinely has multiple users, the options are to pick the accountable owner or to split it into consecutive stages. This is migration work, not an architectural question.

**Parity testing.** For each migrated module, run the old sweep and the new per-document evaluation over the same document set and diff the resulting workflow assignment. Because §5.1 preserves set semantics exactly, any difference is a bug in the port, and the diff is the acceptance gate.

**TestFlow first.** Phase 0 delivers a `TestFlow` model and a disposable test document type, proving the generic engine drives an arbitrary concrete flow before any business module is involved. This matters more than usual here: **none of the target modules exist in OMS-Backend** — `budget`, `prdo`, `credit_limit`, `backdate`, `bp_master`, `item_master` are absent from every branch, still JSAP-only. `TestFlow` is the only possible first integration, and Budget must be **built** in OMS before it can be migrated.

---

## 17. Django Implementation Plan

Structure follows baseline convention (flat `models.py`/`services.py` per app, as `approvals/` and `payments/` do), splitting into packages only where size demands it — `notifications/services/` is the precedent.

```
workflow/
    __init__.py
    apps.py
    models.py              # config + task + action + outbox + WorkflowFlowBase
    services/
        __init__.py        # public API: start, approve, reject, cancel, inbox_for
        selection.py       # §6
        conditions.py      # §5 — wrapper, read-only connection
        stages.py          # §7
        replacements.py    # §8
        outbox.py          # §13
    validators.py          # §14 Layer 2
    permissions.py
    serializers.py
    views.py
    admin.py
    hooks.py               # notification registration
    migrations/
    tests/
```

### 17.1 Implementation order (planned — not started)

1. **Workflow configuration tables** — `workflow_modules`, `workflows`, `workflow_queries`, `workflow_stages`, `workflow_user_replacements`
2. **SQL query validation and safe execution layer** — validator (§14 Layer 2), `workflow_ro` role and connection alias, bound wrapper (§5.1)
3. **Generic workflow flow/task/action runtime** — `WorkflowFlowBase`, `workflow_task`, `workflow_action`
4. **TestFlow integration** — concrete flow model proving the engine drives an arbitrary module
5. **Workflow selection and fail-loud ambiguity handling** — §6, including `WorkflowNotConfigured` / `AmbiguousWorkflowSelection`
6. **Stage approval/rejection runtime** — one user, one task; approve → next stage; reject → workflow rejected
7. **User replacement resolution** — date-based effective-actor resolution (§8)
8. **APIs / admin / permissions / notification integration**
9. **First real module migration, starting with Budget**

Steps 1–7 touch no existing app. `workflow_outbox` (§4.8) enters at step 8 or later, gated on OD-4.

---

## 18. Testing Strategy

**No quorum tests exist in this plan.** There are no tests for N-of-M approval, approval counts, rejection counts, approver quorum, partial approvals, quorum completion, or quorum failure — none of those behaviors exist in the design.

### Stage assignment
- a stage has exactly one user; `user_id` is `NOT NULL`
- opening a stage creates **exactly one** `workflow_task` for that user
- `UNIQUE(module_id, flow_id, stage_id)` rejects a second task for the same stage
- opening a stage whose effective user is inactive raises `StageUserUnavailable` and rolls back

### Approval
- assigned user approves → task `APPROVED`, stage completed, next stage opens with its own single task
- approving the last stage → flow `APPROVED`
- a user who is not the effective actor cannot approve

### Rejection
- assigned user rejects → task `REJECTED`, flow `REJECTED` (terminal for that flow)
- no further action is accepted on a rejected flow
- a rejected flow is immutable: `status`, `current_stage_id`, `current_sequence` unchanged after rejection

### Workflow execution state and re-invocation
- a document has **exactly one** flow row; `UNIQUE(document_id)` rejects a second
- re-invoking the engine after a rejection reuses the **same** flow row (assert the primary key is unchanged) and does **not** insert a new one
- re-invocation resets `status` to `PENDING`, `current_sequence` to 1, and re-resolves `workflow_id` / `matched_query_id`; `created_at` is unchanged
- `workflow_action` history from the first execution survives re-invocation intact, and new actions append with a continuing monotonic `sequence` (assert the earlier rows are byte-identical afterwards)
- terminal tasks from the earlier execution are retained; re-opening a stage creates a new pending task without violating the partial unique
- at most one **pending** task per stage at any instant, across any number of executions
- invoking the engine while `status = 'PENDING'` raises `WorkflowAlreadyRunning` and changes nothing
- two concurrent `start()` calls on one document: exactly one begins an execution, the other is refused
- selection on re-invocation runs from scratch: with configuration changed so a *different* workflow now matches, the re-invocation uses the new workflow and records it in `matched_query_id`
- re-invocation with an ambiguous configuration raises `AmbiguousWorkflowSelection` — it does **not** fall back to the previously used workflow
- the engine exposes **no** resubmission API, no attempt counter, and no `RESUBMITTED` action (assert `RESUBMITTED` is not a valid `workflow_action.action` value)
- the engine does not decide whether re-invocation is permitted: given a rejected flow, `start()` succeeds when called, so the policy gate lives in the module (§3.1) — assert this explicitly so the boundary is not accidentally moved into the engine later

### Workflow selection
- 0 matches → `WorkflowNotConfigured`, no flow created
- 1 match → workflow selected, `matched_query_id` recorded
- \>1 matches → `AmbiguousWorkflowSelection`, full rollback, nothing persisted
- outcome is independent of query and workflow row order (shuffle fixtures and assert the same result — guards against reintroducing implicit ordering)

### Company scope
- **T1 specific company** — `Workflow A → SPECIFIC(OIL)`, document OIL → A matches
- **T2 ALL** — `Workflow A → ALL`, document OIL → A matches
- **T3 ALL works for every company** — one `ALL` row; documents in OIL,
  BEVERAGES and MART each match it
- **T4 specific + ALL both apply** — `A → SPECIFIC(OIL)`, `B → ALL`,
  document OIL → **2 matches → `AmbiguousWorkflowSelection` + rollback**;
  assert A is NOT preferred and that no flow row was created
- **T5 specific does not leak** — `A → SPECIFIC(OIL)`, document BEVERAGES →
  A does not match (with no other config, `WorkflowNotConfigured`)
- **T6 no duplication for ALL** — after configuring one `ALL` workflow and
  running documents for three companies, assert `Workflow.objects.count() == 1`
  and `WorkflowQuery.objects.count() == 1` — the ALL case must never be stored
  as one row per company
- query-level scope narrows within an applicable workflow: workflow `ALL` with
  queries `SPECIFIC(OIL)` and `SPECIFIC(BEVERAGES)` evaluates only the matching
  one per document, and is **one** workflow match either way (not ambiguity)
- contradiction refused: a query `SPECIFIC(BEVERAGES)` under a workflow
  `SPECIFIC(OIL)` is rejected by the serializer/admin
- the CHECK constraints reject every invalid representation:
  `(ALL, 'OIL')`, `(SPECIFIC, NULL)`, and an unknown company code
- company-scope filtering adds no ordering: shuffling row creation order
  leaves every outcome above unchanged

### SQL configuration
- workflow with **one** SQL query
- workflow with **multiple** SQL queries (OR semantics; each can match alone)
- query validation: `;` chaining, CTE-hidden DML, disallowed relation, `pg_sleep`, missing `key_column`
- bound document-key execution: correct document matches, others do not
- SELECT-only restriction: a write attempt on `workflow_ro` fails at the server
- statement timeout: a slow query is killed and does not abort the caller's transaction
- invalid query handling: SQL error fails the start and rolls back
- zero-row and multi-row results both handled (no match / match)

### User replacement
- before `start_date` → old user
- during the range, including both boundary dates → new user
- after `end_date` → old user, automatically, **for a task that was already open**
- a replacement created after a task exists changes the effective actor without rewriting the task row
- chain 5→8, 8→12 resolves one hop (5 → 8)
- overlapping replacements for one user are rejected by the exclusion constraint (`15–22 Sep` then `20–25 Sep` ⇒ `IntegrityError`)
- **adjacent, non-overlapping** windows for one user are accepted (`15–22 Sep` then `23–30 Sep`)
- inclusive bounds: `15–22 Sep` then `22–30 Sep` **is** an overlap and is rejected
- replacements for two *different* `old_user_id` values with identical dates are both accepted
- concurrent inserts of two overlapping windows: exactly one commits (the constraint holds under concurrency, not just serially)
- the `btree_gist` extension is present in the test database — a migration-level check, since the constraint cannot be created without it
- `old_user_id == new_user_id` rejected by CHECK
- **the actual user account is never modified** — assert `users_user.is_active`, role, and all other fields are byte-identical before and after a replacement is created, applied, and expires
- inbox: task appears for the effective user and not for the configured user during the window

### Transactions and concurrency
- workflow failure rolls back the business insert; no notification on rollback
- no SAP call inside a transaction
- two concurrent approvals of one task: one wins, the other re-validates and is refused
- double submit of one document blocked by the partial unique constraint
- two schedulers over one outbox row
- duplicate task creation under retry

### Isolation from `approvals/`
- payments and deposits behave identically with `workflow/` installed

### Test database — decided

**A disposable PostgreSQL database/environment must be available for the test suite.** SQLite is not an acceptable substitute for the tests below, because it would report success while exercising none of the real behavior:

| Requires real PostgreSQL | Why SQLite cannot stand in |
|---|---|
| `UNIQUE(document_id)` on flow tables | expressible, but meaningless without the locking tests beside it |
| partial unique constraints (`WHERE status = 'PENDING'`) — tasks (§4.6) | SQLite supports partial indexes but not the concurrent behavior being tested |
| GiST exclusion constraint (§4.5) | no `EXCLUDE USING gist` at all |
| `btree_gist` extension (§4.0.1) | no extensions; the constraint cannot even be created |
| concurrent workflow actions | no meaningful row-level locking |
| transaction / locking behavior — `select_for_update()`, `FOR UPDATE SKIP LOCKED` | `select_for_update()` is a silent no-op on SQLite |
| race-condition testing (`WorkflowAlreadyRunning`, double-approve, outbox claim) | needs two real concurrent sessions |
| the read-only role refusing writes (§5.2) | no role system |
| `statement_timeout` | not supported |

The precedent for guarding on the backend already exists — `payments/migrations/0012` checks `connection.vendor != 'postgresql'` because the suite may run on SQLite. **These tests must not be skipped or rewritten to pass on SQLite**: a skipped concurrency test is indistinguishable from a passing one in CI, which is precisely the failure this decision prevents.

Practical requirements: a throwaway database the suite may create and drop, with a migrating role holding `CREATE EXTENSION` for `btree_gist` (§4.0.1), and the `workflow` schema plus `workflow_ro` role provisioned the same way as production so isolation tests are real. Not to be created during the planning phase.

---

## 19. Open Decisions and Risks

### 19.1 Closed — do not reopen

| Decision | Outcome |
|---|---|
| Users per stage | **exactly one** (`workflow_stages.user_id`) |
| Quorum | **none** — 1 approval completes a stage, 1 rejection ends the workflow |
| `workflow_stage_approvers` | **not created** |
| `approval_required` / `rejection_required` | **not created** |
| SQL queries | **separate `workflow_queries` table**, 1:N from `workflows` |
| Workflow priority | **none** — no `priority`, no `selection_priority`, no implicit ordering |
| Company scope | `company_scope` (`ALL`/`SPECIFIC`) + nullable `company`, on **both** `workflows` and `workflow_queries`; mutually exclusive by CHECK (§4.0.2) |
| Company domain | the existing **`OIL`/`BEVERAGES`/`MART`** document enum, CHECK-constrained; **no new company master**, and no FK onto `users.Company` or `orders.Categories` (different concepts) |
| ALL = one row | a single `ALL` configuration serves every company; never duplicated per company |
| Specific vs ALL precedence | **none** — both matching is `AmbiguousWorkflowSelection`, unlike `approvals.resolve_workflow` |
| Query scope vs workflow scope | query may only **narrow** the workflow's scope; contradictions refused at validation |
| `is_active` | **not added**; no substitute active-state mechanism invented |
| Ambiguous selection | **fail loud** — `AmbiguousWorkflowSelection` + rollback |
| User replacement | **date-based mapping table**; account never modified |
| Resubmission | **a module-level business concept, not an engine concept** (§3.1) |
| Module-flow row on resubmission | **the same row is reused**; the engine never creates a second (§4.9.1) |
| Resubmission history | recorded by the module in **`flow_logs`** (§4.9.2); no generic engine log |
| Resubmission policy | **owned by the module** — the engine does not decide if, or how often |
| Flow uniqueness | `UNIQUE(document_id)`, defined on **workflow execution state** (§4.9.1) |
| Duplicate simultaneous execution | barred by the row lock + `WorkflowAlreadyRunning` guard |
| `round_number` / attempt counter | **not created**, in any form |
| JSAP `jsQuery` columns | **reviewed and documented** (§2.5); `type` semantics recorded as unverifiable |
| Query execution mechanism | **one shared generic executor** for all modules (§5.0) |
| PostgreSQL schema | **dedicated `workflow` schema** (§4.0) |
| `btree_gist` | **available** and required; declared in the first migration (§4.0.1) |
| Replacement overlap | **rejected by the database** for the same `old_user_id`; adjacent windows allowed |
| Test database | **disposable PostgreSQL required**; SQLite not an acceptable substitute (§18) |
| Implementation baseline | **`production`** (merged into `JSAP`); `main` is stale and unused |

The four conflicts raised in the previous revision are all closed: multi-flow ambiguity by fail-loud selection; batch-sweep vs per-document by synchronous evaluation at submit (§9); JSAP's per-assignment windows by the replacement mapping (§8); and the quorum question by the one-user-per-stage decision. The former `CONFLICT-D` (quorum) no longer exists.

### 19.2 Genuinely unresolved

| # | Decision | Why it matters |
|---|---|---|
| **OD-4** | Build `workflow_outbox`, given payments deleted theirs? | Rebuilding an abandoned pattern is a real risk (§13). The alternative is payments' direct-post-plus-reconcile approach. Gates step 8 of §17.1, nothing earlier. |

This is the only unresolved architectural decision, and it blocks nothing in steps 1–7 of §17.1.

**Resolved and moved to §19.1:** OD-1 (jsQuery columns — documented in §2.5), OD-2 (common query executor — §5.0), OD-3 (dedicated `workflow` schema — §4.0), OD-5 (resubmission is module-level — §3.1, §4.9.1), OD-6 (`btree_gist` available — §4.0.1), OD-7 (disposable PostgreSQL test database — §18).

The numbering gap is deliberate: OD-4 keeps the identifier used in earlier review rounds so references from prior discussion stay valid. New items should continue from OD-8.

**One data question, not a design question, remains open inside a resolved decision:** the business meaning and SQL type of `jsQuery.type` could not be recovered from the available source (§2.5). It is answerable only by inspecting the live `jsQuery` rows or the `jsAddQuery` / `jsGetQueryName` procedure bodies. The design accommodates it either way — `type` is carried for parity and nothing depends on it — so it is not listed as an unresolved architectural decision.

### 19.3 Risks

| Risk | Mitigation |
|---|---|
| One user per stage is a single point of failure | `StageUserUnavailable` guard at stage open (§7.3) plus the replacement mechanism (§8) are the only safeguards, by design. Staffing changes are stage edits. |
| No role-based assignment | Accepted consequence of one-user-per-stage. Staff churn means editing stage rows; there is no role or grant fallback as in `approvals/`. |
| Admin-authored SQL is the largest attack surface | Read-only role is the guarantee, not validation (§14); separate permission; full audit |
| Imported JSAP queries fail validation | Expected; each is a human decision, not an automatic allow-list widening |
| Multi-user JSAP stages cannot be auto-migrated | Per-stage human mapping during migration (§16) |
| `DjangoJobStore` disabled ⇒ jobs lost on restart | Durability lives in the outbox table, not the scheduler |
| Two approval engines coexisting long-term | Strict isolation, no shared tables; migrate on a schedule |
| No target module exists in OMS-Backend yet | `TestFlow` is the only possible first integration; Budget must be built before migration |
| Losing design records again | This document is committed to the repo; the two it replaces were local-only and lost |

---

## 20. Final Status

```
PLAN STATUS: READY FOR FINAL REVIEW

Resolved:
- Exactly one user per stage
- Exactly 1 approval per stage
- Exactly 1 rejection per stage
- Separate workflow_queries table
- No workflow priority
- No is_active
- Company scope: ALL or SPECIFIC, one row for ALL, no specific-over-ALL precedence
- User replacement with date range
- Resubmission is a module-level business concept
- Same module_flow row may be reused after resubmission
- Resubmission history is recorded in flow_logs
- Workflow Engine does not own resubmission policy
- Exact JSAP jsQuery columns reviewed/documented
- Common query execution mechanism for jsExecuteBudgetQueries migrations
- Dedicated PostgreSQL schema for Workflow Engine
- btree_gist is available
- Disposable PostgreSQL test database/environment
- Production branch baseline

Still unresolved:
- OD-4 — whether to build workflow_outbox

Implementation:
NOT STARTED
```

### 20.1 Consistency verification

The updated plan was re-read and checked for contradictions against the final decisions:

| Prohibited concept | Status in this document |
|---|---|
| multiple approvers | absent from the design; appears only in §2.1b/§2.1d/§2.3/§16 as recorded JSAP fact and explicit departure |
| `workflow_stage_approvers` | absent; §4.4 states the table is not created |
| `approval_required` | absent from all schema; §2.1d records it as JSAP-only, §4.4 and §19.1 state it is not created |
| `rejection_required` | same as above |
| quorum | no quorum logic anywhere; §7.2, §11, §18 and §19.1 state its absence explicitly |
| `priority` | no selection priority anywhere; §2.1a distinguishes JSAP's stage-ordering `priority` from selection priority; §6.1 rules out every tiebreak |
| `selection_priority` | never introduced |
| `is_active` (on workflows) | never introduced; §4.2 explains lifecycle without it |
| stale `main` as baseline | baseline is `production` throughout (§0.2); `main` appears only to record the earlier error and the Django-version correction |

`sequence` is retained on `workflow_stages` for stage execution order only, and §4.4, §6.1 and §7 each state that it is not used in workflow selection.

The newly resolved decisions were checked for consistent treatment across every section that touches them:

| Topic | Stated consistently in |
|---|---|
| resubmission is **module-owned**, not an engine concept | §1, §3.1, §4.9.1, §4.9.2, §7.2, §18, §19.1 |
| the **same** module_flow row is reused; engine never creates a second | §3.1, §4.9 constraint, §4.9.1, §18, §19.1 |
| `flow_logs` is module-owned and holds `RESUBMITTED` | §3.1, §4.9.2, §18, §19.1 |
| engine has no `RESUBMITTED` action and no resubmission API | §3.1, §4.7 action list, §4.9.2, §18 |
| no `round_number`, no attempt counter | §4.6, §4.9.1, §19.1 |
| flow uniqueness is on **execution state** (`UNIQUE(document_id)`) | §4.9, §4.9.1, §11, §18 |
| duplicate simultaneous execution barred | §4.9.1 guard, §11 (three rows), §18 |
| rejection ends the **execution**, and control returns to the module | §7.2, §4.9.1, §18 |
| selection on re-invocation runs from scratch, no reuse of prior workflow | §4.9.1, §6.2, §18 |
| no priority or implicit ordering anywhere | §4.2, §6.1, §6.2, §19.1 |
| exact `jsQuery` columns documented; `type` recorded unverifiable | §0.4, §2.5, §4.3, §19.2 |
| company scope is ALL or SPECIFIC, mutually exclusive by CHECK | §4.0.2, §4.2, §4.3, §18 |
| ALL is one row, never duplicated per company | §4.0.2, §18 (T6), §19.1 |
| no specific-over-ALL precedence; both matching is ambiguity | §4.0.2, §4.2, §6.1 (worked examples), §18 (T4), §19.1 |
| company scope lives on workflow AND query, query narrowing | §4.0.2, §4.3, §6, §18 |
| no new/duplicated company master, no FK onto the wrong domain | §4.0.2, §19.1 |
| one shared query executor; no per-module SQL | §5.0, §16, §17 |
| dedicated `workflow` PostgreSQL schema | §4.0, §5.2, §18, §19.1 |
| replacement overlap rejected by the database; adjacent allowed | §4.5, §8.3, §11, §18 |
| `btree_gist` required and available | §4.0.1, §4.5, §18, §19.1 |
| disposable PostgreSQL test DB required; SQLite not a substitute | §18, §19.1 |

No section describes a new module-flow row being created for a resubmission, resubmission as an engine-owned lifecycle, an engine-level resubmission policy or counter, `round_number`, multiple users per stage, quorum, workflow priority, `is_active` on workflows, duplicated ALL-company configurations, a duplicated company master, implicit specific-company-over-ALL precedence, or `main` as the baseline.

### 20.2 Database verification — company scope

The verification scripts in the final report must additionally show, for both
`workflows` and `workflow_queries`:

* the `company_scope` and `company` columns with nullability and defaults;
* all three company CHECK constraints (`information_schema.check_constraints`
  joined to `table_constraints`), so the mutual exclusivity is visible;
* the `(module_id, company_scope, company)` and
  `(workflow_id, company_scope, company)` indexes;
* a `SELECT code, name, company_scope, company FROM workflow.workflows` so an
  operator can read applicability off one row without inspecting children;
* an ambiguity pre-check an operator can run before production:

```sql
-- Workflows in one module that would both apply to the same company.
-- Any row returned is a latent AmbiguousWorkflowSelection.
SELECT a.module_id,
       a.code AS workflow_a, a.company_scope AS scope_a, a.company AS company_a,
       b.code AS workflow_b, b.company_scope AS scope_b, b.company AS company_b
FROM workflow.workflows a
JOIN workflow.workflows b
  ON a.module_id = b.module_id
 AND a.id < b.id
 AND (a.company_scope = 'ALL'
      OR b.company_scope = 'ALL'
      OR a.company = b.company)
ORDER BY a.module_id, a.code, b.code;
```

Note this reports *potential* overlap only — whether both actually match also
depends on their condition queries, which only the engine can evaluate.

### 20.3 API payloads — company scope

Configuration payloads for `POST /api/workflow/workflows/` and
`POST /api/workflow/queries/` carry the pair explicitly, so a client can never
express the ambiguous combination:

```json
{ "company_scope": "ALL" }                                  // company omitted / null
{ "company_scope": "SPECIFIC", "company": "OIL" }
```

The serializer rejects `{"company_scope": "ALL", "company": "OIL"}` and
`{"company_scope": "SPECIFIC"}` with a field error rather than letting the
database CHECK surface as a 500.
