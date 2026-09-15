# BKDT — BackDate

**BKDT = the BackDate module.** A user asks for permission to post SAP documents
of one type, in one or more companies, with a posting date inside a bounded past
window. The request is approved through a configured chain, and the final
approval writes the grant into SAP's HANA database.

This document is the technical source of truth for:

- the data model
- Workflow Engine integration
- SAP/HANA integration
- permissions
- notifications
- **SQL Server → PostgreSQL query translation** (copy a live JSAP condition,
  get a ready-to-paste OMS one — see §12)
- debugging

**Existing JSAP transaction data is NOT migrated into OMS. OMS BKDT starts
fresh.** JSAP is used as the behavioural and configuration reference only:
`backdate.userDocument`, `backdate.jsFlow` and `backdate.jsFlowStatus` stay in
JSAP and remain queryable there. The BEHAVIOUR was migrated; the rows were not.

### How claims in this document are labelled

| Label | Means |
|---|---|
| **[OMS]** | Verified from the current OMS implementation (code or live schema) |
| **[JSAP]** | Verified from JSAP source, its live SQL Server, or the live HANA catalogue |
| **[INFERRED]** | A reasonable reading, not directly observed |
| **[UNKNOWN]** | Needs business confirmation — do not guess |

Related documents:
`docs/migration/BKDT_TO_OMS_MIGRATION_PLAN.md` (the migration analysis) and
`docs/architecture/WORKFLOW_MODULE_INTEGRATION.md` (how any module plugs into
the engine).

---

## 1. High-level architecture

```
                         OMS
                          |
             ┌────────────┴─────────────┐
             |                          |
      WORKFLOW ENGINE               BKDT MODULE
             |                          |
             |                          ├── BackDate
             |                          ├── BackDateFlow
             |                          ├── ActionLogs
             |                          ├── HANA
             |                          └── Notifications
             |
             ├── workflow_modules
             ├── workflows
             ├── workflow_queries
             ├── workflow_stages
             └── workflow_user_replacements
```

**Workflow Engine** = workflow configuration + workflow selection + stage/user
configuration. It answers exactly one question: *which workflow applies to this
document, and what stages/users are configured?*

**BKDT** = business data + flow + actions/history + HANA + notifications.

The engine never calls HANA and never stores a BKDT row. BKDT never decides
which workflow applies. The only thing crossing the boundary is a stage id.
**[OMS]**

---

## 2. Workflow Engine tables

All five live in the `workflow` PostgreSQL schema. **[OMS]**

### 2.1 `workflow.workflow_modules`

A registry of Workflow-enabled modules, and nothing else.

| Column | Notes |
|---|---|
| `id` | PK |
| `created_at`, `updated_at` | |
| `code` | UNIQUE, forced uppercase by CHECK `workflow_module_code_upper`. BKDT registers as `BKDT` |
| `name` | display name |

It must **NOT** contain `business_table`, `business_key_column`, `flow_table`,
`flow_model` or `is_active`. Those were removed deliberately: the engine is
generic and cannot know which tables a module owns, so the module supplies that
as runtime context on every call instead of the engine holding a second copy it
could not verify.

BKDT self-registers from `backdate/apps.py` on `post_migrate`:

```python
register_module(code='BKDT', name='BackDate')
```

### 2.2 `workflow.workflows`

| Column | Notes |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `company` | `ALL` / `OIL` / `BEVERAGES` / `MART`, CHECK `workflow_company_valid` |
| `code` | UNIQUE per module (`workflow_module_code_uq`) |
| `name` | |
| `module_id` | FK → `workflow_modules` |
| `is_active` | inactive workflows are excluded from SELECTION only |

`company` is a direct value. **There is no `company_scope` column** — one column
with `ALL` as a literal member replaced the old scope/value pair.

`ALL` does **not** outrank a specific company. If both an `ALL` workflow and an
`OIL` workflow match an OIL document, that is **two matches and therefore
ambiguity**, not a precedence rule.

Deactivating a workflow does **not** break flows already running on it:
`selection.stages_for()` filters on stage activity, not workflow activity. Only
new selection excludes it. **[OMS]**

### 2.3 `workflow.workflow_queries`

> A workflow query determines whether a business entry belongs to a workflow.

| Column | Notes |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `company` | same four values as above |
| `name` | UNIQUE per workflow (`workflow_query_name_uq`) |
| `query_text` | the condition SQL. CHECK `workflow_query_select_only` enforces it starts with SELECT/WITH |
| `validated_at` | **the execution gate** — selection refuses to run a query whose `validated_at` is NULL |
| `validation_error` | the last failure text, for the UI |
| `workflow_id` | FK → `workflows` |
| `is_active` | |

No `company_scope`, no `type`, no `key_column`. The key column is runtime
context supplied by the module (BKDT uses the default, `id`).

### 2.4 `workflow.workflow_stages`

| Column | Notes |
|---|---|
| `workflow_id` | FK |
| `sequence` | execution order ONLY. UNIQUE per workflow; CHECK `sequence >= 1` |
| `name` | |
| `user_id` | FK → `users_user`. **Exactly one configured user per stage** |
| `is_active` | |

