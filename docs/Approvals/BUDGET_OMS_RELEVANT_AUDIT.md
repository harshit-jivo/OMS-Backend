# BUDGET — OMS-Relevant Audit

**Date:** 2026-09-24 · **Phase:** audit only — nothing implemented, no schema proposed.
**Scope:** only the runtime behaviour a new OMS Budget module needs. OMS Budget starts
from scratch; **no legacy data is migrated** and no legacy data was analysed for this
document.

**Sources re-checked for this document**

| Source | What was read |
|---|---|
| Live SQL definitions, database `jsaplive3` | `JSAPNEW/docs/procedures/live/*.sql` — every object named below, read in full |
| SQL Agent job definitions (`msdb`) | job name, step command, schedule |
| JSAP C# | `UserService.cs` (approve 2295–2420, reject 1318, notify 2283, pending reminder 2422), `Auth2Service.cs` (allocation 591, 642, 692) |
| OMS Workflow Engine | `workflow/models.py`, `workflow/services/selection.py`, `assignments.py`, `WORKFLOW_ENGINE_IMPLEMENTATION_PLAN.md` |

**Labels:** **[SQL]** live definition · **[JOB]** SQL Agent definition · **[CONFIG]**
live approval configuration (templates, stages, stage users, counts — configuration,
not business data) · **[HANA]** live SAP HANA catalog / read-only query via the
JIVO gateway, 2026-09-24 · **[C#]** JSAP
source · **[ENGINE]** OMS workflow app · **[ASSUMPTION]** reasoning, not proven ·
**[UNKNOWN]** not answerable from these sources.

Legacy object names appear below only as evidence citations. They are not proposed
names for OMS.

---

## 1. Entry Ingestion

**Origin.** A budget entry is not created by a user. It originates from **SAP draft
lines** returned by the HANA procedure `"JIVO_OIL_HANADB"."DRAFT_APPROVAL"`, called
over the SQL Server linked server `HANADB112`. [SQL `InsertBudgetFromHANA`]
The procedure's result carries a `Branch` column; it is the only SAP call and it
feeds both Oil and Beverage entries. [SQL] Whether `DRAFT_APPROVAL` covers Mart is
[UNKNOWN] — nothing processes Mart downstream anyway (§2).

**Trigger.** SQL Agent job *Budget Sync Data*, enabled, **every 5 minutes**, three
steps in order: [JOB]

1. load SAP lines (`InsertBudgetFromHANA`);
2. route and create entries for company 1;
3. route and create entries for company 2.

There is no event from SAP and no user action; ingestion is a timed poll.

**Fields SAP supplies per line** [SQL, the load's column list]: `Branch` (company),
`DocEntry`, `ObjectName`, `ObjType`, `LineNum`, `VisOrder`, `AcctCode`, `AcctName`,
`CardCode`, `CardName`, `EFFECTMONTH`, `BUDGET`, `SUB_BUDGET`, `STATE`, `DocDate`,
`CreateDate`, `AMOUNT`, `CURRENTMONTH`, `Current_month_Posted_Amount`,
`Budget_Owner`, `OwnerCode`, `Approver Name`, `ApprovalCode`,
`Current_month_Budget`, `Status`, `U_NAME`, `CreatedDate`, `CreateTime`,
`LineRemarks`, `Comments`, `ProcesStat`, `UpdateDate`, `OcrCode`, `ACOMMENT`,
`VCOMMENT`, `VerifiedStatus`, `ApprovedStatus`.

**Line identity used by the legacy system** [SQL]:
`Branch + DocEntry + ObjType + LineNum + VisOrder`.

**Eligibility.** A line can become part of an entry only when `ProcesStat = 'Y'`.
[SQL `jsExecuteBudgetQueries`]

**Duplicates / new lines.** [SQL]

* A line is inserted only if its identity is not already stored. Stored lines are
  **never updated or removed** — a later change in SAP is not picked up.
* An entry is created only if no entry exists for **(DocEntry, template)**. Once it
  exists, later lines of the same DocEntry are **not added** to it and are not
  routed to that template again.
* The entry-existence check does not include ObjType or company, so two SAP
  documents of different types sharing a DocEntry collide under one template.
* For journal vouchers (ObjType 28) the line identity above is **not unique**. The
  true SAP identity is `BatchNum + TransId + Line_ID` (the SAP primary key of the
  voucher-line table), and `DRAFT_APPROVAL` drops `TransId` — proven in §13
  "B-2 evidence". [HANA] **Decision (B-2):** OMS identifies a JV source line by
  `Company + BatchNum + TransId + Line_ID`; SAP write-back keeps the JSAP key (§8, B-5).

**Budget figures on the line** (`Current_month_Budget`,
`Current_month_Posted_Amount`) are computed by SAP inside `DRAFT_APPROVAL`; the
legacy system does not calculate them. [SQL]

---

## 2. Workflow Routing

**Configuration.** Each approval **template** belongs to one company, has an active
flag, is linked to approval type **5 (Budget Approval)**, and owns one or more stored
**query texts**. [SQL `jsGetQueries`]

**Rule evaluated.** Every 5 minutes, for **each user** of the company:
[SQL `jsProcessAllUsersBudgetApprovals`, `jsGetQueries`, `jsExecuteBudgetQueries`]

1. take the stages the user **currently validly holds** (assignment has no dates, or
   is active with today inside its date window);
2. take the **active** type-5 templates of that company that contain one of those
   stages;
3. run each template's stored query — a full `SELECT … FROM <snapshot of SAP lines>
   WHERE …` — and keep result rows with `flag = 'A' AND ProcesStat = 'Y'`;
4. group the rows by (DocEntry, company, ObjType, month) and create an entry per
   group that has no entry yet for (DocEntry, template).

**What the stored queries filter on** [SQL, stored query texts]: company; budget
head; optional sub-budget list; journal voucher (`ObjType = 28`) vs any other type;
optional account-code include/exclude lists; optional load-date cut-off; occasionally
specific document numbers.

**Multiple matches.** **Yes — possible and not prevented.** Nothing compares templates
with each other; each matching template creates its own entry, so one SAP line can sit
in two entries with two approval chains. [SQL] (Observed in the live system.)

**No match.** The line is **silently ignored** — no entry, no error, no log. [SQL]

**Dependency on staffing.** A template's query runs only while at least one user
validly holds one of its stages; a template with no currently valid approver
creates nothing. [SQL]

**Companies processed.** 1 and 2 only. [JOB]

---

## 3. Workflow Creation

When an entry is created [SQL `jsExecuteBudgetQueries`]:

* **Stages:** the entry gets `totalStage` = number of stages linked to the template.
* **First stage:** the template stage with priority **1**; `currentStage = 1`.
* **Status:** `P` (pending). The entry's month = the lines' `CURRENTMONTH`.
* **Lines:** the distinct (ObjType, company, LineNum, VisOrder) rows from the query
  result for that DocEntry.
* Header and lines are written in **one transaction** (since the 2026-09-14 patch;
  before it, a header could be committed with no lines). [SQL]
* Each line is also inserted into a HANA table `"<schema>"."jsDocEntryDetail"`;
  failures are logged, not raised. [SQL]

No approver is copied onto the entry; approvers are resolved from configuration at
action time (§4). [SQL]

---

## 4. Stage & Approver Resolution

**Assignment.** A stage has **zero, one or many users** (user–stage assignment rows).
A stage can be shared by several templates. [SQL]

**Required counts.** A stage requires `approval` approvals (via the stage's
`approvalId`). The rejection requirement is read two different ways: the reject
action reads it through the stage's **`approvalId`**, the flow display through the
stage's **`rejectid`**. [SQL `jsRejectBudget` vs `jsGetBudgetApprovalFlow`]

**Multiple approvers at one stage: supported by the code** — any assigned user may
act until the required count is reached. [SQL]

**Live configuration (read 2026-09-24, active templates only)** [CONFIG]:

| | Budget Approval (type 5) | Allocation (type 15) |
|---|---|---|
| Active templates | 52 for company 1, 44 for company 2 | 1 each for companies 1, 2 **and 3** |
| Stages per template | 1, or 2 for a few (e.g. NJ/NPD templates) | 1 |
| Users per stage | **1 on every stage** | **1 on every stage** |
| Approvals required | **1 on every stage** | **1** |
| Rejections required | **1 on every stage** — read either way (`approvalId` or `rejectid`) | **1** |

So in practice every stage is **one user, one approval, one rejection** — the
multi-approver and count logic is never exercised, and the `approvalId`/`rejectid`
discrepancy has no effect today. A stage name carries a version suffix; stages are
not shared between the listed active templates.

**Acting stage of a user.** A user's stage in an entry is **the lowest-priority
stage they hold in that template** (`TOP 1 … ORDER BY priority`). A user holding two
stages of one template always acts as the lower one. [SQL]

**Assignment validity dates** are honoured by entry creation and the pending list,
but **not** by approve, reject, flow display, approved/rejected lists or
auto-approval. [SQL]

**Next approver** = users assigned to the current stage who have no action on the
entry at that stage. [SQL `jsGetNextApprover`] The C# calls it once per row of the
pending list. [C#]

---

## 5. Approve

[SQL `jsApproveBudget`; C# `UserService.cs:2295–2420`]

**Input:** entry id, company, acting user id (from the request body), remarks.
The procedure also accepts `@action = 'Revoke'`; the C# never sends it.

**One database transaction per entry:**

1. Entry not found → error.
2. User holds no stage in the template → error.
3. Allowed only if the user holds the **current** stage, or the **previous** stage
   while nobody has acted on the current stage yet; else error.
4. Already approved at the current stage → error.
5. If a previous-stage user who had approved approves again while the current stage
   is untouched → the entry **moves back** to that user's stage (and a rejected entry
   becomes pending again).
6. Record the approval for (entry, user's stage, user): insert, or update the
   user's existing row (status, remarks, timestamp **overwritten**).
7. Count approvals at the current stage:
   * required count reached, **last stage** → entry status `A`, then the SAP write
     `A` (§7, §8);
   * reached, not last → advance to the next stage, then the SAP write `V`;
   * not reached → returns *"Not enough approvals … Current: x, Required: y"*.
8. Any error → rollback, error logged, error re-raised.

**Bulk:** the C# accepts comma-separated ids and calls the procedure once per id on
one connection **without a transaction across ids**; a failure part-way leaves the
earlier ones approved and sends no notification. [C#]

**Remarks/history:** one row per (entry, stage, user) holding the latest status,
remarks and time. A changed decision **overwrites** the earlier one. [SQL]

**Notification** [C#]: after each entry, the C# asks `jsBudgetNotify` for the users
to notify, then — once for the whole request — sends an FCM push per distinct device
token and one in-app notification per user ("Pending request", page 6). Push text:
*"New budget approval request for Budget ID … Please approve within 48 hours, or it
will be auto-approved."* `jsBudgetNotify` chooses "remaining users of the current
stage" vs "users of the next stage" by comparing the number of actions (any status)
with the stage's **`approvalId`** — an id, not the required count. [SQL] A separate
endpoint sends each user a "You have N pending requests" push; what calls it is not
in the sources. [C#][UNKNOWN]

---

## 6. Reject

[SQL `jsRejectBudget`; C# `UserService.cs:1318`]

**Input:** as approve; one entry per request (a comma-separated list throws). The
procedure also accepts `@action = 'Cancel'`; the C# never sends it.

**One transaction:**

1. Same eligibility as approve (current stage, or untouched previous stage); already
   rejected at the current stage → error.
2. A previous-stage approver switching to reject moves the entry back to their stage.
3. Record `R` (insert or overwrite the user's row) **and immediately write `R` to SAP
   for all the entry's lines** — on every rejection vote.
4. Count rejections at the current stage against the rejection requirement (read via
   `approvalId`, §4):
   * reached → entry status `R`, SAP `R` again;
   * not reached → entry stays `P` in JSAP while SAP already shows `R`.

**Remarks** are stored as the action's description. **No notification** is sent. [C#]

**After rejection:** the entry stays rejected. The only way back is a previous-stage
approver re-approving (§5 step 5). An older mechanism that archived rejected entries
for re-creation when SAP changed them is a trigger that no longer fires (nothing
performs the update it listens for). [SQL]

---

## 7. Final Approval

When the last stage reaches its required approvals [SQL `jsApproveBudget`]:

1. entry status `P` → `A`;
2. `jsSyncBudgetToHanaDraftApproval(entry, company, 'A', user, remarks)` — inside the
   same transaction (§8);
3. result message *"Budget approved and marked as Approved"*.

Nothing else changes on final approval: no allocation, balance or budget total is
updated; no other record is written. [SQL] "Consumption" exists only as a
calculation over approved entries in reports and in auto-approval (§10).

---

## 8. SAP/HANA Integration

**Does approval write to SAP? Yes.** [SQL `jsSyncBudgetToHanaDraftApproval`]

| Item | Verified behaviour |
|---|---|
| Object changed | HANA table `"<company schema>"."tbl_Draft_Approvals"`; schema from configuration (company 1 → Oil database, 2 → Beverage, 3 → Mart); linked server hard-coded `HANADB112` |
| Row granularity | one row per entry line, keyed `DocEntry + ObjType + LineNum + VisOrder` |
| Columns written | `ApprovedStatus`, `ACOMMENT`; if the row does not exist it is inserted first with document fields |
| Row source when inserting | ObjType 13/14/18/19 only → from SAP draft tables `ODRF`/`DRF1`; otherwise → from the stored SAP line |
| Status values | **`A`** final approval · **`V`** a non-final stage completed · **`R`** a rejection vote |
| Execution | all lines in one HANA `DO BEGIN … END` block |
| Verification | reads back the count of lines with the new status; fewer than the entry's lines → logs and **raises** |
| Timing | **synchronous, inside the approve/reject transaction** — a HANA failure or failed verification **rolls back the approval** |
| Entry with no lines | logs and returns **without error** — the approval succeeds and nothing reaches SAP |
| Log | every attempt, success included, to an error-log table |

**Other SAP writes** (not decision-critical): each routed line is mirrored to
`"<schema>"."jsDocEntryDetail"` at creation; an hourly job copies the current
approval state of every entry to `"<schema>"."js_budget_approval_workflow"`. [SQL][JOB]

**What SAP does with `ApprovedStatus`** — whether a draft cannot be posted until `A`,
and what `V` means inside SAP — is [UNKNOWN]; it is SAP-side logic.

**Attachments** are read from SAP (`DRAFT_APPROVAL_ATC1`) every 2 minutes and matched
to entries by DocEntry only. [SQL][JOB]

**Decision (B-5): OMS reproduces this write-back exactly** — same table, same status
values (`A` / `V` / `R`), same row key `DocEntry + ObjType + LineNum + VisOrder`. For a
journal voucher, `DocEntry` = `BatchNum` and `LineNum` = `VisOrder` = `Line_ID`, as
`DRAFT_APPROVAL` supplies them.

> **Known inherited limitation — not an OMS Workflow Engine defect.** SAP's decision
> table `tbl_Draft_Approvals` has no `TransId`, so when two vouchers of one batch share
> a `Line_ID`, a decision written for one line is also recorded against the other.
> This comes from SAP's table and `DRAFT_APPROVAL`, and exists identically in JSAP
> today. OMS keeps the correct source identity internally (B-2) and inherits the SAP
> write-back limitation unchanged; it adds no blocking rule and no new SAP mechanism.
> Evidence: §13 "B-5 evidence".

---

## 9. Allocation Approval

Separate from entry approval: its own approval type (**15**), its own request and
flow records, its own procedures. It shares only the stage/template configuration
tables. [SQL]

**Creation** [SQL `CreateBudgetAllocationRequest`; C# `Auth2Service.cs:692`]

* Input: allocation id (a budget's month), `newAmount` (> 0), requester.
* Validates: allocation exists, user exists, amount > 0.
* Always stores the request.
* **Only when `newAmount > 300000`:** finds the first type-15 template of the budget's
  company, creates a flow at stage 1 (status `P`). No template → error.
* **`newAmount ≤ 300000`:** the request is stored with **no flow**; the message says
  it is *"NOT approvable"*. Nothing else happens to it.

**Approval stages** [SQL `jsApproveBudgetAllocationRequest`] — same pattern as entry
approval (§4–§5): lowest stage held, current or untouched previous stage, required
count per stage, advance or finalise. Additional check: the request's company must
equal the caller's `@company`.

**Final approval changes** — exactly one thing: [SQL]

```sql
allocatedAmount = allocatedAmount + newAmount      -- of that budget-month allocation
```

`newAmount` is **added**. The request-detail procedure, however, displays
`amountDifference = newAmount − allocatedAmount`, i.e. treats `newAmount` as the
**new total**. [SQL `jsGetBudgetAllocationRequestDetail`]

**Decision (B-3):** `newAmount` is an **increase**. OMS reproduces the approval rule
`allocatedAmount = allocatedAmount + newAmount`; the detail procedure's "new total"
presentation is the inconsistency not to copy (§12).

**Rejection** [SQL `jsRejectBudgetAllocationRequest`] — rejection votes against a
rejection count (read via `approvalId`); reached → flow `R`. The allocation is **not
changed**. (The approve procedure also has a `Reject` branch that rejects on a single
vote; the C# calls the separate reject procedure.) No SAP write. **No notification.**
[SQL][C#]

**Direct changes without approval** [SQL `CreateMonthlyAllocations`]: creating an
allocation for a month that already has one **overwrites** it (budget amount when
> 0; sub-budget amounts always). The dedicated update procedure the API calls
(`UpdateMonthlyAllocations`) **does not exist** in the live database, so that
endpoint always fails. [SQL]

**Threshold:** approval is required **only above 3,00,000** — confirmed, hard-coded.

**Decision (B-3):** reproduced as-is — requests **> ₹3,00,000** create an approval
workflow; requests **≤ ₹3,00,000** are stored with **no approval flow**, exactly as JSAP
does (they do not change the allocation).

---

## 10. Auto Approval

**Exists: yes — confirmed.** [SQL `jsAutoApproveBudgetAfter48Hours`; JOB "48 hrs"]

| Item | Verified |
|---|---|
| Scheduler | SQL Agent job *48 hrs*, enabled, every 10 minutes, `EXEC … 75` (database `jsaplive3`) |
| Candidates | entries with status `P` and a current stage |
| Timer | hours since the latest action at the current stage, else since the entry's `updatedOn`; acts at **≥ 48** |
| Budget check | amount of this entry for its month + budget head, plus the sum of **fully approved** entries of the same company/month/budget head, must not exceed that budget-month's allocation; **no allocation counts as 0** → skipped |
| Action | inserts approvals for **real users assigned to the current stage** — enough to meet the required count; never users 68, 79, 95; never someone who already approved; description *"Auto-approved after 48 hours"* |
| Outcome | last stage → status `A` + SAP `A`; otherwise advance + SAP `V` |
| Identity | parameter `@systemUserId = 75` is logged as the performer; it is a named user account |
| Logging | every decision (including skips) written to a log table |
| Failure | one error aborts the whole run |

A second, disabled job ("24 hrs") targets a different database (`jsap`). [JOB]
The C# push texts promising auto-approval are consistent with this job. [C#]
Why users 68, 79 and 95 are excluded is not stated. [UNKNOWN] Because every stage has
exactly one user (§4), the effect is that a stage held by one of those users is never
auto-approved (no eligible user to record the approval under).

**Decision (B-4): KEEP.** OMS implements the same business behaviour — 48 hours idle
at the current stage, the monthly-allocation check, stage advance or final approval,
SAP `V` / `A` — but records the step as a **system-generated action**, not as an
approval by the stage's real user or by a named person's account.

---

## 11. Confirmed OMS Requirements

What OMS **must reproduce**, stated as behaviour (each item traces to a section above):

| # | Requirement | From |
|---|---|---|
| R-1 | Budget entries originate from SAP draft lines on a schedule, not from user submission. | §1 |
| R-2 | Only lines SAP marks ready (`ProcesStat = 'Y'`) become entries. | §1 |
| R-3 | Routing is configuration: company, budget head, sub-budget, journal voucher vs other, account-code include/exclude, effective date. | §2 |
| R-4 | An entry is one SAP document's slice that one workflow approves; one SAP document can yield several entries with different approvers. | §2, §3 |
| R-5 | Ordered stages; the entry starts at stage 1; the current stage's approver acts. | §3, §4 |
| R-6 | Approve advances to the next stage; approving the last stage makes the entry approved. | §5, §7 |
| R-7 | Reject records a reason and ends the entry as rejected. | §6 |
| R-8 | Approvers can list: pending for them, what they approved, what they rejected (own actions). | §4, §5, §6 |
| R-9 | The decision reaches SAP per line in `tbl_Draft_Approvals.ApprovedStatus` with the remark: `A` on final approval, `V` when a non-final stage completes, `R` on rejection — row key `DocEntry + ObjType + LineNum + VisOrder`, exactly as JSAP (B-5). | §8 |
| R-10 | Journal vouchers are budget-controlled like other documents; ObjType 19 amounts count negative in totals. | §1, §2 |
| R-11 | Allocation changes are a separate approval with its own workflow, and change the allocation only on final approval; rejection leaves it unchanged. | §9 |
| R-12 | The next approver is notified after an approval. | §5 |
| R-13 | Allocation request `newAmount` is an **increase**: final approval sets `allocatedAmount = allocatedAmount + newAmount`. Only requests **> ₹3,00,000** start an approval workflow; requests **≤ ₹3,00,000** are stored with no flow (B-3). | §9 |
| R-14 | **48-hour auto-approval is kept**: idle ≥ 48 h at the current stage + monthly-allocation check (approved entries of the same company/month/budget + this entry ≤ allocation; no allocation = 0) → approve the stage (advance with SAP `V`, or final with SAP `A`). Recorded as a **system-generated action** (B-4). | §10 |
| R-15 | Journal-voucher source lines are identified by **Company + BatchNum + TransId + Line_ID** (B-2). | §1, §13 |

Mapping onto the existing OMS Workflow Engine [ENGINE]:

* The engine selects **exactly one** workflow per document by executing validated,
  read-only condition queries bound to the document's key; zero → not configured,
  more than one → ambiguous (rollback). This directly satisfies R-3/R-4 **and**
  removes the legacy multiple-match and silent-no-match behaviour — provided the
  module presents the unit the engine selects for (the line, or a group of lines that
  cannot match different workflows).
* Stages are ordered, one user per stage; temporary stand-ins by date
  (`WorkflowUserReplacement`); the engine owns configuration only — the Budget module
  owns its runtime (tasks, actions, history, statuses).
* No approval/rejection counts exist in the engine (closed decision). The live
  configuration uses one user, one approval and one rejection on every stage (§4), so
  the engine's model reproduces current behaviour exactly — B-1 resolved.
* Allocation approval has an active template for company 3 (Mart) as well as 1 and 2
  [CONFIG], so the allocation workflow must cover all three companies even though
  entry ingestion processes only 1 and 2.

What OMS **should intentionally improve** (legacy behaviour verified above, replaced
on purpose):

| # | Improvement | Legacy behaviour it replaces |
|---|---|---|
| I-1 | A line matching **no** workflow is surfaced (the engine's `WorkflowNotConfigured`), not ignored. Applies to no-match lines only — journal-voucher key collisions get **no** special rule (B-5). | §2 silent ignore |
| I-2 | A line can belong to **one** entry only (engine fail-loud selection). | §2 multiple entries per line |
| I-3 | A line that arrives after its entry exists joins the entry while it is pending; after a decision it is handled explicitly. | §1 late lines never routed |
| I-4 | SAP line changes are detected and reflected; a change after approval is not silently accepted. | §1 append-only |
| I-5 | Entry identity includes company and ObjType. | §1 collision |
| I-6 | Routing does not depend on whether an approver is currently valid. | §2 staffing dependency |
| I-7 | Acting user from the authenticated session, never the request body. | §5 |
| I-8 | Append-only history — every action kept, none overwritten. | §5, §6 |
| I-9 | Stand-in/validity rules applied everywhere, via the engine's replacement mechanism. | §4 |
| I-10 | SAP told `R` only when the entry is actually rejected (automatic with one-reject-ends). | §6 |
| I-11 | An entry without lines cannot exist or be approved. | §3, §8 |
| I-12 | Bulk approval returns a per-entry result; bulk reject available too. | §5, §6 |
| I-13 | Notify on rejection and on allocation decisions; compute recipients from workflow state. | §5, §6, §9 |
| I-14 | Attachments matched by company + ObjType + DocEntry. | §8 |
| I-15 | Auto-approval (kept, B-4) is recorded as a system-generated action, not as real approvers' ids or a named person's account. | §10 |

---

## 12. Legacy Behavior NOT to Copy

* **Stored SQL text executed as a batch sweep** over all SAP lines per user per run.
  (The engine's bound, validated, read-only per-document condition is the
  replacement.)
* **Existence check on (DocEntry, template)** as the only duplicate guard.
* **Append-only snapshot** of SAP lines with no change detection; the non-unique
  journal-voucher **source** line key (OMS uses `Company + BatchNum + TransId +
  Line_ID`). The SAP **write-back** key is *not* in this list — it is reproduced
  unchanged (B-5).
* **Creating a header before its lines** / any path that leaves an entry without lines.
* **Lowest-stage-wins** actor resolution for users on several stages.
* **Previous-stage "pull back"** on re-approval as an implicit side effect of approving.
  (If the business wants recall, it must be an explicit action.)
* **Overwriting a user's earlier action** row on a changed decision.
* **Writing `R` to SAP on a rejection vote** that does not reject the entry.
* **Rejection requirement read through the approval-count column**; notification
  recipients chosen by comparing an action count with a configuration id.
* **Per-request `userId` trust**; unauthenticated endpoints.
* **Presenting an allocation request as a new total** (the detail view's
  `newAmount − allocatedAmount`) while approval adds it — OMS presents it as the
  increase it is (B-3). The add-on-approval rule and the ≤ ₹3,00,000 no-flow rule are
  *reproduced*, not in this list.
* Allocation "create" that silently overwrites an existing month.
* **Hard-coded values inside SQL:** linked server name, schema names, user ids
  68/75/79/95, document numbers inside routing rules, month strings in three formats.
  (The ₹3,00,000 threshold *value* is reproduced — B-3.)
* **Auto-approval recorded as named users' approvals.**
* **Per-run success logging** into ever-growing log tables; the hourly full resend of
  approval state to SAP.
* **Dormant/destructive objects**: the re-creation trigger, the old updating loader,
  and the "cleanup" procedures that delete entries not found in SAP.

---

## 13. Remaining Unknowns

### Formerly blocking — all resolved (2026-09-24)

| # | Question | Resolution | Basis |
|---|---|---|---|
| B-1 | Do live budget stages need several users per stage, or approval/rejection counts > 1? | **Resolved.** Every active stage of every type-5 and type-15 template has 1 user, 1 approval, 1 rejection. The OMS Workflow Engine fits; **no structural engine change**. | live configuration (§4) |
| B-2 | What uniquely identifies a journal-voucher source line? | **Resolved.** `Company + BatchNum + TransId + Line_ID` (SAP primary key of `BTF1`). | HANA proof below |
| B-3 | Allocation request semantics and the threshold. | **Resolved — reproduce JSAP.** `newAmount` is an increase; final approval `allocatedAmount = allocatedAmount + newAmount`; only requests > ₹3,00,000 create a workflow; ≤ ₹3,00,000 stored with no flow. | business decision; §9, R-13 |
| B-4 | Keep 48-hour auto-approval? | **Resolved — keep.** Same business behaviour (§10), recorded as a system-generated action. | business decision; R-14 |
| B-5 | Journal-voucher SAP write-back cannot carry `TransId`. | **Resolved for OMS design — reproduce JSAP write-back exactly** (`DocEntry + ObjType + LineNum + VisOrder`). The collision is a **known inherited SAP/JSAP limitation, not an OMS Workflow Engine defect**. No blocking/unrouted rule for collisions, no SAP schema or procedure change required, no new write-back mechanism. | business/design decision; §8, evidence below |

The evidence sections below are kept as the record of what was verified.

### B-2 evidence — journal-voucher line identity (HANA, 2026-09-24)

All queries read-only, via the JIVO gateway (`SYS.PROCEDURES`, table metadata,
`SELECT` on SAP tables). Nothing was executed on SAP except `SELECT`.

**1. Where JV lines come from.** Live `"JIVO_OIL_HANADB"."DRAFT_APPROVAL"` (created
2026-08-14, 20,192 chars; read from `SYS.PROCEDURES.DEFINITION`) builds the ObjType 28
branch — once for Oil, once for Beverage — as:

```sql
SELECT DISTINCT 'OIL' AS "Branch", B."BatchNum" "DocEntry", 'JOURNAL VOUCHER' AS "ObjectName",
       28 "ObjType", A."Line_ID" "LineId", A."Line_ID" "VisOrder", A."Account", …
FROM "JIVO_OIL_HANADB"."BTF1" A
INNER JOIN "JIVO_OIL_HANADB"."OBTF" B ON A."BatchNum" = B."BatchNum"      -- no TransId
…
WHERE C."FatherNum" IN ('5690000',…,'5610000') AND C."AcctCode" NOT IN ('5630001',…,'5630016')
  AND A."Debit" <> 0 AND B."RefDate" >= '2025-04-01' AND A."OcrCode3" != 'Sal CF'
```

The outer query then keeps only lines whose `tbl_Draft_Approvals` row is not yet `A`,
joining on `DocEntry + LineNum + ObjType` (no `VisOrder`, no `TransId`).

**2. Every JV source column the procedure outputs.**

| Output column | Source | | Output column | Source |
|---|---|---|---|---|
| `DocEntry` | `OBTF.BatchNum` | | `DocDate` / `CreateDate` / `CreatedDate` | `OBTF.RefDate` / `OBTF.CreateDate` / `OBTF.CreateDate` |
| `ObjType` | constant 28 | | `AMOUNT` | `BTF1.Debit` |
| `LineNum`, `VisOrder` | **both** `BTF1.Line_ID` | | `LineRemarks` | `BTF1.LineMemo` |
| `AcctCode` / `AcctName` | `BTF1.Account` / `OACT.AcctName` | | `Comments` | `OBTF.Memo` |
| `EFFECTMONTH` / `BUDGET` / `SUB_BUDGET` / `STATE` | `BTF1.OcrCode2/3/4/5` | | `ProcesStat` | `OBTF.BtfStatus`: `O`→`Y`, `C`→`P` |
| `OcrCode` | `BTF1.ProfitCode` | | `UpdateDate` | `OBTF.UpdateDate` |
| `CardCode` / `CardName` | constant `'NA'` | | `U_NAME` | `OUSR` via `OBTF.UserSign` |
| `Status` / `CreateTime` | constant `'Y'` / `0000` | | budget / posted figures | computed sub-queries |

**`TransId` is not output.**

**3. SAP keys** (HANA table metadata):

| Table | Primary key |
|---|---|
| `BTF1` (voucher lines) | **`TransId`, `Line_ID`, `BatchNum`** |
| `OBTF` (voucher headers) | **`BatchNum`, `TransId`** — one row per voucher in a batch |
| `tbl_Draft_Approvals` (decision record) | **none**; columns include `DocEntry`, `ObjType`, `LineNum`, `VisOrder` — **no `TransId`** |

A batch holds several vouchers (`TransId` 1, 2, …, restarting per batch), each with
lines numbered from `Line_ID` 0. So `BatchNum + Line_ID` repeats across vouchers of
one batch.

**4. Real duplicates.** `BTF1` today: 18 Oil batches and 13 Beverage batches hold more
than one voucher; `BatchNum + Line_ID` repeats 57 times (Oil) and 42 times (Beverage).
Example, Beverage batch 1843 (RefDate 2026-05-31, 2 vouchers):

| BatchNum | TransId | Line_ID | Account | OcrCode3/4 | Debit |
|---|---|---|---|---|---|
| 1843 | 1 | 0 | 5630001 | FACT_COM | 80,312.00 |
| 1843 | 2 | 0 | 5630001 | Sales / GT | 268,806.00 |
| 1843 | 1 | 1 | 5630001 | Factory | 682,863.00 |
| 1843 | 2 | 1 | 5630011 | Sales / GT | 176.00 |

(These particular lines fall outside the procedure's account filter; with the full
filter applied, no colliding `BatchNum + Line_ID` pair is present in HANA today. The
legacy collisions were on batches no longer in SAP — e.g. Oil 5483 has no row in
`OBTF`, `BTF1`, `OJDT` or `JDT1`.)

**5. The second defect — header fan-out.** Because the procedure joins `OBTF` on
`BatchNum` only, each voucher line is paired with **every** voucher header of its
batch. Beverage 1843, `Line_ID` 27 (account 5680014, Factory, 0.13 — a line that
passes the filter) belongs to voucher `TransId` 1, yet the procedure's join returns it
twice:

| Line's TransId | Joined header TransId | Memo on the output row |
|---|---|---|
| 1 | 1 | FACTORY BELOW May-26 |
| 1 | 2 | **SALES BELOW MAY-26** — another voucher's memo |

`tbl_Draft_Approvals` in Beverage holds **two rows** for `DocEntry 1843, ObjType 28,
LineNum 27` — the fan-out reached SAP's decision record. This is also why legacy lines
appeared "four times": a batch with four vouchers.

**6. Can the identity be used for write-back?** No, not with SAP's objects as they are:

* `tbl_Draft_Approvals` has no `TransId` and no key; the JSAP write-back
  (`jsSyncBudgetToHanaDraftApproval`) matches `DocEntry + ObjType + LineNum + VisOrder`,
  and `DRAFT_APPROVAL` reads decisions back by `DocEntry + LineNum + ObjType`.
* Therefore, for two vouchers of one batch that share a `Line_ID`, SAP write-back
  **can** distinguish: company (schema), batch, line number. It **cannot** distinguish:
  which voucher. Writing `A` for one updates both rows, and `DRAFT_APPROVAL` then hides
  both lines as approved.

**Conclusion.** Source identity: **resolved** — `company + BatchNum + TransId +
Line_ID`, guaranteed unique by SAP's primary key on `BTF1`; OMS ingestion must read
`TransId` and join `OBTF` on `BatchNum + TransId`. Write-back: SAP's decision table
cannot carry `TransId`; by decision B-5 OMS reproduces the JSAP write-back key
unchanged and documents the collision as an inherited limitation.

### B-5 evidence — every live journal-voucher collision (HANA, 2026-09-24)

Read-only. A **collision group** = one company + `BatchNum` + `Line_ID` carried by more
than one `TransId` in `BTF1` (headers joined correctly on `BatchNum + TransId`).
**Eligible** = passes the procedure's own JV filter (expense father account, not in the
excluded 5630001… list, `Debit <> 0`, `RefDate >= 2025-04-01`, budget ≠ `Sal CF`).

**1–2. All groups and their source rows.** 176 `BTF1` rows form the groups below.

| Company | Batches | Groups | Budget differs | Budget/sub-budget differs | Account differs | Debit differs | Fully identical |
|---|---|---|---|---|---|---|---|
| Oil | 18 | 41 | 3 | 3 | 16 | 20 | 15 |
| Beverage | 13 | 36 | 12 | 12 | 15 | 21 | 11 |

Batches: Oil 131, 1244, 1355, 1640, 3749, 3752, 4354, 4355, 4781, 4903, 5564, 5607,
6125, 6210, 6351, 6573, 6590, 6683 · Beverage 413, 547, 873, 1740, 1843, 1873, 1895,
1901, 1974, 1982, 1984, 2010, 2029.

Examples with **different budget decisions in one group**:

| Company · Batch · Line_ID | TransId | Account | Budget / sub-budget | Debit |
|---|---|---|---|---|
| Beverage · 1843 · 0 | 1 | 5630001 | FACT_COM | 80,312.00 |
| | 2 | 5630001 | Sales / GT | 268,806.00 |
| Beverage · 1843 · 1 | 1 | 5630001 | Factory | 682,863.00 |
| | 2 | 5630011 | Sales / GT | 176.00 |
| Oil · 1355 · 0 | 1 | 5630001 | Sales / GT | 44,000.00 |
| | 2 | 5630001 | NPD2 | 242,000.00 |
| Oil · 1355 · 1 | 1 | 5630001 | Sales / GT | 80,292.74 |
| | 2 | 2133004 | NPD2 | 0.00 (credit 42,000) |

**3. `tbl_Draft_Approvals` rows for these batches** (ObjType 28): none, except Beverage
1843 `LineNum 27`, stored **twice** (the header fan-out, B-2 evidence §5). No decision
has been written for any collision group.

**4. Comparison.**

* Budget: differs in 15 of 77 groups (FACT_COM vs Sales/GT, Factory vs Sales/GT,
  Sales/GT vs NPD2 …).
* Sub-budget: differs wherever the budget differs.
* Account: differs in 31 groups.
* Amount: differs in 41 groups.
* Eligibility: **all 176 rows are currently ineligible** (balance-sheet accounts, or
  expense accounts in the excluded 5630001… list, or pre-2025-04-01). Eligibility is
  decided **per line** (account, debit, budget), so nothing ties the members of a group
  to the same answer.
* Routing: a line's workflow is chosen by company + budget + sub-budget + JV class +
  account class (§2). Lines with different budgets therefore route to **different
  workflows with different approvers** — e.g. Sales/GT vs FACT_COM vs NPD2 are
  separate active templates (§4).

**5. What the limitation means.** Collision groups do **not** always form one budget
decision: 15 live groups mix budget heads, and the legacy snapshot recorded an eligible
case (Oil JV 5483, `Line_ID` 0 carrying Factory, FACT_COM, BackOff/Legal and
BackOff/IT). Where eligible lines of different vouchers share a `Line_ID`, the SAP
decision written for one of them is recorded against the shared key, so SAP's decision
table cannot show a different outcome for the other. Today no eligible line is in a
collision group (point 4).

**6. Decision (B-5).** OMS reproduces the JSAP write-back exactly and treats this as a
**known inherited SAP/JSAP limitation — not an OMS Workflow Engine defect.** Inside
OMS every voucher line keeps its own identity (B-2), routing and approval; only the
SAP decision row is shared, as in JSAP. No collision-blocking or unrouted rule is
added, no SAP schema or procedure change is required, and no SAP or SQL Server object
was modified in this audit. A future SAP-side change (adding `TransId` to
`tbl_Draft_Approvals` and to `DRAFT_APPROVAL`'s join) would remove the limitation; it
is optional and outside the OMS scope.

### Non-blocking — needed later, with a verified default

| # | Unknown | Default until answered |
|---|---|---|
| N-1 | What SAP does with `ApprovedStatus` `A`/`V`/`R` (posting gate?) | reproduce the write exactly (R-9) |
| N-2 | Does `DRAFT_APPROVAL` return Mart lines; should Mart be budget-controlled? | Oil and Beverage only, as today |
| N-3 | SAP write timing in OMS: synchronous with rollback (legacy) or outbox with retry (engine plan) | design choice, not a legacy question |
| N-4 | Why users 68/79/95 are excluded from auto-approval (effect: their stages are never auto-approved, §10) | reproduce the effect as a configurable exemption list of stage users; confirm the reason with the business |
| N-5 | What calls the pending-count reminder endpoint | reproduce as a scheduled reminder if wanted |
| N-6 | Definition of "used/available" for dashboards (count pending? show overspend?) | approved entries only, as auto-approval does |

---

## FINAL STATUS

**READY FOR OMS DESIGN.**

| Item | Status |
|---|---|
| B-1 Stage staffing / counts | Resolved — engine fits, no structural change |
| B-2 JV source identity | Resolved — `Company + BatchNum + TransId + Line_ID` |
| B-3 Allocation semantics | Resolved — increase; > ₹3,00,000 workflow; ≤ ₹3,00,000 stored with no flow |
| B-4 48-hour auto-approval | Resolved — kept, recorded as a system-generated action |
| B-5 JV write-back | Resolved for OMS design — JSAP write-back reproduced exactly; known inherited SAP/JSAP limitation, not an OMS Workflow Engine defect |

**No blocking audit items remain.** The non-blocking items N-1 … N-6 each have a
verified default and can be settled during design. Nothing was implemented; no code,
database, SAP or SQL Server change was made.
