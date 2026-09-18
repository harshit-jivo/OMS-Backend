# Budget Approval — how JSAP does it, and what OMS has to reproduce

Companion to `PRODUCTION_ORDER_JSAP_SAP.md`. Written for whoever builds the
budget module in OMS.

This is **not** a defect list. Budget approval in JSAP works, and the shape of it
is sound — a generic workflow engine driven by per-stage SQL queries, gating SAP
drafts through a table SAP's own notification procedure reads. That design is
worth keeping. Section 7 lists what is broken so it is not copied across;
everything before it is the working model.

Everything here was read out of `jsaplive3`, `JIVO_OIL_HANADB` and the live API
on 2026-09-18.

---

## 1. What the approval actually gates

A budget approval is not advisory. It decides whether a SAP document can be
posted at all.

```
user creates a document in SAP  ->  SAP saves it as a DRAFT (ODRF/DRF1)
                                    and raises its own approval request (OWDD/WDD1)
        |
        v
JSAP reads those drafts, tags each LINE with a budget + month, routes it
        |
        v
approver decides in JSAP  ->  JSAP writes the decision into
                              JIVO_OIL_HANADB."tbl_Draft_Approvals"
        |
        v
SAP's SBO_SP_TRANSACTIONNOTIFICATION checks that table and refuses to post
a document whose lines are not there:

    IF NOT EXISTS ( SELECT 1 FROM "tbl_Draft_Approvals"
                    WHERE "DocEntry" = :DRAFTKEY18
                      AND "LineNum"  = :LINECOUNT18
                      AND "ObjType"  = '14' )
```

So the unit of approval is the **document line**, not the document, and the
enforcement point is inside SAP. OMS must write to the same table (or replace
the gate) or its decisions will have no effect.

Gated object types: **14** A/R Credit Memo, **15** Delivery, **18** A/P Invoice,
**19** A/P Credit Memo, **46** Outgoing Payment, **59** Goods Receipt, **60**
Goods Issue. A/P Invoice dominates in practice (7,035 OIL + 1,900 BEV documents).

---

## 2. Intake — what enters approval

HANA procedure **`JIVO_OIL_HANADB."DRAFT_APPROVAL"`** (244 lines) is the source
of truth. It returns one row per draft document line, joining:

| source | gives |
|---|---|
| `ODRF` + `DRF1` | the draft document and its lines, `ObjType IN (14,15,18,19,59,60)` |
| `OWDD` + `WDD1` | SAP's approval request; only `WDD1."Status"='Y'` and `OWDD."ProcesStat"='Y'` |
| `OACT` | account code / name |
| `OPRC` + `OHEM` | cost-centre owner and co-owner (`CCOwner`, `U_Co_Owner`) -> `Budget_Owner`, `Approver Name` |
| `OBGS` + `OBGT` | SAP's *own* budget for the period (`Current_month_Budget`) |
| `OJDT` + `JDT1` | month-to-date spend, expense accounts under parent `5600000`, excluding `5680014` |

The budget dimension is the cost centre **`OcrCode3`**. `CURRENTMONTH` and
`EFFECTMONTH` are `MM-YYYY` strings.

`bud.InsertBudgetFromHANA` (SQL Agent job *Budget Sync Data*, every 5 minutes)
calls it over linked server `HANADB112` and lands the rows in SQL Server.

> **Only OIL has `DRAFT_APPROVAL`.** The procedure's own comment says so and I
> confirmed it — Beverages and Mart do not have it. Beverage documents reach
> budget approval by some other route. Resolve this before building
> multi-company.

---

## 3. Routing — the workflow engine

This is JSAP's generic engine, shared by every module. It is the part OMS
already has an equivalent of.

```
jsApproval            module registry, per company
  id 5  = Budget Approval          id 15 = Budget Allocation
  id 13 = Production Order         id 11 = backdate ...
   |
jsTemplate            a workflow, scoped to one company, isActive flag
   |
jsTemplateQuery ----> jsQuery      the SQL that SELECTS which lines belong here
   |
jsStageTemplate       ordered stages (priority = 1..n)
   |
jsStage               a stage; approvalId -> jsApprovalCount.approval = how many
   |                  approvals this stage needs
jsUserStage           which user(s) sit on the stage, with an optional
                      startTime/endDate/status window = delegation
```