There is no quorum and no approval count. One approve completes a stage; one
reject ends the flow. JSAP's engine supports quorum, but BKDT never used it —
all six live JSAP templates have `approval = 1` and exactly one user per stage.
**[JSAP]**

### 2.5 `workflow.workflow_user_replacements`

| Column | Notes |
|---|---|
| `old_user_id` | the configured stage user being covered |
| `new_user_id` | the stand-in. CHECK `workflow_replacement_not_self` |
| `start_date`, `end_date` | inclusive. CHECK `end_date >= start_date` |
| `reason` | |
| `is_active` | |

An `EXCLUDE USING gist` constraint (`workflow_replacement_no_overlap`) makes
overlapping windows for the same `old_user` impossible at the database level, so
"who is covering?" always has one answer.

A replacement changes the **effective** user without changing the permanent
stage assignment. See §8.

---

## 3. BKDT tables

Three tables, in the `backdate` PostgreSQL schema. **[OMS]**

```
backdate.backdate              the business document
backdate.backdate_flow         where it is now, and the SAP outcome
backdate.backdate_action_logs  what was decided or changed, by whom, when
```

### 3.1 `backdate.backdate`

**Purpose:** one request for back-posting rights.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint | NOT NULL | PK |
| `company` | varchar(64) | NOT NULL | the companies asked for, comma-separated and canonical: `OIL`, `OIL,BEVERAGES`, … See §9 |
| `sap_username` | varchar(20) | NOT NULL | the SAP login (`OUSR.USER_CODE`) the rights are granted to — **not** the OMS user asking |
| `document_type` | integer | NOT NULL | SAP object type (`MOBJ.ObjType`), e.g. 13 = A/R Invoice. The label is resolved from HANA for display and never stored |
| `from_date` | date | NOT NULL | start of the posting window (inclusive) |
| `to_date` | date | NOT NULL | end of the posting window (inclusive) |
| `time_limit` | timestamptz | **NOT NULL** | when the rights lapse in SAP. Required — see §11.3 |
| `action` | varchar(3) | NOT NULL | `A`, `U`, or `A,U` for both |
| `remarks` | text | NOT NULL (blank ok) | the requester's reason; approvers see it |
| `created_by_id` | integer | NOT NULL | FK → `users_user`. **Always** the authenticated user |
| `created_at` | timestamptz | NOT NULL | |
| `updated_at` | timestamptz | NOT NULL | |

**Primary key:** `id`.
**Foreign keys:** `created_by_id → users_user(id)`.
**Indexes:** `bkdt_creator_idx (created_by, -created_at)`, `bkdt_company_idx
(company)`.

**CHECK constraints:**

| Name | Rule |
|---|---|
| `backdate_company_valid` | `company ~ '^(OIL\|BEVERAGES\|MART)(,(OIL\|BEVERAGES\|MART))*$'` |
| `backdate_action_valid` | `action IN ('A','U','A,U')` |
| `backdate_dates_ordered` | `to_date >= from_date` |

`created_by` is never accepted from the request body. JSAP took it from the
payload, so a request could be raised in somebody else's name. **[JSAP]**

### 3.2 `backdate.backdate_flow`

**Purpose:** where one request currently is, and what SAP said. **One row per
request** (UNIQUE `backdate_id`).

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint | NOT NULL | PK |
| `backdate_id` | bigint | NOT NULL | FK → `backdate.backdate(id)`, UNIQUE |
| `status` | varchar(10) | NOT NULL | `PENDING` / `APPROVED` / `REJECTED` |
| `hana_status` | varchar(10) | NULL | `NULL` (not attempted) / `SUCCESS` / `FAILED` |
| `sap_payload` | **jsonb** | NULL | the exact parameters sent to `OPEN_BKDT`, one entry per company |
| `hana_status_text` | text | NOT NULL (blank ok) | the exact SAP response or error, per company, as JSON |
| `current_user_id` | integer | NULL | FK → `users_user`. The current responsible user — **denormalised, not the authority** (§8) |
| `workflow_id` | bigint | NOT NULL | FK → `workflow.workflows`, PROTECT |
| `current_stage` | bigint | NULL | FK → `workflow.workflow_stages(id)`. NULL once finished |
| `total_stage` | smallint | NOT NULL | stage count of the chosen workflow AT SUBMISSION, so "stage 2 of 3" stays correct after a stage is added or retired. A count, never a name |
| `created_at` | timestamptz | NOT NULL | |
| `updated_at` | timestamptz | NOT NULL | also the closest thing to "when SAP was written", because that write is the last thing to touch an approved flow. **Not** a dedicated immutable timestamp — any later change moves it |

**Indexes:** `bkdt_flow_queue_idx (status, current_user)`,
`bkdt_flow_stage_idx (current_stage)`, `bkdt_flow_workflow_idx (workflow)`.

