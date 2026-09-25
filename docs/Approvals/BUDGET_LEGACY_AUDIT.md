# BUDGET — Legacy JSAP Audit

**Revision 2 — 2026-09-24.** Rewritten from the **live SQL definitions** of every
budget object in database `jsaplive3`, supplied by the user from SSMS, plus read-only
checks of live data and SQL Agent jobs. Revision 1 (2026-09-23) could read no
definition and left most questions `[UNKNOWN]`; this revision answers them and
corrects the Revision 1 statements that the live SQL disproved (§20).

**Phase:** discovery only. Nothing in OMS, JSAP or SQL Server was changed.
**Decision recorded 2026-09-24:** OMS Budget will be **built new, from scratch.
No legacy data will be migrated.** This audit therefore describes how the legacy
module *works* and where it *fails*, so the OMS design can keep the behaviour the
business relies on and drop the defects. §19 is the design input.

**Companion files:**
[`BUDGET_LEGACY_OBJECT_MATRIX.md`](BUDGET_LEGACY_OBJECT_MATRIX.md) — per-object
reference · [`BUDGET_BUSINESS_RULES.md`](BUDGET_BUSINESS_RULES.md) — confirmed rules
only · `JSAPNEW/docs/procedures/live/` — the verbatim live definitions (one file per
object) and `_live_checks_2026-09-24.md` (results of the read-only data checks).

---

## 0. Sources and evidence labels

| Source | Reached | Where |
|---|---|---|
| Live SQL definitions (`sys.sql_modules`) | **Yes — 42 objects** | `JSAPNEW/docs/procedures/live/*.sql` |
| SQL Agent jobs (`msdb`) | **Yes — full list** | `_live_checks_2026-09-24.md` |
| Linked servers (`sys.servers`) | Yes | same |
| Live data (read-only counts) | Yes | same |
| JSAP C# (`JSAPNEW`, commit `f411b5b`) | Yes | Revision 1 findings retained |
| Stored template queries (`jsQuery`) | Partly — templates 12–349 of company 1 | the paste was truncated; Beverage templates not seen |
| SAP HANA side (`DRAFT_APPROVAL`, what SAP does with `tbl_Draft_Approvals`) | **No** | outside SQL Server |

The grid copy collapsed line breaks, so the saved definitions are one line each —
readable, but not runnable as saved. Nothing depends on formatting.