**Per-stage SQL queries are the routing mechanism.** `dbo.jsGetQueries(@userId,
@company, 5)` returns the queries attached to the stages that user occupies;
`bud.jsExecuteBudgetQueries` runs them against the pulled draft lines, and
whatever they select becomes that user's work. There is no hard-coded routing
rule anywhere — it is all query configuration.

`bud.jsProcessAllUsersBudgetApprovals(@company)` loops **every** user in
`jsUserCompany`, runs their queries, and inserts what matched into:

* `bud.jsDocEntry` — one row per document in approval: `status` P/A/R,
  `currentStage`, `currentStageId`, `totalStage`, `templateId`
* `bud.jsDocEnrtyDetail` — the lines (`objType`, `company`, `lineNum`, `visOrder`)
* `bud.jsBudgetTable` — the budget tagging (`BUDGET`, `CURRENTMONTH`, `amount`)

### OMS already has all of this

| JSAP | OMS `workflow` app |
|---|---|
| `jsApproval` | `WorkflowModule` |
| `jsTemplate` | `Workflow` (company `ALL`/`OIL`/`BEVERAGES`/`MART`) |
| `jsStageTemplate` + `jsStage` | `WorkflowStage` |
| `jsUserStage` (+ its date window) | `WorkflowStage.user` + `workflow_user_replacements` |
| `jsTemplateQuery` + `jsQuery` | `WorkflowQuery` — **with `validated_at` as an execution gate, which JSAP has no equivalent of** |
| `jsApprovalCount.approval` | (no equivalent — OMS stages are single-user) |
| `bud.jsDocEntry` | the flow row |
| `bud.jsBudgetStatusWorkflow` | the action log |

Two real differences to decide on:

1. **JSAP stages can require N approvals from M users; OMS stages hold exactly
   one user.** In live data every budget stage is `approval = 1` with one user
   assigned, so the multi-approver capability is configured but unused. Building
   OMS single-user matches reality and matches PRDO.
2. **JSAP runs the queries per user; OMS runs selection per document.** OMS's
   `selection.select_for_module` + `conditions.validate_and_stamp` is the
   stricter model — 0 matches raises `WorkflowNotConfigured`, more than 1 raises
   `AmbiguousWorkflowSelection`. JSAP silently produces nothing.

---

## 4. The budget limit

Separate from routing, and separate from SAP's own `OBGS`/`OBGT` budget.

```
bud.Budgets                     25 rows: (company, budgetName, totalAmount, isActive)
  |                             company 1 = OIL (14 rows), 2 = BEVERAGE (11 rows)
  |-- bud.BudgetMonthlyAllocations   (budgetId, allocationMonth, allocatedAmount)
  |
bud.SubBudgets (50)  --  bud.SubBudgetMonthlyAllocations (52)
```

`Budgets.totalAmount` is **0.00 for all 25 rows and is never read** by any
decision path. The monthly allocation is the only number that matters.

The limit is checked in exactly one place, `bud.jsAutoApproveBudgetAfter48Hours`:

```sql
SELECT @monthlyAllocated = bm.allocatedAmount
FROM bud.BudgetMonthlyAllocations bm
JOIN bud.budgets b ON b.budgetid = bm.budgetId
WHERE b.company = @docCompany
  AND bm.allocationMonth = @currentMonthDate
  AND b.budgetName = @budgetType;

IF (@monthlyUsed + ISNULL(@currentDocAmount,0)) > ISNULL(@monthlyAllocated, 0)
    -> SKIPPED_LIMIT
```

`@monthlyUsed` is the sum of `amount` over **other** documents with the same
company / month / budget whose `status = 'A'`.

Note what this means: **a human approver is never shown or blocked by the
limit.** It only gates the 48-hour auto-approval. If OMS wants the budget to
constrain people, that is new behaviour, not a port.

Allocations are themselves an approval workflow (module id 15, *Budget
Allocation*): `CreateBudgetAllocationRequest` -> `jsApproveBudgetAllocationRequest`
-> writes `BudgetMonthlyAllocations`.

---

## 5. Deciding

**Human path** — `bud.jsApproveBudget(@docId, @company, @userId, @remarks,
@action)` and `bud.jsRejectBudget(...)`, called from the API.

1. Look up the user's stage against the document's `currentStage`.
2. Write or refresh a row in `bud.jsBudgetStatusWorkflow` (`docId`, `stageId`,
   `templateId`, `userId`, `status`, `description`).
3. Count `status='A'` rows at this stage and compare to
   `jsApprovalCount.approval`.