`hana_status` has **two** values plus NULL. There is no `PARTIAL`: if any
required SAP call fails the request is `FAILED`, because the requester does not
have what they asked for. Which company failed is in `hana_status_text`.

`sap_payload` holds **business parameters only** — no connection string, no
credentials, no resolved schema name.

**Removed fields — do not reintroduce:** `current_sequence` (the stage's own
`sequence` is in `workflow_stages`), `hana_applied_at` (use `updated_at`),
`matched_query_id` (explained the selection at one instant; nothing read it
afterwards).

### 3.3 `backdate.backdate_action_logs`

**Purpose:** append-only history. Nothing in the module ever updates or deletes
a row.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint | NOT NULL | PK |
| `backdate_id` | bigint | NOT NULL | FK → `backdate.backdate(id)` |
| `action` | varchar(10) | NOT NULL | `CREATE` / `UPDATE` / `APPROVE` / `REJECT` |
| `acted_by_id` | integer | NULL | FK → `users_user` |
| `stage_id` | bigint | NULL | FK → `workflow.workflow_stages(id)` |
| `remarks` | text | NOT NULL (blank ok) | |
| `action_data` | **jsonb** | NULL | changed fields, UPDATE rows only |
| `acted_at` | timestamptz | NOT NULL | |

**Index:** `bkdt_log_backdate_idx (backdate, acted_at)`. Order history by
`acted_at, id` — never by a stored sequence.

What each action records:

| Action | `stage_id` | `action_data` |
|---|---|---|
| `CREATE` | **NULL** — creation happens before any stage runs | NULL |
| `UPDATE` | NULL — an edit is not a stage decision | the changed fields |
| `APPROVE` | the stage being approved | NULL |
| `REJECT` | the stage being rejected | NULL |

`action_data` shape — only fields that actually changed:

```json
{
  "from_date":  { "old": "2026-09-15", "new": "2026-09-20" },
  "time_limit": { "old": "2026-09-15T18:00:00", "new": "2026-09-20T18:00:00" }
}
```

Dates and timestamps are ISO strings; numbers stay numbers. Same convention as
`payments.PaymentStatusHistory.change_data`. NULL when nothing tracked changed,
so a row never claims an edit it cannot describe.

**Stage name and sequence are NOT duplicated here.** They are resolved through
`stage_id` → `workflow_stages`, so a renamed stage reads correctly in history
too. Likewise there is no `on_behalf_of`: the actor is `acted_by`, and who was
*configured* at the time is the replacement table's business.

### 3.4 There is no task table

**`backdate.backdate_approval_task` is NOT part of the final architecture**, and
neither is any generic `workflow_task`. An earlier version had one; every row
held a stage id, a sequence and a status, which is exactly what the flow row
already says about the stage that matters — the current one. Finished stages are
in the action log; future stages are configuration the engine already stores.

Current runtime state lives in **`backdate.backdate_flow`**. The pending queue
is a filter on it (§6).

---

## 4. Flow lifecycle

```
Create BackDate
      ↓
CREATE action log
      ↓
Workflow Engine selects workflow
      ↓
BackDateFlow created
      ↓
current_stage
current_user
      ↓
Approval
      ↓
next stage
      ↓
final approval
      ↓
HANA OPEN_BKDT
      ↓
SUCCESS / FAILED
```

Rejection:

```
PENDING
   ↓
REJECT
   ↓
REJECTED
```

Key properties **[OMS]**:

- The business row and its routing **commit together**. A request that cannot be
  routed is never stored. JSAP created the row first and left it orphaned when
  no template matched, silently. **[JSAP]**
- On final approval `current_stage` and `current_user` both become NULL — nothing
  is left claiming to be waiting.
- A HANA failure does **not** undo the approval: the approval is committed
  first, then SAP is called, then the outcome is recorded and reported.
- Rejection is terminal for that request. There is no resubmission; the user
  raises a new request, which re-runs selection against the CURRENT
  configuration. JSAP has no rework path either. **[JSAP]**

---

## 5. Workflow selection

One call, from `backdate/services/flow.py`:

```python
selection.select_for_module(
    module_code='BKDT',
    document_id=backdate.pk,
    company=backdate.company,     # may be 'OIL' or 'OIL,BEVERAGES'
)
```

The engine evaluates every active, validated query belonging to active
workflows of that module whose company scope applies, wrapping each as:

```sql
SELECT 1 FROM ( <your condition> ) AS wf_q WHERE wf_q."id" = %s LIMIT 1
```

Outcomes, with **no tie-breaking of any kind**:

| Matches | Result |
|---|---|
| 0 | `WorkflowNotConfigured` |
| 1 | that workflow is selected |
| >1 | `AmbiguousWorkflowSelection` |

A selected workflow with no active stages raises
`InvalidWorkflowConfiguration` rather than creating a flow that deadlocks.

Execution is isolated: a `statement_timeout` of 3000 ms, and either a dedicated
read-only connection (when a `workflow_ro` database alias is configured) or a
SAVEPOINT on the default connection. `conditions.isolation_mode()` reports
which is live.

