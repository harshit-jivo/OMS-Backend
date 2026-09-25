# BUDGET — Legacy Object Matrix

**Revision 2 — 2026-09-24**, from the live definitions in `jsaplive3`
(`JSAPNEW/docs/procedures/live/`, one file per object). Reasoning:
[`BUDGET_LEGACY_AUDIT.md`](BUDGET_LEGACY_AUDIT.md).

---

## 1. The 18 audited objects

| # | Object | Kind | Called by | Reads | Writes | Tx | SAP |
|---|---|---|---|---|---|---|---|
| 1 | `bud.jsApproveBudget` | proc | `POST /api/auth/approvebudget` (C# loops ids) | `jsDocEntry`, `jsStageTemplate`, `jsUserStage`, `jsStage`, `jsApprovalCount`, `jsBudgetStatusWorkflow` | `jsBudgetStatusWorkflow`, `jsDocEntry`, `jsErrorLog` | yes | writes `A`/`V` via #22 |
| 2 | `bud.jsRejectBudget` | proc | `POST /api/auth/rejectebudget` | as #1 + `jsRejectionCount` | `jsBudgetStatusWorkflow`, `jsDocEntry` | yes | writes `R` via #22 on every vote |
| 3 | `bud.jsGetNextApprover` | proc | pending list, once per row | `jsDocEntry`, `jsUserStage`, `jsUser`, `jsBudgetStatusWorkflow` | — | — | — |
| 4 | `bud.jsGetBudgetApprovalFlow` | proc | `GET /api/auth/GetBudgetApprovalFlow` | `jsStageTemplate`, `jsUserStage`, `jsStage`, `jsUser`, `jsBudgetStatusWorkflow`, `jsApprovalCount`, `jsRejectionCount` | — | — | — |
| 5 | `bud.jsGetBudgetDetailById` | proc | `GET /api/auth/GetBudgetDetailByIdv2` | `jsDocEntry`, `jsDocEnrtyDetail`, `jsBudgetTable` | — | — | — |
| 6 | `bud.CreateBudgetWithSubBudgets` | proc | `POST /api/auth2/CreateBudgetWithSubBudgets` | — | `Budgets`, `SubBudgets` | yes | — |
| 7 | `bud.CreateMonthlyAllocations` | proc | `POST /api/auth2/CreateMonthlyAllocations` | `Budgets`, `SubBudgets` | `BudgetMonthlyAllocations` (upsert), `SubBudgetMonthlyAllocations` (MERGE) | yes | — |
| 8 | `bud.UpdateMonthlyAllocations` | **missing** | `POST /api/auth2/UpdateMonthlyAllocations` — always fails | — | — | — | — |
| 9 | `bud.CreateBudgetAllocationRequest` | proc | `POST /api/auth2/createBudgetAllocation` | `BudgetMonthlyAllocations`, `Budgets`, `jsUser`, `jsTemplate`, `jsTemplateApproval`, `jsStageTemplate` | `BudgetAllocationRequest`; `jsFlow` only if amount > 3,00,000 | yes | — |
| 10 | `bud.jsApproveBudgetAllocationRequest` | proc | `POST /api/auth2/approveBudgetAllocation` | `jsFlow`, `BudgetAllocationRequest`, `BudgetMonthlyAllocations`, `Budgets`, stage tables, `jsFlowStatus` | `jsFlowStatus`, `jsFlow`; **final: `allocatedAmount += newAmount`** | yes | — |
| 11 | `bud.jsRejectBudgetAllocationRequest` | proc | `POST /api/auth2/rejectBudgetAllocation` | as #10 + `jsRejectionCount` | `jsFlowStatus`, `jsFlow` | yes | — |
| 12 | `bud.jsGetBudApprovalFlow` | proc | `GET /api/auth2/GetBudgetAllocationFlow` | `jsFlow`, stage tables, `jsFlowStatus` | — | — | — |
| 13 | `bud.jsGetBudgetSummary` | proc | `GET /api/auth/GetBudgetSummary` (+ C# math) | `Budgets`, `SubBudgets`, `(Sub)BudgetMonthlyAllocations`, `jsBudgetTable`, `jsBudgetCategoryMonthSummary`, `jsBudgetStatusWorkflow`, `jsDocEntry`, `jsDocEnrtyDetail` | — | — | — |
| 14 | `bud.jsGetBudgetCategorySummaryDashboard` | proc | `GET /api/auth/GetBudgetCategorySummaryDashboard` | `jsBudgetCategoryMonthSummary` | — | — | — |
| 15 | `bud.jsGetCategoryMonthlyBudget` | proc | `GET /api/auth/GetCategoryMonthlyBudget` | `jsBudgetCategoryMonthSummary` | — | — | — |
| 16 | `bud.jsGetUserBudgetAllocation` | proc | `GET /api/auth/getbudgetallocation` | stage/template tables, **`jsQuery` text**, `Budgets`, `SubBudgets`, `SubBudgetMonthlyAllocations` | — | — | — |
| 17 | `bud.jsBudgetTable_vg` | **table** (not a view), 39 cols, 208,441 rows | `DashboardService` inline SQL | — | loaded hh:04 every 4 h by an unknown process outside SQL Agent | — | — |
| 18 | `bud.jsSearchBudgetsByCompany` | proc | `GET /api/Reports/GetBudgetByCompany` | `jsBudgetStatusWorkflow`, `jsDocEnrtyDetail`, `jsDocEntry`, `jsBudgetTable` | — | — | — |

## 2. Parameters (live signatures)

| # | Parameters (defaults as in the definition) |
|---|---|
| 1 | `@docId INT`, `@company INT`, `@userId INT`, `@remarks NVARCHAR(MAX)`, `@action NVARCHAR(20) = 'Approve'` (`Approve`/`Revoke`) |
| 2 | `@docId INT`, `@company INT`, `@userId INT`, `@remarks NVARCHAR(MAX)`, `@action NVARCHAR(20) = 'Reject'` (`Reject`/`Cancel`) |
| 3, 4, 5 | `@budgetId INT` |
| 6 | `@company INT`, `@budgetName NVARCHAR(255)`, `@description NVARCHAR(MAX) = NULL`, `@totalAmount DECIMAL(15,2) = 0`, `@isActive BIT = 1`, `@subBudgets bud.SubBudgetTableType`, `@newBudgetId INT OUTPUT` |
| 7 | `@budgetId INT`, `@allocationMonth DATE` (1st of month), `@budgetAllocatedAmount DECIMAL(15,2) = 0`, `@budgetNotes NVARCHAR(MAX) = NULL`, `@subBudgetAllocations bud.MonthlyAllocationTableType` |
| 9 | `@budgetAllocationId INT`, `@newAmount DECIMAL(18,2)`, `@createdBy INT`, `@newRequestId INT OUTPUT`, `@message NVARCHAR(500) OUTPUT`; RETURN 0 / −1 |
| 10 | `@flowId INT`, `@company INT`, `@userId INT`, `@remarks NVARCHAR(MAX)`, `@action NVARCHAR(20) = 'Approve'` (`Approve`/`Reject`) |
| 11 | `@flowId INT`, `@company INT`, `@userId INT`, `@remarks NVARCHAR(MAX)`, `@action NVARCHAR(20) = 'Reject'` (`Reject`/`Cancel`) |
| 12 | `@flowId BIGINT` |
| 13 | `@userId INT`, `@budgetCategory VARCHAR(100)`, `@subBudget VARCHAR(124)`, `@month VARCHAR(20)`, `@company INT` |
| 14 | `@month VARCHAR(20)`, `@company INT` |
| 15 | `@budgetCategory VARCHAR(100)`, `@subBudget VARCHAR(124)`, `@month VARCHAR(20)`, `@company INT` |
| 16 | `@userId INT`, `@company INT`, `@month VARCHAR(7)` (`MM-YYYY`) |
| 18 | `@company INT`, `@docEntry INT = NULL`, `@cardName VARCHAR(100)`, `@month VARCHAR(MAX) = NULL`, `@status VARCHAR(10) = NULL`; errors unless docEntry or cardName given |

## 3. Supporting objects (outside the 18)

| Object | Kind | Role | Runs |
|---|---|---|---|
| `bud.InsertBudgetFromHANA` | proc | SAP draft lines → `jsBudgetTable` (append only) | job, 5 min |
| `bud.jsProcessAllUsersBudgetApprovals` | proc | per user: `jsGetQueries` → `jsExecuteBudgetQueries` | job, 5 min, companies 1 and 2 |
| `dbo.jsGetQueries` | proc | user's valid stages → active type-5 template queries | from above |
| `bud.jsExecuteBudgetQueries` | proc | runs template SQL; creates `jsDocEntry` + `jsDocEnrtyDetail`; mirrors lines to HANA | from above |
| `bud.jsAutoApproveBudgetAfter48Hours` | proc | 48-hour auto-approval with allocation check | job, 10 min |
| `bud.jsSyncBudgetToHanaDraftApproval` | proc | decision → HANA `tbl_Draft_Approvals`, verified | from #1, #2, auto-approval |
| `bud.jsGetHanaConfiguration` | proc | company → HANA schema from `jsConfiguration` | from sync procs |
| `bud.jsSyncBudgetApprovalWorkflow` | proc | approval state → HANA `js_budget_approval_workflow` | job, 60 min |
| `bud.jsSyncAttachmentsToHana` | proc | HANA attachments → `bud.SAPAttachments` (reads, despite name) | job, 2 min |
| `bud.jsBudgetNotify` | proc | recipients after approval | C# after #1 |
| `bud.jsGetBudgetAttachments` | proc | attachments by DocEntry | C# with #5 |
| `bud.jsGetBudgetAllocationRequestDetail` | proc | request header + history | C# |
| `bud.jsGetPendingBudgets` / `jsGetApprovedBudgets` / `jsGetRejectedBudgets` | procs | approver lists (read `jsBudgetTable_Dedup`) | C# |
| `bud.jsGetBudgetInsightAll` | proc | per-user counts | C# |
| `bud.jsBudgetTable_Dedup` | view | latest row per line key and month | the three list procs |
| `bud.trg_BudgetTable_UpdateDate_Change` | trigger | archive + delete rejected docs on SAP change | **dormant** |
| `bud.jsGetBudgetSap` | proc | old loader with updates | **not called** |
| `dbo.usp_Cleanup_Obsolete_Budget_DocEntries` (+ two `…1` copies) | procs | delete documents not found in SAP | **not called; destructive** |
| `bud.jsCompareDocEntryWithDraftApproval` | proc | diagnostic | **not called** |
| `dbo.jsFetchSap` | proc | cost-centre masters from `OPRC` | caller unknown |

## 4. Tables

| Table | Holds |
|---|---|
| `bud.jsBudgetTable` | SAP draft-line snapshot, 39 columns, append-only |
| `bud.jsDocEntry` | budget document header: `id, docEntry, status, currentStageId, templateId, totalStage, currentSatge, date, updatedOn, createdOn` |
| `bud.jsDocEnrtyDetail` | document lines: `objType, company, lineNum, visOrder, docId`; `UQ_jsDocEnrtyDetail_Unique` |
| `bud.jsBudgetStatusWorkflow` | one row per user per stage per document: `status, description, createdOn` (updated in place) |
| `bud.Budgets`, `bud.SubBudgets` | master |
| `bud.BudgetMonthlyAllocations`, `bud.SubBudgetMonthlyAllocations` | monthly amounts; unique per (budget, month) and (sub-budget, month) |
| `bud.BudgetAllocationRequest`, `bud.jsFlow`, `bud.jsFlowStatus` | allocation change workflow |
| `bud.jsBudgetCategoryMonthSummary` | legacy category totals (writer not in the audited set) |
| `bud.SAPAttachments` | attachment list, reloaded every 2 min |
| `bud.jsAutoApprovalLog`, `bud.jsProcessLog`, `bud.jsSyncErrors`, `bud.jsErrorLog` | logs |
| `bud.TempResults`, `bud.jsWorkflowSyncStatus` | working tables |
| `bud.jsBudgetTableV2`, `jsDocEntryV2`, `jsDocEnrtyDetailV2`, `jsBudgetStatusWorkflowV2` | archive written only by the dormant trigger |
| `dbo.jsTemplate`, `jsTemplateApproval`, `jsTemplateQuery`, `jsQuery`, `jsStage`, `jsStageTemplate`, `jsUserStage`, `jsApprovalCount`, `jsRejectionCount`, `jsConfiguration` | shared approval configuration |

## 5. Defects by object

Numbers refer to the audit's §16.

| Object | Defects |
|---|---|
| `jsExecuteBudgetQueries` / template queries | D-1, D-2, D-3, D-5 (pre-2026-09-14), D-6 |
| `InsertBudgetFromHANA` | D-4, D-7 |
| `jsApproveBudget` | D-11, D-12 |
| `jsRejectBudget` | D-8, D-9, D-11, D-12 |
| `jsBudgetNotify` | D-10 |
| `jsAutoApproveBudgetAfter48Hours` | D-12, D-22, D-23 |
| `CreateMonthlyAllocations` / `UpdateMonthlyAllocations` | D-15 |
| `CreateBudgetAllocationRequest` | D-14 |
| `jsApproveBudgetAllocationRequest` / `…RequestDetail` | D-13 |
| `jsGetBudgetSummary` | D-16, D-17, D-18, D-20 |
| `jsGetBudgetCategorySummaryDashboard`, `jsGetCategoryMonthlyBudget` | D-18 |
| `jsGetUserBudgetAllocation` | D-19 |
| `jsGetBudgetDetailById` | D-20 |
| `jsGetBudgetAttachments` | D-21 |
| `jsSyncBudgetApprovalWorkflow` | D-24 |