4. If satisfied and `currentStage = totalStages` -> `jsDocEntry.status = 'A'`
   and sync `'A'` to HANA. Otherwise advance `currentSatge` / `currentStageId`
   to the next `jsStageTemplate.priority` and sync `'V'`.
5. Rejecting sets `status='R'` and syncs `'R'`. A later approval on a rejected
   document flips it back to `'P'` — **rejection is not terminal**.

**Auto path** — `bud.jsAutoApproveBudgetAfter48Hours` (job *48 hrs*, every 10
minutes, run as userId 75, Gagandeep Singh). Per pending document: if
`DATEDIFF(HOUR, last action at this stage, now) >= 48` and the budget guard
passes, it inserts approval rows on the user's behalf and advances the same way.

**Write-back** — `bud.jsSyncBudgetToHanaDraftApproval(@docId, @company, @status,
@userId, @remarks)` builds a `DO BEGIN ... END` block, runs it `AT HANADB112`,
upserts the line into `tbl_Draft_Approvals` setting `ApprovedStatus` and
`ACOMMENT`, then **re-reads the rows back and throws if the count does not
match**:

```
THROW 51001, 'HANA sync incomplete: not all lines updated (missing draft or wrong company).'
```

That verify-after-write is good practice and worth keeping in OMS.

Status vocabulary written to SAP: **`A`** final approved, **`V`** moved to next
stage, **`R`** rejected.

> Live `tbl_Draft_Approvals` holds `R` 242,414 / `V` 56,258 / `A` 2,874 / NULL
> 1,362. JSAP itself has only ever rejected **312** documents, so `R` in that
> table cannot mean "an approver rejected this" — most likely it is the initial
> state of an undecided line. Confirm before relying on it.

---

## 6. The API

Base `http://138.252.101.118:5001` — this is already `JSAP_API_BASE` in the OMS
`.env`. Endpoints live under `/api/auth/`. All take `userId`, `company`, and
`month` as `MM-YYYY`.

| endpoint | method | note |
|---|---|---|
| `budgetstatusCount` | GET | counts behind the badges |
| `getpendingbudgetwithdetails` | GET | the approver's queue |
| `getapprovedbudgetwithdetails` / `getrejectedbudgetwithdetails` / `getallbudgetwithdetails` | GET | |
| `getDocEntryData`, `getTotalAmountOfOneDocEntry` | GET | one document |
| `getuserbudgettypes`, `GetUserBudgetSummaryByType` | GET | filters (`getuserbudgettypes` currently returns HTTP 500) |
| `getflowdocentry`, `GetFlowDocEntryTwo`, `GetBudgetApprovalFlow` | GET | the stage trail |
| `approvebudget`, `rejectebudget` | POST | `?userId=&docEntry=&company=&remarks=` |
| `updatebudget` | POST | |

---

## 7. What NOT to carry across

All measured on live data, 2026-09-18.

### 7.1 The queue is filtered to one budget month — this is the whole backlog

`bud.jsGetBudgetInsight` step 4 ends:

```sql
WHERE bt.CURRENTMONTH = @month
```

`CURRENTMONTH` is the **budget effect month of the line**, not the document's
age. The approver's queue therefore shows one month at a time. Verified against
the live API for `arsh` (userId 95), who has 250 pending documents spread over
**14 different months**:

```
budgetstatusCount   no month  -> totalPending   0
                    09-2026   -> totalPending  11
                    02-2026   -> totalPending 124
```

With no month parameter the count is **zero**, so any screen that loads without
an explicit month shows an empty queue. **695 documents are pending, the oldest
from May 2025, and the people they are assigned to cannot see them.** This is
not a backlog of unwilling approvers; it is a filter.

Ruled out as causes: `jsTemplateApproval.approvalId=5` is present for all 712
pending documents; inactive templates account for only 37; **zero** documents
have already been acted on by their assigned approver; every stage is 1 approval
with 1 user.

**In OMS: scope the queue by assignee and status. Month is a filter the user
chooses, never a default.**

### 7.2 The 48-hour auto-approver is dead

`BudgetMonthlyAllocations` stops at **2026-03-01** (31 rows, last written
2026-03-18). Because the guard is `ISNULL(@monthlyAllocated, 0)`, a missing
month is a **zero limit**, so every document fails it. 688 of 689 pending
documents that carry a budget line have no allocation row.