---

## 6. Identifying pending work

There is no task table. The approver queue is:

```
BackDateFlow
      |
      +--> status = PENDING
      +--> current_stage
      +--> current_user
```

But **the queue query resolves through the engine, not through
`current_user`**. `flow_service.pending_for(user)` finds the stages whose
*effective* user is the caller today, then filters flows by `current_stage`.
That is what keeps §8 true: `current_user` is refreshed when a flow MOVES, so
a stage reassigned — or a replacement window opened — while a flow sat still
would be missed by a naive `current_user = me` query. **[OMS]**

---

## 7. Approval, rejection and editing

### 7.1 Approve

`may_act_on(user, flow)` must pass first (§10). Then, in one transaction: write
the `APPROVE` log with the stage being decided, re-read the workflow's stages as
configured **right now**, and move to the next stage by sequence — or finish.

Re-reading rather than remembering means a stage deactivated mid-flight is
skipped instead of deadlocking the request.

### 7.2 Reject

One rejection ends the flow. A reason is **required**: a missing one is a 400
(validation), not a 409 (state conflict). JSAP sent the requester nothing at all
on rejection. **[JSAP]**

### 7.3 Edit

`PATCH /api/backdate/requests/<pk>/` — the requester only, and only while
nothing has been decided. Once any stage has approved, the request is frozen:
letting the dates move underneath an approval already given would make the
record untrue.

`company` cannot be edited — it decides which workflow applies, and the request
has already been routed.

The sequence is read → apply → diff → log, in one transaction, so the log can
never describe an edit that did not commit or miss one that did.

---

## 8. User assignment and replacement

### 8.1 Permanent change

`workflow.workflow_stages.user_id` is the permanent configured stage user.

When it changes, `User A → User B`:

- the workflow is **not** duplicated
- no new flow, no new stage
- entries already at that stage stay at the same stage
- responsibility follows the current configured user immediately
- **no task migration is required** — there is nothing to migrate

This works because `flow.current_stage` holds the STAGE, and who must act is
resolved from it on every read. **[OMS]**

### 8.2 Temporary replacement

```
Configured:   Rahul
Replacement:  Amit,  15 Sep → 30 Sep
```

| When | Effective user |
|---|---|
| 15 Sep – 30 Sep | **Amit** |
| outside the range | **Rahul** |

The stage stays configured to Rahul throughout; nothing is written to BKDT when
the window opens or closes. During the window Rahul may **not** act and Amit
may. **[OMS]**

The single resolution point for all of this is
`flow_service.effective_user_id(stage_id)`, which wraps
`workflow.services.assignments.get_stage_assignment()`.

---

## 9. Multi-company behaviour

One BackDate request can cover `OIL`, `BEVERAGES` and `MART`. The selected
companies are stored **together, in one request**, comma-separated and in
canonical order (`OIL`, `BEVERAGES`, `MART` — so `BEVERAGES,OIL` and
`OIL,BEVERAGES` are the same request, not two spellings of it).

```
ONE OMS request  →  ONE flow  →  ONE workflow selection
                                      ↓
                              final approval
                                      ↓
                        ┌─────────────┴─────────────┐
                        ↓                           ↓
                   OPEN_BKDT                   OPEN_BKDT
                    (OIL)                     (BEVERAGES)
```

| Input | Requests | Flows | HANA calls | HANA rows |
|---|---|---|---|---|
| `OIL` + `A` | 1 | 1 | 1 | 1 |
| `OIL,BEVERAGES` + `A,U` | 1 | 1 | **2** | 2 |
| `OIL,BEVERAGES,MART` + `A,U` | 1 | 1 | **3** | 3 |

**Action never multiplies anything.** `A,U` is one request with a wider recorded
scope, because `OPEN_BKDT` has no ACTION parameter — so splitting it would write
SAP rows identical in every column SAP reads. JSAP stored the pair on one row
for the same reason (`action = 'A,U'`, 821 rows). **[JSAP]**

**Routing consequence.** A per-company workflow condition (`company = 'OIL'`) is
an EXACT match and will not match `OIL,BEVERAGES`. Multi-company requests need
their own workflow — see §13.

---

## 10. Permissions

| Capability | Requirement |
|---|---|
| Open the BackDate page, create/view own requests | `BackDate` |
| Open the BackDate Approval page | `BackDate_Approval` |
| **Approve / reject** | `BackDate_Approval` **AND** being the current *effective* stage user |
| Configure workflows, queries, stages, replacements | `workflow.config.manage` |

The key constants are `backdate.permissions.REQUEST_KEY` (`BackDate`) and
`APPROVAL_KEY` (`BackDate_Approval`). **[OMS]**

**Approval is enforced by the backend on every decision**, from `request.user` —
never from a client-supplied id. `may_act_on(user, flow)` returns
`(allowed, reason)`, and the two failures give different messages on purpose:
"you lack the permission" sends someone to an administrator, "this is not your
stage" does not.