| Label | Meaning |
|---|---|
| **[SQL]** | A live definition, read in full. File named where useful. |
| **[C#]** | JSAP source. |
| **[DATA]** | A read-only live query result (2026-09-24). |
| **[INFERRED]** | Reasoning shown; not directly stated by code or data. |
| **[UNKNOWN]** | Not answerable from SQL Server. |

---

## 1. Executive summary

1. **Budget approval is fully automatic up to the approver.** Nobody "submits" a
   budget document. Every 5 minutes a SQL Agent job copies SAP draft lines from
   HANA, runs **SQL text stored per approval template** against them, and creates
   a pending budget document for every (SAP document, template) that matches.
   [SQL `InsertBudgetFromHANA`, `jsProcessAllUsersBudgetApprovals`,
   `jsGetQueries`, `jsExecuteBudgetQueries`]

2. **Routing is data, not code — and it is SQL.** Each template carries a
   `SELECT * FROM bud.jsBudgetTable WHERE …` filter on company, budget,
   sub-budget, document type (journal voucher vs other), account-code lists,
   a load-date cut-off and sometimes hard-coded document numbers. Routing is
   changed by cloning templates, adding a new cut-off date and deactivating the
   old ones. [SQL `jsQuery` rows]

3. **The approval engine is sound in outline**: ordered stages per template,
   *N* approvals required per stage, a transaction per action, and the SAP
   write-back inside that transaction with a read-back verification. [SQL
   `jsApproveBudget`, `jsSyncBudgetToHanaDraftApproval`]

4. **JSAP writes the decision into SAP.** Final approval writes `A`, each
   intermediate stage `V`, a rejection `R` — into HANA table
   `"<company schema>"."tbl_Draft_Approvals"`, one row per line. If HANA does not
   confirm every line, the approval is **rolled back**. [SQL]

5. **Auto-approval is real and busy.** A job every 10 minutes approves any pending
   document idle ≥ 48 hours **if** it fits the month's allocation, recording the
   approval under the real stage approvers' user ids. 1,920 documents have been
   finally approved this way. [SQL `jsAutoApproveBudgetAfter48Hours`, DATA]

6. **The routing and ingestion layers are where it breaks** (§16): the same SAP
   line can land in two approval documents; lines added to a SAP draft after
   routing are never approved; lines matching no template are silently ignored;
   journal-voucher lines collide on their line key; empty documents were
   created and some were approved.

7. **Allocation change approval is a separate workflow** (`bud.jsFlow`,
   approval type 15). Final approval **adds** the requested amount to the
   allocation; the detail screen treats it as a **replacement**. Requests of
   ≤ 3,00,000 get no workflow and can never be approved. The "update allocation"
   procedure the API calls **does not exist**. [SQL]

8. **Reporting is inconsistent and partly broken**: three allocation sources with
   a fallback chain, per-user "approved" totals, a Beverage filter that can never
   match, budgets parsed out of SQL text. [SQL]

9. **No application-level authentication** on the budget endpoints; the acting
   `userId` is taken from the request body. The SQL does check the user against
   the stage, but trusts whatever id it is given. [C#][SQL]

---

## 2. Architecture — the whole pipeline

```text
SAP HANA (JIVO_OIL_HANADB, JIVO_BEVERAGES_HANADB, …)
  │  CALL "JIVO_OIL_HANADB"."DRAFT_APPROVAL"   ← returns OIL *and* BEVERAGE lines [DATA]
  │  CALL "JIVO_OIL_HANADB"."DRAFT_APPROVAL_ATC1" (attachments)
  ▼  linked server HANADB112
┌──────────────────────── SQL Server jsaplive3 ─────────────────────────────┐
│ every 5 min  "Budget Sync Data"                                            │
│   1 InsertBudgetFromHANA ─────► bud.jsBudgetTable   (append new lines)     │
│   2 jsProcessAllUsersBudgetApprovals 1  ┐ for every user of the company:   │
│   3 jsProcessAllUsersBudgetApprovals 2  ┘  dbo.jsGetQueries → template SQL │
│                                           jsExecuteBudgetQueries           │
│                                             ─► bud.jsDocEntry (header, 'P')│
│                                             ─► bud.jsDocEnrtyDetail (lines)│
│                                             ─► HANA "jsDocEntryDetail"     │
│ every 2 min  "Sync attchments"  jsSyncAttachmentsToHana ─► bud.SAPAttachments│
│ every 10 min "48 hrs"           jsAutoApproveBudgetAfter48Hours 75         │
│ every 60 min "Sync Workflow To HANA" jsSyncBudgetApprovalWorkflow          │
│                                  ─► HANA "js_budget_approval_workflow"     │
│                                                                            │
│ on user action (JSAP API):                                                 │
│   jsApproveBudget / jsRejectBudget ─► bud.jsBudgetStatusWorkflow, jsDocEntry│
│        └─ jsSyncBudgetToHanaDraftApproval ─► HANA "tbl_Draft_Approvals"    │
│   allocation: CreateBudgetAllocationRequest → jsFlow → jsApprove/Reject…   │
└────────────────────────────────────────────────────────────────────────────┘
  ▲
  │ ASP.NET Core JSAP API (AuthController, Auth2Controller, Reports, Dashboard)
  │ JSAP mobile app / web pages
```

Configuration lives in `dbo` and is shared with other JSAP approval modules:
`jsTemplate`, `jsTemplateApproval`, `jsTemplateQuery`, `jsQuery`, `jsStage`,
`jsStageTemplate`, `jsUserStage`, `jsApprovalCount`, `jsRejectionCount`,
`jsConfiguration`. [SQL]

---

## 3. Configuration model

```text
jsApproval (id 5 = Budget Approval, id 15 = Budget Allocation)
   ▲
jsTemplateApproval (templateId, approvalId)
   │
jsTemplate (id, company, isActive) ──► jsTemplateQuery ──► jsQuery (id, query, type)
   │                                                        └─ the routing SQL
jsStageTemplate (templateId, stageId, priority)   ← ordered stages 1..N
   │
jsStage (id, stage name, company, approvalId → jsApprovalCount.approval,
         rejectid → jsRejectionCount.rejection)
   │
jsUserStage (userId, stageId, startTime, endDate, status)  ← who approves
```

* **Stage requirement.** A stage needs `jsApprovalCount.approval` approvals
  (joined through `jsStage.approvalId`). [SQL]
* **Delegation / validity.** `jsUserStage` rows with both dates NULL are always
  valid; rows with dates are valid only when `status = 1` and today is inside
  the window. **Only some procedures apply this** (§16 D-12). [SQL]
* **A template belongs to one company** (`jsTemplate.company`) and is switched
  with `isActive`. [SQL]

### Routing queries — what they filter on [SQL, `jsQuery` rows]

Every budget template query has the shape

```sql
SELECT * FROM bud.jsBudgetTable
WHERE BRANCH = 'OIL' AND ProcesStat = 'Y'
  AND BUDGET IN (...)                 -- budget head(s)
  [AND SUB_BUDGET IN (...)]           -- sub-budget(s)
  AND ObjType = 28 | ObjType != 28    -- journal voucher vs everything else
  [AND AcctCode IN | NOT IN (5630001 … 5630016[, 5680011])]
  [AND AcctCode IN (5680011)]
  [AND budgetDate > '<cut-off>']      -- only lines loaded after this date
  [AND DocEntry NOT IN (2210,2212,2215)] / [AND DocEntry IN (28264)]
```

So the routing dimensions are: **company · budget · sub-budget · journal
voucher or not · account-code class · load date**, plus hand-coded document
exceptions. The meaning of the account ranges (5630001–5630016, 5680011) is not
stated anywhere. [UNKNOWN] The currently active company-1 set uses cut-off
`2025-11-29`; older sets (cut-offs 2025-05-12, 2025-11-07, 2025-11-14,
2026-01-15) are inactive. [SQL]

Nothing checks that two active templates do not match the same line (§16 D-1).

---

## 4. Ingestion — SAP lines into `bud.jsBudgetTable`

**`[bud].[InsertBudgetFromHANA]`** (modified 2026-08-31) [SQL]

* Calls `"JIVO_OIL_HANADB"."DRAFT_APPROVAL"` over `HANADB112` into a temp table.
  The procedure's comment says only Oil has that HANA procedure; the data shows
  it returns **Beverage lines as well** (Branch is a result column). [SQL][DATA]
* Inserts only lines whose `(Branch, DocEntry, ObjType, LineNum, VisOrder)` is
  not already in the table; stamps `flag = 'A'`, `budgetDate = GETDATE()`.
* **Never updates or deletes.** A line changed in SAP after its first load keeps
  its first-loaded values forever. (The old `jsGetBudgetSap`, which did update,
  is no longer called — §17.)
* Errors from HANA are logged to `bud.jsSyncErrors`, not raised.

Line identity is `DocEntry + ObjType + Branch + LineNum + VisOrder`. For journal
vouchers (ObjType 28) that key is **not unique** — §16 D-4.

`Current_month_Budget` and `Current_month_Posted_Amount` on each line are
computed by SAP inside `DRAFT_APPROVAL`. JSAP does not calculate them. [SQL]

---

## 5. Document creation — routing

**`[bud].[jsProcessAllUsersBudgetApprovals] @company`** (job steps for 1 and 2) [SQL]
for every user in `jsUserCompany` for that company:

1. `EXEC dbo.jsGetQueries @userId, @company, 5` — the user's currently valid
   stages (validity rule of §3) → templates using those stages that are active,
   in the company and of approval type 5 → their `jsQuery.query`.
2. `EXEC bud.jsExecuteBudgetQueries @inputTable` with those rows.
3. Two log rows per user per run into `bud.jsProcessLog`.

**`[bud].[jsExecuteBudgetQueries]`** (patched 2026-09-14) [SQL] for each template
query:

1. Runs it into `bud.TempResults` under a fresh `executionId`.
2. For each distinct `(DocEntry, Branch, ObjType, CURRENTMONTH)` with
   `flag = 'A' AND ProcesStat = 'Y'`:
   * **if no `bud.jsDocEntry` exists for `(docEntry, templateId)`**, in one
     transaction: insert the header (`status 'P'`, `currentStageId` = priority-1
     stage, `totalStage` = stage count, `currentSatge = 1`, `date = CURRENTMONTH`)
     and its lines (`DISTINCT objType, company, lineNum, visOrder`);
   * then mirrors each line to HANA `"<schema>"."jsDocEntryDetail"`.
3. Deletes its `TempResults` rows.

**Consequences, all [SQL]:**

* A budget document is **one (SAP document, template)** — one budget head's
  slice of a SAP document, as Revision 1 observed from outside.
* A template creates documents **only while at least one user validly holds one
  of its stages**; otherwise its query is never run.
* The same template query runs **once per user in it**, every 5 minutes
  (idempotent, redundant).
* The existence check is `docEntry + templateId` only → §16 D-2, D-3.
* Company 3 (Mart) is never processed by the job. [SQL job definition]

---

## 6. Approving — `[bud].[jsApproveBudget]`

Parameters: `@docId, @company, @userId, @remarks, @action` (`'Approve'` |
`'Revoke'`; C# never sends `@action`, so it is always Approve). One transaction;
errors go to `bud.jsErrorLog` and are re-raised. [SQL]

**Who may approve.**

* The user's stage in this template is **the lowest-priority stage they hold**
  (`TOP 1 … ORDER BY priority`). Not in any stage → error 50002.
* Allowed if they hold the **current** stage, **or** the **previous** stage while
  nobody has acted on the current stage yet → otherwise error 50006.
* `jsUserStage` validity dates are **not** checked here.

**What approving does.**

1. Already approved at the current stage → error 50020 (no double vote).
2. A previous-stage user who had approved and re-approves while the current
   stage is untouched → the document **moves back** to that user's stage (and a
   rejected document becomes pending again).
3. Records `A` in `bud.jsBudgetStatusWorkflow` (insert, or update of that user's
   row for their stage).
4. Counts `A` rows at the current stage against the stage requirement:
   * requirement met, **last stage** → `jsDocEntry.status = 'A'`, then
     `jsSyncBudgetToHanaDraftApproval … 'A'`;
   * requirement met, not last → advance `currentSatge`/`currentStageId`, then
     sync `'V'`;
   * not met → message *"Not enough approvals … Current: x, Required: y"*.
5. The HANA sync runs **inside** the transaction; if HANA does not confirm every
   line, the whole approval rolls back (§12).

**Revoke** (`@action = 'Revoke'`, unused by C#): a previous-stage user may withdraw
their approval while the current stage is untouched; the document returns to
their stage. [SQL]

Dead code: a `@status = 'Data is in SAP for DocEntry'` check can never be true —
`@status` is never assigned. [SQL]

**Application side** [C#]: `ApproveBudgetAsync` splits a comma-separated `docIds`
and calls the procedure once per id **without a transaction across ids**; after
the loop it notifies (§10). A failure part-way leaves earlier ids approved and
sends no notification.

---

## 7. Rejecting — `[bud].[jsRejectBudget]`

Same stage rules as approval (lowest stage held; current or untouched-previous).
[SQL]

1. Already rejected at the current stage → error 50021.
2. A previous-stage approver changing to reject → document moves back to their
   stage.
3. Records `R` for the user, **and immediately writes `R` to HANA for every line
   of the document** — on every rejection vote, before checking whether the
   stage's rejection requirement is met.
4. Counts `R` rows at the current stage against the **rejection requirement**,
   which the procedure reads from `jsRejectionCount` joined on
   **`jsStage.approvalId`** (not `jsStage.rejectid`, which the flow procedure
   uses) — §16 D-9.
   * met → `jsDocEntry.status = 'R'`, HANA `R` again;
   * not met → message *"Not enough rejections …"*, document stays pending in
     JSAP **while SAP already says rejected**.

**Cancel** (`@action = 'Cancel'`, unused by C#): a user may withdraw their own
rejection; a previous-stage user's cancel returns the document to their stage
and to `P`. [SQL]

A rejected document stays rejected: nothing re-opens it except a previous-stage
approver re-approving (§6 step 2). The trigger that used to archive and release
rejected documents for re-creation is dormant (§17). [SQL]

**Application side** [C#]: one document per call; no notification.

---

## 8. Auto-approval — `[bud].[jsAutoApproveBudgetAfter48Hours] @systemUserId = 75`

Job "48 hrs", every 10 minutes, enabled. [SQL, jobs]

For every `jsDocEntry` with `status = 'P'` and a current stage:

1. Company from the first detail line (`OIL`=1, `BEVERAGE`=2, else 3). No lines →
   log `SKIPPED`.
2. Hours idle = now − (latest action at the current stage, else `updatedOn`).
   Continue only if **≥ 48**.
3. **Budget check.** Month and budget head from the document's budget lines;
   document amount = sum of its lines for that month/budget; "used" = sum of
   **fully approved** documents of the same company/month/budget; allocation =
   `bud.BudgetMonthlyAllocations` for that budget name and month. If
   used + document > allocation (**no allocation row counts as 0**) → log
   `SKIPPED_LIMIT`. Unparseable month → `SKIPPED_BAD_MONTH`.
4. Otherwise inserts `A` rows **for real users of the current stage** — enough to
   reach the requirement, never users 68, 79, 95, never someone who already
   approved — with description *"Auto-approved after 48 hours"*.
5. Final stage → status `A` + HANA `A`; else advance stage + HANA `V`.
6. Logs every decision to `bud.jsAutoApprovalLog`; one error aborts the whole run
   (`THROW`).

Live effect [DATA]: 1,920 documents finally approved and 66 moved on by the job;
2,652 documents blocked by the limit at some point; 411 with unreadable months.
The log holds 7.1 million rows (skips are re-logged every 10 minutes).

User 75 is a named person's account (Gagandeep Singh), not a system account; the
excluded 68, 79, 95 are Nirmal Kaur, Gurpreet Singh, Arshdeep Singh. Why those
three are excluded is not stated. [DATA][UNKNOWN]

Weaknesses [SQL]: only one month/budget per document is checked (`TOP 1` without
order); the approver-validity dates are ignored; an auto-approval is
indistinguishable from a manual one except by its description text.

---

## 9. Reading a document

| Procedure | What it returns | Notes [SQL] |
|---|---|---|
| `jsGetPendingBudgets @userId, @company, @month` | pending documents at a stage the user **validly** holds, minus any the user already acted on at that stage; lines summed per document; ObjType 19 negated | reads `jsBudgetTable_Dedup`; month matched as text |
| `jsGetApprovedBudgets` / `jsGetRejectedBudgets` | documents **this user** approved / rejected at their own stages | validity dates **not** applied; auto-approvals appear as the user's own; rejected list has no `BUDGET` column |
| `jsGetNextApprover @budgetId` | users of the current stage who have not acted | called once per pending row by C# (N+1) |
| `jsGetBudgetApprovalFlow @budgetId` | every stage × every assigned user, with that user's action if any | ignores validity dates; `status = 1 OR NULL` only |
| `jsGetBudgetDetailById @budgetId` | header + lines joined to `bud.jsBudgetTable` | **not** the de-duplicating view → duplicated lines show |
| `jsGetBudgetAttachments @budgetId` | `bud.SAPAttachments` rows with the same `DocEntry` | no company/ObjType filter → another document's files can appear |
| `jsGetBudgetInsightAll @company, @month` | per user: pending / approved / rejected counts | dashboard |
| `jsSearchBudgetsByCompany` | documents by exact `docEntry` or exact `cardName` | requires one of them (month-only search errors — the live error Revision 1 saw); only documents that already have an action are found |

---

## 10. Notifications

* After approval, C# calls `[bud].[jsBudgetNotify] @budgetId` and pushes to the ids
  in `userIdsToApprove`. [C#]
* The procedure decides "remaining users at this stage" vs "next stage's users" by
  comparing the number of actions with **`jsStage.approvalId`** — the *id* of the
  approval-count row, not the required count — and counts actions of any status.
  [SQL] → §16 D-10.
* No notification on rejection, allocation approval or allocation rejection. [C#]
* A daily stage-transition e-mail job exists but is **disabled**. [jobs]
* Push text promises auto-approval after 48 hours — which §8 does perform. [C#][SQL]

---

## 11. Budget master and monthly allocation

**`CreateBudgetWithSubBudgets`** [SQL] — one transaction: `bud.Budgets(company,
budgetName, description, totalAmount, isActive)` + `bud.SubBudgets(budgetId,
subBudgetName, description, isActive=1)`. No duplicate-name check.

**`CreateMonthlyAllocations`** [SQL] — month must be the 1st; budget must exist;
**upsert**: if a budget allocation for that month exists it is **overwritten**
(only when the amount is > 0); sub-budget rows are MERGEd (overwritten if present).
Sub-budgets must belong to the budget. **No rule** that sub-budgets sum to the
budget amount, or that monthly amounts respect `totalAmount`. Uniqueness is
enforced by `UQ_BudgetMonthlyAllocations_BudgetMonth` and
`UQ_SubBudgetMonthlyAllocations_SubBudgetMonth`. So "create" is also the direct,
unapproved way to change an allocation.

**`UpdateMonthlyAllocations`** — **does not exist** in `jsaplive3`; the endpoint
always fails. [SQL lookup]

### Allocation change request (approval type 15)

**`CreateBudgetAllocationRequest @budgetAllocationId, @newAmount, @createdBy`** [SQL]

* Validates allocation exists, user exists, amount > 0.
* Inserts `bud.BudgetAllocationRequest`.
* **Only if `newAmount > 300000`**: finds the first type-15 template of the
  budget's company and inserts `bud.jsFlow` (status `P`, stage 1). Otherwise the
  request is created with **no workflow** and the message says it is *"NOT
  approvable"*.

**`jsApproveBudgetAllocationRequest @flowId, @company, @userId, @remarks, @action`**
[SQL] — same stage/authorization pattern as §6 over `bud.jsFlowStatus`, plus a
company check. Final approval:

```sql
UPDATE bma SET allocatedAmount = bma.allocatedAmount + bar.newAmount
```

i.e. **the requested amount is added**. Its `'Reject'` branch rejects the whole
flow on a single vote.

**`jsRejectBudgetAllocationRequest`** [SQL] — rejection with a rejection count
(same `approvalId` join as §7), and `Cancel`. The allocation is not touched on
rejection.

**`jsGetBudgetAllocationRequestDetail`** [SQL] shows `requestedAmount = newAmount`
and `amountDifference = newAmount − allocatedAmount` — i.e. it presents
`newAmount` as the **new total**, contradicting the approval's **add** (§16 D-13).

---

## 12. SAP / HANA integration

| Direction | Object | What | [SQL] |
|---|---|---|---|
| read | `InsertBudgetFromHANA` | `CALL "JIVO_OIL_HANADB"."DRAFT_APPROVAL"` → `bud.jsBudgetTable` | every 5 min |
| read | `jsSyncAttachmentsToHana` *(misnamed)* | `CALL "JIVO_OIL_HANADB"."DRAFT_APPROVAL_ATC1"` → `bud.SAPAttachments` (delete-all + reload, no transaction) | every 2 min |
| write | `jsExecuteBudgetQueries` | insert `"<schema>"."jsDocEntryDetail"` per routed line | on creation |
| **write** | **`jsSyncBudgetToHanaDraftApproval`** | insert-if-missing + `UPDATE "tbl_Draft_Approvals" SET "ApprovedStatus", "ACOMMENT"` per line, in one HANA `DO BEGIN … END`, then **read back and verify**; mismatch → `THROW` | on approve/reject/auto |
| write | `jsSyncBudgetApprovalWorkflow` | upsert `"<schema>"."js_budget_approval_workflow"` for every document line × current approver | every 60 min |

Details of the decision write [SQL]:

* Schema from `jsConfiguration` via `jsGetHanaConfiguration` (1 OIL, 2 BEVERAGE,
  3 MART). Linked server is hard-coded `HANADB112`.
* **Two row sources.** If every line is ObjType 13/14/18/19 ("marketing"), the
  inserted row is built from the SAP **draft** tables `ODRF`/`DRF1`; otherwise
  from `bud.jsBudgetTable`.
* Status values: `A` final approval, `V` intermediate stage, `R` rejection.
* Every attempt is logged to `bud.jsSyncErrors` (success included).
* No detail lines → logs and returns **without error** (so an empty document can
  be "approved" with nothing written to SAP — which happened, §16 D-5).

What SAP does with `tbl_Draft_Approvals.ApprovedStatus` — whether a draft cannot
be posted until it is `A` — is on the SAP side. [UNKNOWN]

Linked server `HANA112` (used by five older objects) **does not exist**; only
`HANADB112` does. [DATA]

---

## 13. Scheduled jobs (budget)

| Job | Enabled | Runs | Every |
|---|---|---|---|
| Budget Sync Data | yes | `InsertBudgetFromHANA`; `jsProcessAllUsersBudgetApprovals 1`; `… 2` | 5 min |
| 48 hrs | yes | `jsAutoApproveBudgetAfter48Hours 75` | 10 min |
| Sync attchments | yes | `jsSyncAttachmentsToHana` | 2 min |
| Sync Workflow To HANA | yes | `jsSyncBudgetApprovalWorkflow` | 60 min |
| 24 hrs | no | auto-approve in database `jsap` | — |
| Budget Stage Transitions Email Job | no | `jsBudgetStageTransitionsmail` | 11:00, 16:00 |

`bud.jsBudgetTable_vg` (a 208,441-row **table**, not a view) is loaded at hh:04
every 4 hours by something that is **not** a SQL Agent job and not any module in
`jsaplive3`. [DATA][UNKNOWN] Only the dashboard reads it.

---

## 14. Status model

| Where | Values |
|---|---|
| `bud.jsDocEntry.status` | `P` pending · `A` approved · `R` rejected |
| `bud.jsBudgetStatusWorkflow.status` | `A` · `R` · `Revoked` · `Cancelled` (one row per user per stage, updated in place) |
| `bud.jsFlow.status` / `bud.jsFlowStatus.status` | same, for allocation requests |
| HANA `tbl_Draft_Approvals.ApprovedStatus` | `A` final · `V` intermediate stage passed · `R` rejected (on first rejection vote) |
| `bud.jsBudgetTable.flag` | `A` loaded; `I` (set only by the dead `jsGetBudgetSap`); `D` in the V2 archive |

History is **overwritten**: re-approving or changing a vote updates the user's row
(status, description, `createdOn`), so earlier decisions are lost except for the
`| Revoked:` / `| Cancelled:` text appended to the description. [SQL]

---

## 15. Security

* **No `[Authorize]`** on any budget controller; JWT is registered but not
  required; the acting `userId` comes from the body/query. [C#]
* The SQL checks that `@userId` belongs to the current (or untouched previous)
  stage — so the rule is enforced, but for **whatever user id the caller
  claims**. [SQL]
* Allocation approve/reject additionally check the company. Document
  approve/reject accept `@company` but use it only for the HANA schema. [SQL]
* Dynamic SQL to HANA escapes remarks and names by quote-doubling in the decision
  sync; the hourly workflow sync does **not** escape user and stage names (an
  apostrophe breaks the run). [SQL]

---

## 16. Defects found — evidence only

| # | Defect | Evidence | Effect |
|---|---|---|---|
| D-1 | **Overlapping routing.** Two active templates can match the same line; nothing prevents it. | [SQL][DATA] several hundred lines sit in two (a few in three) approval documents — mostly templates 421 + 422 (Oil invoices 36826–49737) | the same expense is approved twice, possibly by different chains; SAP status = whichever sync ran last; auto-approval's "used" double-counts |
| D-2 | **Late lines are never routed.** Creation skips when a `(docEntry, templateId)` document exists, so lines added to the SAP draft later are ignored. | [SQL][DATA] SAP 42632: document created 2026-03-25; six more matching lines loaded 2026-04-16, never added | expenses reach SAP approval status via other lines' approval, or never |
| D-3 | **Different SAP documents sharing a number collide** — the existence check has no ObjType or company. | [SQL] | second document of the same number under the same template is skipped |
| D-4 | **Journal-voucher line key is not unique.** Several different lines share `DocEntry+ObjType+Branch+LineNum+VisOrder`. | [DATA] 35 colliding keys, all ObjType 28 Oil; JV 5483 line 0 = four different budgets/accounts | detail insert failed (D-5); the de-dup view keeps one of the colliding lines. **Cause proven 2026-09-24** (see `BUDGET_OMS_RELEVANT_AUDIT.md` §13 B-2): `DRAFT_APPROVAL` omits `BTF1.TransId` and joins `OBTF` on `BatchNum` only, so vouchers of one batch collide and each line is repeated once per voucher header |
| D-5 | **Headers without lines.** Before the 2026-09-14 patch a failed line insert left the header committed. | [DATA] 18 empty documents: 14 approved, 4 still pending since 2026-04-30 | pending ones are invisible (lists join lines) and re-logged `SKIPPED` every 10 min; approved ones wrote nothing to SAP; their real lines can never route |
| D-6 | **Unrouted lines are silent.** A line matching no active template simply never becomes a document. | [DATA] e.g. OTE with sub-budget GT/CSD/MT on ObjType 14; Del Bkhp with account 5680011 | no approval, no alert |
| D-7 | **Ingestion is append-only.** Changed SAP lines are never refreshed. | [SQL] | approvers see first-loaded amounts |
| D-8 | **SAP says "rejected" before JSAP does.** Every rejection vote writes `R` to HANA; the document may stay `P` in JSAP. | [SQL] | SAP and JSAP disagree for multi-rejection stages |
| D-9 | **Rejection threshold read through the wrong column** (`jsStage.approvalId` into `jsRejectionCount`), while the flow screen uses `jsStage.rejectid`. | [SQL] | the rejection requirement shown ≠ the one enforced |
| D-10 | **Notify compares actions with an id.** `jsBudgetNotify` uses `jsStage.approvalId` as the required count and counts actions of any status. | [SQL] | wrong recipients (current vs next stage) |
| D-11 | **A user holding two stages of one template** is always treated as the lower one; their vote at the higher stage is recorded against the lower stage and never counts. | [SQL] | such a user cannot complete the later stage |
| D-12 | **Approver validity applied inconsistently** — honoured by pending list and document creation, ignored by approve/reject, flow, approved/rejected lists and auto-approval. | [SQL] | an expired delegate can still act; a document can be created only while someone is valid |
| D-13 | **Allocation request amount: add vs replace.** Approval adds `newAmount`; the detail screen presents it as the new total. | [SQL] | a request to "set 5 L" on a 4 L allocation yields 9 L |
| D-14 | **Allocation requests ≤ 3,00,000 are unapprovable** (no flow is created). | [SQL] | dead requests |
| D-15 | **`UpdateMonthlyAllocations` missing**; `CreateMonthlyAllocations` silently overwrites instead. | [SQL] | unapproved allocation changes through "create" |
| D-16 | **Budget summary never matches Beverage** — it filters `company = 'BEVERAGES'`; lines say `BEVERAGE`. | [SQL] | Beverage approved/rejected/pending = 0 |
| D-17 | **"Approved amount" is per user**, not per budget: the summary sums documents *the caller* approved. | [SQL] | two approvers of a budget see different "approved" |
| D-18 | **Three allocation sources** with a fallback chain (allocation tables → SAP `Current_month_Budget` snapshot → legacy `jsBudgetCategoryMonthSummary`); the category dashboard reads only the legacy one. | [SQL] | different screens, different totals |
| D-19 | **Budgets parsed from SQL text** — `jsGetUserBudgetAllocation` extracts the first `BUDGET IN (…)` list from template queries. | [SQL] | misses `BUDGET = '…'` forms and sub-budget filters |
| D-20 | **Detail and summary read the raw, duplicated snapshot**, joined without ObjType/company in places. | [SQL] | duplicated lines and inflated sums |
| D-21 | **Attachments matched by DocEntry only.** | [SQL] | another document's files can be shown |
| D-22 | **Months as text** in three formats (`MM-YYYY`, `MM/YYYY`, `YYYY-MM`), parsed repeatedly. | [SQL][DATA] 411 documents unparseable by auto-approval | skipped documents |
| D-23 | **Unbounded logs** — `jsAutoApprovalLog` 7.1 M rows, `jsProcessLog` 2 rows/user/5 min. | [DATA][SQL] | growth only |
| D-24 | **Hourly workflow sync resends everything** (tracking table truncated and all rows marked modified), keeps one approver per line key, unescaped names. | [SQL] | load; partial data in HANA |
| D-25 | **Single overwritten backup** of `jsaplive3` nightly. | [jobs] | operational, outside Budget |

---

## 17. Dormant and dead objects

| Object | State | Why it matters |
|---|---|---|
| `bud.trg_BudgetTable_UpdateDate_Change` | fires only on an `UPDATE` of `UpdateDate`; nothing performs one | would archive a **rejected** document to `…V2` tables and delete it so it can be re-created — the only "resubmission" mechanism, now inactive |
| `bud.jsGetBudgetSap` | not called; uses missing `HANA112`; its `INSERT … SELECT *` supplies 38 of 39 columns [INFERRED] | the old loader that updated changed lines |
| `dbo.usp_Cleanup_Obsolete_Budget_DocEntries` | not called; missing `HANA112` | **deletes every budget document** if its SAP reads all fail — must never be run |
| `bud.usp_Cleanup_Obsolete_Budget_DocEntries1` | not called | still **deletes** ObjType 28 snapshot rows |
| `dbo.usp_Cleanup_Obsolete_Budget_DocEntries1` | not called | dry run only |
| `bud.jsCompareDocEntryWithDraftApproval` | not called; missing `HANA112` | read-only diagnostic |
| `dbo.jsFetchSap` | caller unknown | refreshes cost-centre masters (`jsVariety`, `jsBudget`, `jsSubBudget`, …) from `OPRC`; `jsBranch` gets Oil only (Beverage/Mart inserts built but never executed); no transaction |
| `bud.UpdateMonthlyAllocations` | **missing** | endpoint always fails |
| Job "24 hrs" | disabled | targets database `jsap` |

---

## 18. Data migration

**Not applicable.** Decision 2026-09-24: OMS Budget starts empty; no legacy
budget, allocation, document, approval history or attachment is migrated.
The legacy module and the OMS module will not share state. If both run for a
period, the only shared resource is SAP's `tbl_Draft_Approvals` — one system
must own each SAP document's decision (see §19.9).

---

## 19. OMS from scratch — design input

These are **proposals** derived from §4–§17, not decisions and not legacy rules.
They follow the OMS precedents already in the codebase (Workflow Engine;
BKDT's company-wise SAP write at final approval; one request per approval unit).

### 19.1 Keep — behaviour the business relies on

* A budget document is **one budget head's slice of one SAP document**, with its
  own approval chain (§5).
* Ordered stages with **N approvals required per stage** (§3, §6).
* Only an approver of the current stage acts; an approval and the SAP write
  commit or fail **together**, with read-back verification (§6, §12).
* SAP learns the decision per line: `A` final, `V` stage passed, `R` rejected
  (§12).
* Credit memos (ObjType 19) count negative (§9).
* Approvers see: pending for them, what they approved, what they rejected —
  **own-action semantics** (§9), as already built for BKDT.
* Monthly allocations per budget and per sub-budget, one per month (§11).
* Allocation changes above a threshold go through their own approval (§11).

### 19.2 Ingestion

* Read SAP draft lines on a schedule into an OMS staging table with an
  **upsert** keyed on a **true line identity**. For journal vouchers the legacy key
  is not unique (D-4); the key must include whatever distinguishes the lines in
  SAP (for example the journal-entry id inside the batch) — to be confirmed with
  the SAP team before design (§21 Q-7).
* Detect changes (SAP `UpdateDate`) and mark affected documents: a changed line
  on a **pending** document refreshes it; on an **approved** document it raises a
  re-approval or an exception, not a silent mismatch (D-7).
* Store months as `DATE` (first of month), amounts as decimals (D-22).

### 19.3 Routing — replace SQL text with structured rules

* A routing rule = company · budget head · sub-budget set · document class
  (journal voucher / other) · account include/exclude set · effective from/to ·
  target workflow. Evaluated in code, not by executing stored SQL.
* **Every line resolves to exactly one rule.** Saving a rule that overlaps an
  active rule is refused (D-1). A line matching none goes to an **unrouted queue**
  with an alert (D-6).
* Versioning by **effective dates** on the rule instead of cloning templates and
  load-date cut-offs; hard-coded document exceptions become explicit, audited
  overrides.
* Routing does **not** depend on whether an approver is currently valid (§5).

### 19.4 Document assembly

* One document per (company, SAP ObjType, SAP DocEntry, route) — the existence key
  includes ObjType and company (D-3).
* A line that arrives after the document exists **joins it** while it is pending;
  after a decision it starts a **supplementary** document (D-2).
* A document with zero lines is impossible by construction (D-5).

### 19.5 Workflow

* Use the OMS **Workflow Engine** for both budget documents and allocation
  requests — one engine, one action log, append-only history (§14: legacy
  overwrites votes).
* Stage membership is per stage, so a user on two stages votes on each (D-11).
* Delegation validity is applied **everywhere** — lists, actions, notifications,
  auto-approval (D-12).
* Rejection policy is explicit per stage and read from one place (D-9); SAP is
  told `R` only when the document is actually rejected (D-8), unless the business
  wants the legacy "first vote blocks SAP" behaviour (Q-2).
* The acting user comes from the **JWT**, never the request body (§15).
* Bulk approve returns a per-document result; each document is its own
  transaction; bulk reject supported the same way.

### 19.6 Budget control and auto-approval

* One allocation source: OMS monthly allocations (budget and sub-budget). No
  fallback to SAP snapshots or a legacy summary (D-18).
* Define consumption once — e.g. *committed* = approved documents,
  *in-flight* = pending, *available* = allocation − committed — and show an
  overspend as negative, not zero (Revision 1 C#: `max(0, …)`) (Q-5).
* Budget figures are **per budget**, not per viewing user (D-17), and filter the
  company by id, not a name string (D-16).
* Auto-approval, if kept (Q-1): a per-workflow policy (off / hours / budget
  check), acting as a **system actor** recorded as such — not as real approvers or
  a named person's account — with an escalation notice before it fires.

### 19.7 Allocation changes

* Decide and name the semantics: **target amount** or **increase** (D-13, Q-3).
* A request below the threshold is either applied directly with an audit record
  or not creatable — never an unapprovable request (D-14, Q-4).
* Direct edits of an allocation and approved changes are distinct, permissioned
  actions (D-15).

### 19.8 Notifications and reporting

* Notify the next actors computed from the workflow state (D-10), and notify on
  rejection and on allocation decisions.
* Attachments keyed by company + ObjType + DocEntry (D-21).
* Reports come from one query layer over OMS tables; numeric types end to end.
* Logs with retention; no per-run success rows (D-23).

### 19.9 SAP ownership during coexistence

If legacy JSAP and OMS both run for a period, each SAP document's decision must be
written by **one** system only. The routing rules in OMS can be switched on per
company / budget head with an effective date, and the matching legacy templates
switched off (`isActive = 0`) on the same date — otherwise both systems write
`tbl_Draft_Approvals` for the same lines.

---

## 20. Corrections to Revision 1

| Revision 1 said | Live SQL shows |
|---|---|
| None of the 18 definitions readable; most internals `[UNKNOWN]` | all read (§0) |
| Whether approve/reject write to SAP: unknown | they do, inside the transaction, with verification (§6, §7, §12) |
| 48-hour auto-approval "not implemented" | implemented as a SQL Agent job, every 10 min (§8) |
| `InsertBudgetFromHANA` drops/re-creates `tbl_Draft_Approvals` without `ACOMMENT` — contradiction | that was the repo copy; the live version (2026-08-31) does not touch the HANA table (§4) |
| `InsertBudgetFromHANA` is Oil-only | it calls Oil's procedure, which returns Beverage lines too (§4) |
| TotalBudget fallback order: category summary → allocation tables → snapshot | allocation tables → SAP snapshot → category summary (§16 D-18) |
| `jsBudgetTable_vg` is a view with 30 columns | a **table** with 39 columns, loaded outside SQL Agent (§13) |
| `Current_month_Budget` is `NVARCHAR(50)` in `bud.jsBudgetTable` | `NVARCHAR(50)` is the loader's **temp table**; the stored type in `bud.jsBudgetTable` was not queried (in `bud.jsBudgetTable_vg` it is `decimal(18,2)`) |
| `UpdateMonthlyAllocations` changes an allocation directly [INFERRED] | the procedure does not exist (§11) |
| Allocation final approval effect unknown | adds `newAmount` to the allocation (§11) |
| Who may approve unknown | current-stage member, or untouched previous stage; lowest stage held (§6) |

---

## 21. Open questions for the business

Only questions the SQL cannot answer and the OMS design needs.

1. **Q-1** Keep 48-hour auto-approval? If so, with the budget check, and who is
   shown as the approver?
2. **Q-2** Should SAP be told "rejected" on the **first** rejection vote (legacy) or
   only when the stage's rejection requirement is met?
3. **Q-3** Is an allocation change request a **new total** or an **increase**?
4. **Q-4** What should happen to allocation changes of ≤ 3,00,000 — apply directly,
   or no request needed?
5. **Q-5** Definition of *used/available*: count pending? block or warn when a
   document would exceed the allocation? show overspend?
6. **Q-6** What does SAP do with `tbl_Draft_Approvals.ApprovedStatus` — does a draft
   stay unpostable until `A`? What does `V` do on the SAP side?
7. **Q-7** For journal vouchers, what uniquely identifies a line in
   `DRAFT_APPROVAL` output (35 colliding keys)?
8. **Q-8** Should Mart (company 3) have budget control? (The job never processes it.)
9. **Q-9** Why are users 68, 79 and 95 excluded from auto-approval, and should an
   equivalent rule exist in OMS?
10. **Q-10** A document rejected in JSAP — can it come back (legacy mechanism is
    dormant, §17)? If the SAP draft is edited, is that a new approval?

---

## Appendix — live definition files

`JSAPNEW/docs/procedures/live/`: the 18 audited objects and every object they
depend on, one file each, plus `jsBudgetTable_vg.sql` (table note),
`UpdateMonthlyAllocations.sql` (does-not-exist note) and
`_live_checks_2026-09-24.md` (all read-only data checks).