Cost: `bud.jsAutoApprovalLog` is 6.5M rows / **1.4 GB**, writing ~90,000
`SKIPPED_LIMIT` rows a day, every one reading `> Allocated=0.00`.

**In OMS: a missing allocation must BLOCK loudly and surface as "unconfigured",
never be silently read as zero.**

### 7.3 Hardcoded approver exclusions

```sql
DECLARE user_cursor CURSOR ... SELECT TOP (@approvalRequired - @approvedCount) us.userId
FROM jsUserStage us WHERE us.stageId = @currentStageId
  AND us.userId NOT IN (68, 79, 95)     -- Nirmal Kaur, Gurpreet Singh, Arshdeep Singh
```

**292 of the 695 pending documents (42%)** sit on a stage whose only users are
all on that list, so the 48-hour escape hatch can never fire for them. Where
such a document also passes the budget guard, the procedure logs `AUTO_APPROVE`
with `approvalsAdded = 0` and the document never moves — document **15830** has
done exactly that every 10 minutes since 2026-07-08.

**In OMS: exclusions are configuration. And a no-op auto-approval must not log
as though it did something.**

### 7.4 An unscoped shared scratch table

`dbo.jsGetPendingBudgetDataRelatedToUser`, `dbo.jsExecuteSelectedQueries` and
`dbo.jsSaveDataInSAP` each run:

```sql
DELETE FROM dbo.TempResults;   -- every user's rows, not just this caller's
```

Two approvers loading their list at the same time wipe each other. The newer
`bud.jsExecuteBudgetQueries` fixed this properly with an `executionId` and
deletes only its own rows — but the old procedures are still live.

**In OMS: no shared scratch tables. Query directly, or scope per request.**

### 7.5 Log volume

| table | rows | size | growth |
|---|---|---|---|
| `bud.jsProcessLog` | 31.3M | **2.7 GB** | **127,296/day** — 288 runs x 221 users, 92% of them "No pending budget approvals found" |
| `bud.TempResults` | 70,705 live | **2.3 GB** | fragmented dead pages, 178 orphaned executions |
| `bud.jsAutoApprovalLog` | 6.5M | 1.4 GB | ~90,000/day |

About 6.5 GB to record that nothing happened. **Log transitions, not polls.**

### 7.6 The linked server is unreliable

`bud.jsSyncErrors` (139k rows) is a **trace** log, not an error log —
"Executed Successfully" and "Built HANA Query" rows are in it. Real failures are
~63,657, dominated by the `HANA112` -> `HANADB112` rename (44,368 plus 14,728
"could not find server"). Still failing today:
`jsExecuteBudgetQueries-DetailInsert`, 83–165/day.
`jsSyncBudgetToHanaDraftApproval` has only **900** real failures, so the
approval write-back itself mostly works.

**In OMS: the SAP write is the part that must be retryable and observable — see
the `RetrySapView` pattern in the `production` app.**

---

## 8. Suggested shape in OMS

* Reuse the existing `workflow` app. Add a `WorkflowModule` for budget; do not
  build a second engine.
* One request row per **SAP draft document**, with lines — the approval unit is
  the line, and `tbl_Draft_Approvals` is keyed `(DocEntry, LineNum, ObjType)`.
* Intake as a scheduled command with the noisy-failure discipline already used
  by `sync_production_orders`: an unreachable SAP **raises**, a silent run is
  reported, and `synced_at` is stamped so "when did this last work?" is
  answerable from the data. JSAP's production feed dying unnoticed for 33 days
  is exactly what that guards against.
* Pull the draft lines from SAP directly rather than through JSAP. PRDO showed
  why: 17.9% of JSAP's production-order rows carry an item that does not match
  the SAP planned order.
* Keep: the verify-after-write on the SAP sync, and the per-stage query
  configuration model.
* Decide explicitly whether the monthly allocation blocks a **human** approver.
  In JSAP it does not. That is a business decision, not a porting detail.

---

## 9. Open questions

1. How do Beverage and Mart documents enter budget approval, when
   `DRAFT_APPROVAL` exists only in `JIVO_OIL_HANADB`?
2. What does `ApprovedStatus = 'R'` mean on the 242,414 `tbl_Draft_Approvals`
   rows, when JSAP has only ever rejected 312 documents?
3. Is the 48-hour auto-approval wanted in OMS at all? It has effectively not run
   since March 2026 and nobody appears to have noticed.
4. Who owns maintaining monthly allocations, and should a month with no
   allocation block posting or allow it?