Holding `workflow.config.manage` does **not** let you raise or approve a
BackDate request, and holding `BackDate_Approval` does not let you approve
something that is not yours.

> **[JSAP]** For contrast: all 22 JSAP BKDT endpoints were reachable without
> authentication, and the approver's identity came from the request body. Its
> `jsApproveDocument` did check stage membership (`THROW 50006`), so the
> exposure was impersonation of a legitimate approver rather than arbitrary
> approval.

---

## 11. SAP / HANA integration

### 11.1 `OPEN_BKDT`

Verified against the live HANA catalogue. 11 IN parameters, no OUT parameter,
**no result set**; the body is a bare `INSERT` into a per-schema `BKDT` table.
**[JSAP]**

| # | Parameter | Type | | # | Parameter | Type |
|---|---|---|---|---|---|---|
| 1 | `BRANCH` | NVARCHAR(50) | | 7 | `RIGHTS` | NVARCHAR(10) |
| 2 | `USERID` | NVARCHAR(20) | | 8 | `CREATEDBY` | NVARCHAR(20) |
| 3 | `TRANSTYPE` | INTEGER | | 9 | `CREATEDON` | TIMESTAMP |
| 4 | `FROMDATE` | DATE | | 10 | `DELETEDBY` | NVARCHAR(10) |
| 5 | `TODATE` | DATE | | 11 | `DELETEDON` | TIMESTAMP |
| 6 | `TIMELIMIT` | TIMESTAMP | | | | |

**There is no ACTION parameter.** Do not add one, and do not add `COMPANY_ID`,
`WORKFLOW_ID` or `APPROVER`.

Because the procedure returns nothing, **success means only "the call did not
raise"**. That is a property of the procedure, not an omission.

### 11.2 What OMS sends

| Parameter | Source | Note |
|---|---|---|
| `BRANCH` | the company NAME (`OIL`/`BEVERAGES`/`MART`) | one branch per call |
| `USERID` | `backdate.sap_username` | truncated to 20 |
| `TRANSTYPE` | `backdate.document_type` | real int |
| `FROMDATE` / `TODATE` | `from_date` / `to_date` | real `date` objects |
| `TIMELIMIT` | `time_limit` | **never NULL** |
| `RIGHTS` | literal `'NO'` | see below |
| `CREATEDBY` | **`str(backdate.created_by_id)`** — the REQUESTER | not the approver |
| `CREATEDON` | **`backdate.created_at`** — the REQUEST time | not the approval time |
| `DELETEDBY` / `DELETEDON` | NULL | |

`CREATEDBY` and `CREATEDON` describe the REQUEST, never the approval. Verified
against JSAP: `BackDateSaveInHana` builds its payload from
`jsGetDocumentDetailUsingFlowId`, whose `createdBy` is
`userDocument.createdBy` — the requester's numeric id — and live HANA rows carry
`createdBy='92'` for requests raised by jsUser 92 while a different user did the
approving. **[JSAP]** The approver is not lost: they are in
`backdate_action_logs`.

`RIGHTS` is always `'NO'`, which is what JSAP always sent. **[JSAP]** Whether SAP
expects something else for UPDATE is **[UNKNOWN]** — do not invent a value.

### 11.3 Why `time_limit` is mandatory

SAP's posting validator `SBO_SP_TRANSACTIONNOTIFICATION` contains **14** BKDT
lookups, and every one ends:

```sql
AND CURRENT_TIMESTAMP < r."timeLimit"
```

Against a NULL that comparison is UNKNOWN, so the row never matches: the request
reads as approved and the user still cannot post. **0 of 1,292** live JSAP rows
have a NULL here. **[JSAP]** It is enforced in the model, the serializer, the
API and the payload builder.

`createdBy` and `rights` are **not read by SAP at all** — they are audit labels.

### 11.4 Schema resolution

The schema is chosen by `hana.services.connection.Queries._schema_for_branch()`
from settings, **never from the request**. HANA cannot bind an identifier, so a
schema name is the one thing interpolated and it must come from a checked
source. Every value binds with `?`.

| Company | Setting | Test | Live |
|---|---|---|---|
| OIL | `HANA_DB_OIL_NAME` | `TEST_JIVO_OIL_HANADB` | `JIVO_OIL_HANADB` |
| BEVERAGES | `HANA_DB_BEVERAGE_NAME` | `TEST_JIVO_BEVERAGES_HANADB` | `JIVO_BEVERAGES_HANADB` |
| MART | `HANA_DB_MART_NAME` | `TEST_JIVO_MART_HANADB` | `JIVO_MART_HANADB` |

All six are schemas on one server. **Always confirm which set the environment
points at before testing.**

### 11.5 Recording the outcome

`sap_payload`:

```json
{ "calls": [
  { "branch": "OIL", "parameters": {
      "BRANCH": "OIL", "USERID": "USER02", "TRANSTYPE": 30,
      "FROMDATE": "2026-09-15", "TODATE": "2026-09-15",
      "TIMELIMIT": "2026-09-15T15:21:00+00:00", "RIGHTS": "NO",
      "CREATEDBY": "92", "CREATEDON": "2026-09-15T05:33:00+00:00",
      "DELETEDBY": null, "DELETEDON": null } } ] }
```

