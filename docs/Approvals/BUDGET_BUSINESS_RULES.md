# BUDGET — Confirmed Legacy Business Rules

**Revision 2 — 2026-09-24**, from the live SQL definitions in `jsaplive3`
(`JSAPNEW/docs/procedures/live/`) and the JSAP C#. Detail and reasoning:
[`BUDGET_LEGACY_AUDIT.md`](BUDGET_LEGACY_AUDIT.md).

This file lists **what the legacy system does**, with evidence. It does not say
what OMS should do — the audit's §19 does that, and §16 lists where the legacy
behaviour is a defect rather than a rule. A rule marked **(defect)** is
reproduced here because it is real behaviour, not because OMS should copy it.

Evidence: **SQL** live definition · **C#** JSAP source · **DATA** live read-only
check, 2026-09-24.

---

## 1. Where budget documents come from

**BR-SRC-1.** Budget documents are created automatically from SAP draft lines;
no user submits them. Every 5 minutes the lines returned by HANA procedure
`"JIVO_OIL_HANADB"."DRAFT_APPROVAL"` are appended to `bud.jsBudgetTable`, and
each active budget template's stored query is run against that table. *SQL*
`InsertBudgetFromHANA`, `jsProcessAllUsersBudgetApprovals`, `jsExecuteBudgetQueries`;
job "Budget Sync Data".

**BR-SRC-2.** `DRAFT_APPROVAL` returns lines for both Oil and Beverage. *DATA.*

**BR-SRC-3.** A line already loaded is never updated or removed. *SQL.* (defect D-7)

**BR-SRC-4.** Only lines with `ProcesStat = 'Y'` can become budget documents. *SQL*
(`jsExecuteBudgetQueries`, every template query).

**BR-SRC-5.** The budget, current-month budget and current-month posted amount of a
line are computed by SAP, not JSAP. *SQL.*

## 2. Routing

**BR-RTE-1.** A template's stored query decides which lines it takes. The queries
filter on company, budget, sub-budget, journal voucher (ObjType 28) or not,
account-code lists, a load-date cut-off, and sometimes specific document
numbers. *SQL* (`jsQuery`).

**BR-RTE-2.** A budget document is one (SAP document, template): a SAP document
whose lines match several templates becomes several budget documents, each with
its own approvers. *SQL*; *DATA* (e.g. DocEntry 57334 → three documents).

**BR-RTE-3.** A template's query runs only while at least one user validly holds
one of its stages (no validity dates, or `status = 1` and today within
`startTime`–`endDate`). *SQL* `jsGetQueries`.

**BR-RTE-4.** Only companies 1 and 2 are processed. *SQL* job steps.

**BR-RTE-5.** A new document starts at stage 1, status `P`; its month is the lines'
`CURRENTMONTH`. *SQL.*

**BR-RTE-6.** Once a document exists for (DocEntry, template), no later line of that
DocEntry is added to it or routed to that template again. *SQL.* (defect D-2)

**BR-RTE-7.** Nothing prevents two active templates from taking the same line.
*SQL; DATA.* (defect D-1)

## 3. Approving a budget document

**BR-APR-1.** A stage requires a configured number of approvals
(`jsApprovalCount.approval` via `jsStage.approvalId`). *SQL.*

**BR-APR-2.** A user acts as the **lowest** stage they hold in the document's
template. *SQL.* (defect D-11 when they hold two)

**BR-APR-3.** A user may approve if they hold the current stage, or the previous
stage while nobody has acted on the current stage. *SQL.*

**BR-APR-4.** A user cannot approve twice at the current stage. *SQL* (error 50020).

**BR-APR-5.** When the current stage has its required approvals: at the last stage
the document becomes `A` and SAP is told `A`; otherwise it moves to the next stage
and SAP is told `V`. Short of the requirement, nothing moves. *SQL.*

**BR-APR-6.** A previous-stage approver who approves again while the current stage
is untouched pulls the document back to their stage (a rejected document becomes
pending). *SQL.*

**BR-APR-7.** An approval and its SAP write succeed or fail together; SAP must
confirm every line or the approval is rolled back. *SQL*
`jsSyncBudgetToHanaDraftApproval` inside `jsApproveBudget`'s transaction.

**BR-APR-8.** Several documents can be approved in one request; each is a separate
call and they are not atomic together. *C#.*

**BR-APR-9.** Approver validity dates are not checked when approving or rejecting.
*SQL.* (defect D-12)

## 4. Rejecting