`hana_status_text`:

```json
{ "results": [
  { "branch": "OIL",       "status": "SUCCESS", "response": "<exact SAP response>" },
  { "branch": "BEVERAGES", "status": "FAILED",  "response": "<exact SAP error>" } ] }
```

Partial failure is recorded, not hidden: the calls are independent (SAP gives no
cross-schema transaction), so OIL can land while BEVERAGES fails. `hana_status`
is then `FAILED` for the request as a whole.

### 11.6 Retry

`POST /api/backdate/requests/<pk>/retry-hana/` — the only path to HANA that is
not a final approval. Requires `BackDate_Approval`, a fully approved flow, and
refuses a flow already at `SUCCESS`.

> **[JSAP]** Not migrated, deliberately: `SaveBKDT` (wrote arbitrary rights with
> no request, no approval and no authentication) and `UpdateHanaStatus` (let any
> caller rewrite the applied-rights flag).

---

## 12. Query translation: JSAP → OMS

**You cannot paste a JSAP condition into OMS.** JSAP's queries read
`backdate.userDocument` in **SQL Server**; OMS conditions run against OMS's
**PostgreSQL**, which has no such relation. Both were tested; both were
rejected.

### 12.1 The four things that must change

| # | JSAP | OMS | Why |
|---|---|---|---|
| 1 | `FROM backdate.userDocument` | `FROM backdate.backdate` | different server AND different table |
| 2 | `SELECT 1 …` | `SELECT id …` (or `SELECT *`) | OMS joins on the key column, so it must be exposed |
| 3 | `AND id = @id` | **delete it** | OMS binds the document id on the OUTER wrapper; the inner query must not filter by id |
| 4 | column names | see the map below | |

### 12.2 Column map

| JSAP (SQL Server) | OMS (PostgreSQL) | Note |
|---|---|---|
| `branch` = `'1'` | `company` = `'OIL'` | `1→OIL`, `2→BEVERAGES`, `3→MART` |
| `branch` = `'2'` | `company` = `'BEVERAGES'` | |
| `branch` = `'3'` | `company` = `'MART'` | |
| `toDate` | `to_date` | |
| `fromDate` | `from_date` | |
| `timeLimit` | `time_limit` | |
| `username` | `sap_username` | |
| `documentType` | `document_type` | |
| `createdBy` | `created_by_id` | **different id namespace** — a jsUser id is not an OMS user id |
| `companyId` | *(dropped)* | JSAP tenant id, not the document company |
| `id` | `id` | but see rule 3 |

### 12.3 Worked example

JSAP live template 446:

```sql
SELECT 1 FROM backdate.userDocument
WHERE createdBy != 72 AND branch = '1' AND toDate >= '2026-06-01' AND id = @id
```

becomes:

```sql
SELECT id
FROM backdate.backdate
WHERE company = 'OIL'
  AND to_date >= '2026-06-01'
```

(the `createdBy != 72` exclusion is dropped — see §14).

### 12.4 Matching a company when a request may name several

`company = 'OIL'` is an EXACT match and will **not** match `'OIL,BEVERAGES'`.
That is intended: it keeps single-company workflows meaning "only this company".

To match any request that INCLUDES a company:

```sql
WHERE ',' || company || ',' LIKE '%,OIL,%'
```

To match every multi-company request:

```sql
WHERE company LIKE '%,%'
```

**Write `%` normally.** The engine escapes literal percent signs for psycopg2
itself (`conditions._escape_percent`, applied in `matches`, `execute` and
`explain`). Never write `%%` in stored SQL.

### 12.5 What the validator enforces

Every query passes `workflow.validators.validate_query_text` plus a real
PostgreSQL `EXPLAIN` before `validated_at` is stamped:

- **SELECT/WITH only**, a single statement (an interior `;` is rejected)
- no DML/DDL keyword anywhere, including inside CTEs and subqueries —
  `insert`, `update`, `delete`, `merge`, `drop`, `alter`, `create`, `grant`,
  `copy`, `execute`, `call`, `set`, `truncate`, `vacuum`, … (34 in total)
- a function deny-list — `pg_read_file`, `dblink`, `pg_sleep`,
  `pg_terminate_backend`, `current_setting`, `query_to_xml`, `lo_import`, …
- forbidden schemas — `pg_catalog`, `information_schema`, `pg_temp`, `pg_toast`
- the **key column must be selectable**: expose `id`, or `SELECT *`

Relations are otherwise unrestricted, which is why `workflow.config.manage` is
an administrator permission.

### 12.6 Checklist

1. Change the table to `backdate.backdate`.
2. Change `SELECT 1` to `SELECT id`.
3. Delete `AND id = @id`.
4. Map every column and translate branch numbers to company names.
5. Decide exact vs membership matching (§12.4).
6. Re-check any `createdBy` filter against OMS user ids — **they do not match
   JSAP's**.
7. Paste into the Workflows page; it validates on save.

---

## 13. Configuring workflows

**Workflow configuration is managed manually through the Workflows UI.** It is
deliberately NOT a migration and NOT a management command: user ids and
configuration differ per environment, and a deploy-time script would overwrite
whatever an administrator had set.

For each workflow you need three things:

1. **Workflow** — module `BKDT`, a code, a name, a company scope.
2. **One query** — the condition (§12).
3. **At least one stage** — sequence, name, and one user who holds
   `BackDate_Approval`. A workflow with no active stages can never progress.

Use `company = ALL` on the workflow and the query when the SQL itself pins the
company; that keeps the company decision in one place instead of two.

**Conditions must be mutually exclusive.** There is no priority or
tie-breaking — two matches is an error, by design. A useful shape:

| Workflow | Condition |
|---|---|
| per company | `company = 'OIL'` — matches only single-company OIL requests |
| multi-company | `company LIKE '%,%'` — matches only requests naming several |

Those never overlap, because a comma appears exactly when more than one company
was chosen.

---

## 14. Open items — do not guess

### 14.1 `createdBy != 72` — PENDING BUSINESS DECISION

Every active JSAP BKDT condition excludes user 72. **[JSAP]** jsUser 72 is
`avtarsingh`, and **there is no OMS user with that username**. **[OMS]**

In JSAP a request raised by user 72 matches no template and is silently
orphaned with no flow. In OMS the equivalent would raise
`WorkflowNotConfigured` and the request would not be stored — louder, and
better, but a behaviour change.

Whether the exclusion is deliberate business logic (self-approval prevention),
legacy configuration or an obsolete workaround is **[UNKNOWN]**. It is
deliberately **not ported**. If it should be, add
`AND created_by_id != <the OMS user id>` to each condition.

### 14.2 The `2026-06-01` boundary

Hard-coded in SQL, exactly as JSAP has it. **[JSAP]** When the date passes,
someone must edit every condition that mentions it.

*Future improvement: replace the hard-coded policy date with explicit
configuration if the business requires it.*

### 14.3 JSAP's real approvers have no OMS accounts

JSAP's BKDT stage users are `avtarsingh` (stage 1 on all six active templates)
and `ziaulhaque` (stage 2 on the three "before 1 Jun" templates). **[JSAP]**
Neither exists in OMS. **[OMS]** Stage users must be set to real OMS accounts
holding `BackDate_Approval` before go-live.

### 14.4 `RIGHTS` for UPDATE

JSAP always sent `'NO'` and never sent the action. Whether SAP expects a
different `RIGHTS` value for an UPDATE grant is **[UNKNOWN]**.

### 14.5 Document-type list

The numeric `ObjType` values come from HANA `MOBJ` at runtime; no list exists in
JSAP source. Whether BackDate should offer all of `MOBJ` or a restricted set is
**[UNKNOWN]**.

---

## 15. API

Mounted at `api/backdate/` (and `api/v1/backdate/`). All responses use the
project `{success, message, data}` envelope. **[OMS]**

**Requester — needs `BackDate`:**

| Method | Path | Notes |
|---|---|---|
| GET POST | `/requests/` | `?status=&company=&month=MM-YYYY`. GET is scoped to the caller |
| GET PATCH | `/requests/<pk>/` | PATCH edits a pending request |
| GET | `/requests/<pk>/history/` | the action log, stage names resolved |
| GET | `/insights/` | `?company=&month=` counts |
| GET | `/sap-users/` | `?company=OIL`, cached |
| GET | `/document-types/` | `?company=OIL`, cached |

**Approver — needs `BackDate_Approval` AND the effective stage:**

| Method | Path |
|---|---|
| GET | `/approvals/queue/` `?company=` |
| GET | `/approvals/history/` `?status=&company=` |
| GET | `/approvals/insights/` `?company=` |
| POST | `/requests/<pk>/approve/` |
| POST | `/requests/<pk>/reject/` (reason required) |
| POST | `/requests/<pk>/retry-hana/` |

**Decisions are addressed by REQUEST, not by task.** There is no task table: a
request waits at one stage at a time and its flow says which, so the request id
is enough. The old `/tasks/<pk>/approve/` pair no longer exists.

---

## 16. Frontend pages

| Page | Route | Permission | Purpose |
|---|---|---|---|
| BackDate | `/BackDate` | `BackDate` | create and view requests |
| BackDate Approval | `/BackDate_Approval` | `BackDate_Approval` | approve/reject — only what this user may act on |
| Workflows | `/Workflows` | `workflow.config.manage` | workflow, query, stage and replacement configuration |

The BackDate page has two tabs (Entries, New Request), clickable KPI cards that
filter the list, and company + status filters. The approval queue only ever
contains what the server says this user may act on, so "can I see it" and "can I
decide it" are the same question — holding the key alone shows an empty queue,
which is the honest outcome rather than buttons that all 403.