**BR-REJ-1.** Same eligibility as approval (BR-APR-2, -3); no double rejection at
the current stage. *SQL.*

**BR-REJ-2.** Every rejection vote writes `R` to SAP for all the document's lines at
once. *SQL.* (defect D-8)

**BR-REJ-3.** The document becomes `R` when the stage's rejection requirement is met.
The requirement is read from `jsRejectionCount` through `jsStage.approvalId`.
*SQL.* (defect D-9)

**BR-REJ-4.** A rejection reason is stored in the action's description. *SQL; DATA.*

**BR-REJ-5.** One document per rejection request; no notification is sent. *C#.*

**BR-REJ-6.** A user may cancel their own rejection (action `Cancel`, not used by the
C#). *SQL.*

## 5. Auto-approval

**BR-AUTO-1.** Every 10 minutes, a pending document idle for 48 hours or more at its
current stage is auto-approved — if it passes the budget check. *SQL; job "48 hrs".*

**BR-AUTO-2.** Budget check: approved documents of the same company, month and budget
plus this document must not exceed that month's allocation in
`bud.BudgetMonthlyAllocations`; a missing allocation counts as 0. *SQL.*

**BR-AUTO-3.** Auto-approvals are recorded as approvals by the current stage's real
users (never users 68, 79, 95), described "Auto-approved after 48 hours"; the
job's own id is 75. *SQL; DATA.*

**BR-AUTO-4.** Auto-approval moves stages and writes SAP exactly as a manual approval
(`V` / `A`). *SQL.*

## 6. What approvers see

**BR-LST-1.** Pending: documents at a stage the user validly holds, excluding those
the user already acted on at that stage. *SQL* `jsGetPendingBudgets`.

**BR-LST-2.** Approved / rejected: documents on which **this user** recorded `A` / `R`
at one of their stages. *SQL* `jsGetApprovedBudgets`, `jsGetRejectedBudgets`.

**BR-LST-3.** Credit memos (ObjType 19) are shown with negative totals. *SQL; DATA.*

**BR-LST-4.** Next approver = users of the current stage who have not acted. *SQL.*

**BR-LST-5.** The approval flow lists every stage of the template with every assigned
user and that user's action, before anyone acts. *SQL; DATA.*

## 7. Budget master and allocations

**BR-MST-1.** A budget belongs to a company and has a name, optional description,
total amount and active flag; sub-budgets have a name and description, no amount.
*SQL* `CreateBudgetWithSubBudgets`.

**BR-ALC-1.** One allocation per budget per month and one per sub-budget per month;
the month is the first day of the month. *SQL* (unique constraints; month check).

**BR-ALC-2.** Creating an allocation for a month that already has one overwrites it
(budget amount only when > 0; sub-budgets always). *SQL.*

**BR-ALC-3.** Sub-budgets in an allocation must belong to that budget. No rule ties
sub-budget amounts to the budget amount or monthly amounts to the total. *SQL.*

## 8. Allocation change requests

**BR-REQ-1.** A request names an allocation, an amount (> 0) and the requester. *SQL.*

**BR-REQ-2.** Only a request above 3,00,000 starts an approval workflow (approval
type 15, the company's first such template). Smaller requests are stored with no
workflow and cannot be approved. *SQL.* (defect D-14)

**BR-REQ-3.** On final approval the requested amount is **added** to the allocation.
*SQL* `jsApproveBudgetAllocationRequest`. The detail screen presents it as the new
total. (defect D-13)

**BR-REQ-4.** Approve/reject check that the request belongs to the caller's company.
*SQL.*

**BR-REQ-5.** No notification on allocation decisions. *C#.*

## 9. SAP write-back

**BR-SAP-1.** Decisions are written per line to `"<company schema>"."tbl_Draft_Approvals"`
(`ApprovedStatus`, `ACOMMENT`) over linked server `HANADB112`: `A` final, `V` stage
passed, `R` rejected. A missing row is inserted first. *SQL.*

**BR-SAP-2.** For documents made only of ObjType 13/14/18/19, the row is built from
SAP's draft tables `ODRF`/`DRF1`; otherwise from `bud.jsBudgetTable`. *SQL.*

**BR-SAP-3.** Every hour the current approval state of every document is also copied
to `"<company schema>"."js_budget_approval_workflow"`. *SQL.*

**BR-SAP-4.** No budget code uses SAP Service Layer. *C#.*

## 10. Security

**BR-SEC-1.** The budget endpoints require no authentication; the acting user id is
supplied by the caller. The SQL enforces stage membership for that id. *C#; SQL.*