The request detail shows the resolved current stage, "stage N of M", the
effective user (naming the stand-in when one is covering), the history with a
readable diff for UPDATE rows, and the per-branch SAP payload and response.

---

## 17. Notifications

`notifications.services.dispatcher.notify(...)`, via the module's
`backdate/services/notify.py`. Event types **[OMS]**:

| Event | When | Recipient |
|---|---|---|
| `BACKDATE_AWAITING_APPROVAL` | a stage opens | the stage's effective user |
| `BACKDATE_APPROVED` | final approval | the requester |
| `BACKDATE_REJECTED` | rejection | the requester |
| `BACKDATE_SUBMITTED` | *(declared, currently unused)* | |

Delivery is best-effort: a notification failure is logged and never fails the
approval.

> **[JSAP]** JSAP sent nothing at all on rejection, so a requester was never told
> their request had been refused. That is fixed here.

---

## 18. Debugging

### 18.1 Everything about one request

```sql
SELECT r.id, r.company, r.sap_username, r.document_type, r.action,
       r.from_date, r.to_date, r.time_limit, r.remarks,
       u.username AS raised_by, r.created_at,
       f.status, f.hana_status, f.total_stage,
       w.code AS workflow, s.name AS current_stage_name, s.sequence,
       cu.username AS current_user
FROM backdate.backdate r
JOIN users_user u ON u.id = r.created_by_id
LEFT JOIN backdate.backdate_flow f ON f.backdate_id = r.id
LEFT JOIN workflow.workflows w ON w.id = f.workflow_id
LEFT JOIN workflow.workflow_stages s ON s.id = f.current_stage
LEFT JOIN users_user cu ON cu.id = f.current_user_id
WHERE r.id = :id;
```

### 18.2 History, with stage names resolved

```sql
SELECT l.id, l.action, u.username AS acted_by, s.name AS stage,
       l.remarks, l.action_data, l.acted_at
FROM backdate.backdate_action_logs l
LEFT JOIN users_user u ON u.id = l.acted_by_id
LEFT JOIN workflow.workflow_stages s ON s.id = l.stage_id
WHERE l.backdate_id = :id
ORDER BY l.acted_at, l.id;
```

### 18.3 What was sent to SAP, and what SAP said

```sql
SELECT backdate_id, hana_status,
       jsonb_pretty(sap_payload) AS payload,
       hana_status_text          AS response
FROM backdate.backdate_flow
WHERE backdate_id = :id;
```

### 18.4 Did the row reach SAP?

```sql
SELECT * FROM "TEST_JIVO_OIL_HANADB"."BKDT"
WHERE "userid" = 'USER01'
ORDER BY "createdOn" DESC;
```

**Order by `createdOn`, never by `id`.** The HANA `BKDT.id` column has no
identity or default and `OPEN_BKDT` never sets it, so every row the procedure
writes has `id = NULL`. `ORDER BY id DESC` returns an unrelated older row.
**[JSAP]**

To find rows a specific OMS request produced, match on `createdOn` — it carries
the REQUEST's timestamp, which is unique enough in practice.

### 18.5 Why did routing fail?

```sql
SELECT w.code, w.company, w.is_active,
       q.name, q.query_text, q.validated_at, q.validation_error
FROM workflow.workflows w
JOIN workflow.workflow_queries q ON q.workflow_id = w.id
WHERE w.module_id = (SELECT id FROM workflow.workflow_modules WHERE code='BKDT')
ORDER BY w.code;
```

| Symptom | Likely cause |
|---|---|
| `WorkflowNotConfigured` | no condition matched — check company spelling, and whether a multi-company request has a workflow (§9, §13) |
| `AmbiguousWorkflowSelection` | two conditions match — they overlap, or an old workflow was left active |
| "has not passed validation" | `validated_at` is NULL — re-save the query |
| "could not be evaluated" | the SQL errored at runtime: usually a renamed table or column |
| approve returns 403 | either the key is missing or this is not the caller's stage — the message says which |

### 18.6 Is the flow pointing where you expect?

```sql
SELECT f.backdate_id, f.status, f.current_stage, f.current_user_id,
       s.name, s.sequence, s.user_id AS configured_user
FROM backdate.backdate_flow f
LEFT JOIN workflow.workflow_stages s ON s.id = f.current_stage
WHERE f.status = 'PENDING';
```

If `current_user_id` differs from `s.user_id`, that is **expected** after a
stage reassignment: the stored value is refreshed when the flow moves, and the
queue resolves through the engine regardless (§6).

---

## 19. Testing notes

Backend tests live in `backdate/tests/`. **`manage.py test` cannot currently run
in this environment**: the database role has neither `SUPERUSER` nor `CREATEDB`,
so the test database cannot be created ("permission denied to create
database"). Until a `CREATEDB` grant exists, verification has been done with
live-database acceptance scripts that wrap every write in a rolled-back
transaction.

When testing the HANA path, **use the `TEST_JIVO_*` schemas** and assert the
resolved schema starts with `TEST_` before executing anything. Rows written
there are durable — they are not rolled back with the Postgres transaction.
