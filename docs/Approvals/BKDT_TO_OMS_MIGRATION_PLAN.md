# BKDT / BackDate — JSAP → OMS Migration Plan

**Status:** implemented. Verified end-to-end against the live OMS database and
`TEST_JIVO_OIL_HANADB` (40/40 acceptance checks).

### Discovered during implementation

* **`OPEN_BKDT` inserts rows with `id = NULL`.** The SAP `BKDT` table's `id`
  column has no identity and no default, and the procedure does not set it. So
  rows written by the procedure cannot be ordered or addressed by `id` — match
  on values instead. Pre-existing rows in the table *do* carry ids, which makes
  `ORDER BY id DESC` silently return somebody else's row.
* **`HANAConnection.execute` returns dicts**, keyed by column name — not
  tuples. It also commits when a statement has no result set, which is what
  makes the `CALL OPEN_BKDT(...)` write durable.
* **OIL resolves to `TEST_JIVO_OIL_HANADB`** in this environment, so the SAP
  write path was proven end-to-end without touching production.

**Scope:** module functionality only. **No JSAP transactional data is imported.**
OMS BKDT starts with an empty database.

Evidence tiers are kept distinct throughout:

| Tag | Meaning |
|---|---|
| **[SRC]** | Verified from JSAP C# source |
| **[SQL]** | Verified by reading the live JSAP SQL Server catalogue (read-only) |
| **[HANA]** | Verified by reading the live HANA catalogue (read-only) |
| **[INF]** | Inferred — reasoning stated |
| **[?]** | Requires business confirmation |

---

## 1. Executive summary

BKDT grants a SAP user temporary permission to post documents with a back-dated
posting date, for one company, one document type and a bounded date range,
after an approval chain.

Three findings shaped this plan.

**The JSAP implementation is unauthenticated.** All 22 BKDT endpoints are
reachable without a session **[SRC]**, and three of them write backdate rights
straight into production HANA. Migration must not reproduce this, and the OMS
module therefore derives every identity from the authenticated user.

**BKDT fits the OMS Workflow Engine exactly.** JSAP's approval engine supports
quorum (N approvals per stage), which the OMS engine deliberately does not. Of
the six live BKDT templates, **every stage requires exactly one approval and has
exactly one assigned user** **[SQL]** — so the engine's model is faithful, not a
compromise. This was the main risk and it is closed.

**The workflow conditions are already SQL.** JSAP selects a template by running
a stored `SELECT` per candidate and taking the first that returns a row **[SQL]**.
That is precisely what `workflow_queries.query_text` does, so the six live
conditions port across as SQL rather than being reimplemented.

---

## 2. Current JSAP architecture

```
Browser ──► ItemMasterController ──► ItemMasterService ──┬──► SQL Server  [backdate].*
                                                         ├──► HANA        OPEN_BKDT
                                                         └──► NotificationService ──► FCM
```

BKDT has **no files of its own** **[SRC]**. It is embedded in the Item Master
controller/service between comment markers.

---

## 3. JSAP source files

| File | BKDT region |
|---|---|
| `Controllers/ItemMasterController.cs` | `:947`–`:1863` |
| `Services/Implementation/ItemMasterService.cs` | `:2440`–`:3320` |
| `Services/Interfaces/IItemMasterService.cs` | `:55`–`:79` |
| `Models/ItemMasterModel.cs` | `:518`–`:687` |
| `Services/Implementation/NotificationService.cs` | FCM transport |
| `Filters/CheckUserPermissionAttribute.cs` | permission filter (never applied to BKDT) |

There is no `BackDateController.cs` / `IBackDateService.cs` / `BackDateService.cs`
**[SRC]**.

---

## 4. Current business flow

```
POST /CreateDocument
      │
      ▼
[backdate].[jsCreateDocument]        one userDocument row PER BRANCH (STRING_SPLIT on ',')
      │
      ▼  (AFTER INSERT trigger)
trg_AutoCreateUserDocumentWorkflow ──► sp_CreateUserDocumentWorkflow
      │        cursor over active templates for the company;
      │        run each jsQuery (type=11) with @id → first match WINS (BREAK)
      ▼
backdate.jsFlow  status='P', currentStage=1, templateId, totalStage
      │
      ▼
POST /ApproveBKDT ──► [backdate].[jsApproveDocument]
      │                 · authorises via jsUserStage (THROW 50006)
      │                 · blocks duplicate approval (THROW 50020)
      │                 · advances stage, or sets status='A' on the last stage
      ▼
GET flow status ──► [backdate].[jsGetFlowStatus]
      │
      ├─ status != 'A' ──► stop
      └─ status == 'A' ──► BackDateSaveInHana(flowId)
                                 │
                                 ├─► HANA OPEN_BKDT   (per branch in the row)
                                 └─► [backdate].[updateHanaStatus]
```

Rejection: `[backdate].[jsRejectDocument]` → one rejection sets `jsFlow.status='R'`
**[SQL]**. No notification is sent on rejection **[SRC]**.

There is no return-to-creator / rework path **[SRC]**.

---

## 5. API inventory

22 endpoints under `/api/ItemMaster`. **None is authenticated** — there is no
`[Authorize]` on the class or any method and no global filter in `Program.cs`
**[SRC]**. Four carry a *commented-out* `[CheckUserPermission("item_master_creation", …)]`,
which even then names the **Item Master** module, not backdate.

| Verb | Route | Proc(s) | HANA | Auth |
|---|---|---|---|---|
| GET | `/GetUserDetails?company=` | — | `GETUSERDETAILS` | commented |
| GET | `/GetMobjDetails?company=` | — | `GETMOBJDETAILS` | commented |
| POST | `/SaveBKDT` | — | **`OPEN_BKDT`** | commented |
| GET | `/GetBKDTinsights` | `jsGetDocumentInsight` | — | commented |
| GET | `/GetBKDTPendingDoc` | `jsGetPendingDocuments` | indirect | none |
| GET | `/GetBKDTApprovedDoc` | `jsGetApprovedDocuments` | indirect | none |
| GET | `/GetBKDTRejectedDoc` | `jsGetRejectedDocuments` | indirect | none |
| GET | `/GetBKDTFullDetails` | all three list procs | indirect | none |
| GET | `/GetBKDTDocumentDetail` | `jsGetDocumentDetail` | indirect | none |
| GET | `/GetBKDTDocumentDetailUsingFlowId` | `jsGetDocumentDetailUsingFlowId` | indirect | none |
| POST | `/CreateDocument` | `jsCreateDocument` (+ trigger) | — | none |
| POST | `/ApproveBKDT` | `jsApproveDocument`, `jsGetFlowStatus`, … | **conditional** | none |
| POST | `/RejectBKDT` | `jsRejectDocument` | — | none |
| GET | `/GetBackDateApprovalFlow` | `jsGetBackDateApprovalFlow` | — | none |
| GET | `/GetUserDocumentInsights` | `jsGetUserDocumentInsights` | — | none |
| GET | `/GetUserDocumentsByCreatedByAndMonth` | `jsGetUserDocumentsByCreatedByAndMonth` | indirect | none |
| GET | `/GetFlowStatus` | `jsGetFlowStatus` | — | none |
| POST | `/UpdateHanaStatus` | `updateHanaStatus` | — | none |
| POST | `/BackDateSaveInHana?flowId=` | `jsGetDocumentDetailUsingFlowId`, `updateHanaStatus` | **`OPEN_BKDT`** | none |
| GET | `/GetBkdtUserIdsSendNotificatios` | `jsBackdateNotify` | — | none |
| GET | `/SendPendingBkdtCountNotification` | `jsGetActiveUsers`, `jsGetUserDocumentInsights` | — | none |
| GET | `/GetBKDTCurrentUsersSendNotification` | `GetUsersInCurrentStage` | — | none |

"indirect" = the list transform calls `GETMOBJDETAILS` to turn a numeric
`ObjType` into a label, so **every list read opens a live HANA connection** **[SRC]**.

---

## 6. SQL stored procedures

19 procedures in the `backdate` schema **[SQL]**. Two are never called by C#:
`jsGetBackDateApprovalFlowv2` and `sp_CreateUserDocumentWorkflow` (the latter is
invoked by a trigger).

Decisive bodies:

**`sp_CreateUserDocumentWorkflow`** — the selector. Cursors over
`jsTemplate ⋈ jsTemplateQuery ⋈ jsQuery` where `t.company = @company AND
t.isActive = 1 AND q.type = 11`, substitutes `@id` with the document id, executes
each query, and `BREAK`s on the first that yields `Valid = 1`. Then creates
`jsFlow` with `status='P'`, `currentStage=1`.

**`jsApproveDocument`** — authorises the approver against `jsUserStage` for the
current stage (`THROW 50006`), blocks duplicate approval (`THROW 50020`), counts
approvals against `jsApprovalCount.approval`, then advances the stage or sets
`status='A'` when `currentStage = totalStages`. Handles reject in the same
procedure via `@action`, inserting `status='R'` and setting the flow rejected.

**`jsCreateDocument`** — validates (branch/username/documentType/fromDate
required; `toDate >= fromDate`; `timeLimit >= fromDate`; `createdBy` must exist)
and inserts **one row per comma-separated branch**. It does *not* create the flow.

---

## 7. SQL data model

Module-owned **[SQL]**:

| Table | Rows | Columns |
|---|---|---|
| `backdate.userDocument` | 1,093 | `id, branch, username, documentType, fromDate, toDate, timeLimit, action, companyId, createdBy, createdOn` |
| `backdate.jsFlow` | 996 | `id, userDocumentId, status, currentStageId, templateId, totalStage, currentStage, createdOn, updatedOn, hanaStatus, hanastatusText` |
| `backdate.jsFlowStatus` | 1,335 | `id, flowId, status, stageId, templateId, userId, createdOn, description` |

Shared approval engine, in `dbo` and **also used by Budget** **[SQL]**:
`jsTemplate`, `jsTemplateQuery`, `jsQuery`, `jsStageTemplate`, `jsUserStage`,
`jsStage`, `jsApprovalCount`.

`jsUserStage` carries `mainUser` + `startTime`/`endDate` — JSAP's own temporary
delegation, equivalent to `workflow_user_replacements` **[SQL]**.

### Live workflow configuration

13 templates have been used historically; **6 are active** **[SQL]**:

| Template | Company | Stages | Condition (`@id` = document id) |
|---|---|---|---|
| 446 | OIL | 1 | `createdBy != 72 AND branch = '1' AND toDate >= '2026-06-01'` |
| 447 | BEVERAGES | 1 | `createdBy != 72 AND branch = '2' AND toDate >= '2026-06-01'` |
| 448 | MART | 1 | `createdBy != 72 AND branch = '3' AND toDate >= '2026-06-01'` |
| 452 | OIL | 2 | `createdBy != 72 AND branch = '1' AND toDate <  '2026-06-01'` |
| 453 | BEVERAGES | 2 | `createdBy != 72 AND branch = '2' AND toDate <  '2026-06-01'` |
| 454 | MART | 2 | `createdBy != 72 AND branch = '3' AND toDate <  '2026-06-01'` |

Every stage: `approvalRequired = 1`, exactly one assigned user **[SQL]**.
The six conditions are **mutually exclusive** (branch × date boundary), so
porting all six cannot produce `AmbiguousWorkflowSelection`.

---

## 8. HANA integration

Verified from the live catalogue **[HANA]**. `OPEN_BKDT`, `GETUSERDETAILS` and
`GETMOBJDETAILS` exist in `JIVO_OIL_HANADB`, `JIVO_BEVERAGES_HANADB`,
`JIVO_MART_HANADB` **and in `TEST_` variants of each** — the test schemas are the
safe target for integration testing.

`OPEN_BKDT` — 11 parameters, all `IN`, no `OUT`, no result set:

| # | Name | Type | # | Name | Type |
|---|---|---|---|---|---|
| 1 | `BRANCH` | `NVARCHAR(50)` | 7 | `RIGHTS` | `NVARCHAR(10)` |
| 2 | `USERID` | `NVARCHAR(20)` | 8 | `CREATEDBY` | `NVARCHAR(20)` |
| 3 | `TRANSTYPE` | `INTEGER` | 9 | `CREATEDON` | `TIMESTAMP` |
| 4 | `FROMDATE` | `DATE` | 10 | `DELETEDBY` | `NVARCHAR(10)` |
| 5 | `TODATE` | `DATE` | 11 | `DELETEDON` | `TIMESTAMP` |
| 6 | `TIMELIMIT` | `TIMESTAMP` | | | |

Its body is a **bare `INSERT`** into a per-schema `BKDT` table (`id, userid,
transtype, fromDate, toDate, timeLimit, rights, createdBy, createdOn, deletedBy,
deletedOn, branch`). It returns nothing and raises nothing — **"success" means
only "no exception"** **[HANA]**.

`GETUSERDETAILS` selects from `OUSR` where `USER_CODE LIKE 'USER%' OR = 'manager'`.
`GETMOBJDETAILS` selects all of `MOBJ`. Both take **zero** parameters **[HANA]**.

JSAP passes `branch` as the *name* (`"OIL"`), `transType` as an int, dates as
`DateTime.Date`, and defaults `rights` to `"NO"` **[SRC]**.

---

## 9. Notification flow

FCM HTTP v1 plus a SQL notification row **[SRC]**.

| Trigger | Recipients | Source |
|---|---|---|
| Create | current-stage users | `GetUsersInCurrentStage` |
| Approve | next-stage users | `jsBackdateNotify` |
| Scheduled | **every active user** | `jsGetActiveUsers` |
| **Reject** | **nobody** | — |

---

## 10. Current permissions

None in effect **[SRC]**. Identity is taken from the client on every endpoint:
`ApproveRequestModel.UserId` and `RejectRequestModel.UserId` from the JSON body,
`createdBy` / `userId` from body or query string. `HttpContext.User` is never
read in the controller.

The SQL procedure is the only real gate: `jsApproveDocument` refuses a user not
assigned to the stage **[SQL]**.

---

## 11. Current security gaps

| # | Gap | Consequence |
|---|---|---|
| 1 | All 22 endpoints unauthenticated | anyone reaching the host drives the module |
| 2 | `POST /SaveBKDT` writes to live HANA with no approval and no SQL record | arbitrary backdate rights in any company |
| 3 | `POST /BackDateSaveInHana?flowId=` re-applies any flow, approved or not | replay / force-apply |
| 4 | Approver identity from request body | **impersonation of a legitimate approver** (the proc blocks non-approvers, so this is the precise exposure) |
| 5 | `POST /UpdateHanaStatus` open | falsify the applied-rights audit flag |
| 6 | IDOR on every read | enumerate all requests across all companies |
| 7 | `ApproveBKDT` checks only HTTP 400/500 from the HANA step; the 404 "no rows" case falls through | returns **200 `Success=true` with no HANA write** |
| 8 | `hanaStatusText` never reassigned | always reports "HANA not triggered" |
| 9 | `updateHanaStatus` hardcodes `Status = true`, reached only on success | a failed write records nothing |
| 10 | Culture-dependent date parsing (`DateTime.TryParse`, server culture first) | `03-04-2026` is March 4 or April 3 depending on host locale |
| 11 | No transaction across branches | `"1,2"` can grant OIL and fail BEVERAGES with no rollback |
| 12 | `action` (A/U) never reaches HANA | approved ADD-vs-UPDATE scope unenforced in SAP |
| 13 | BKDT always uses *Live* HANA, ignoring `ActiveEnvironment` | a dev deployment writes to production |
| 14 | Raw `ex.Message` returned | leaks schema, host, procedure names |

Not gaps: SQL and HANA calls are properly parameterised; the only interpolated
identifier is a schema name from a hardcoded switch **[SRC]**.

---

## 12. OMS target architecture

```
OMS
├── WORKFLOW ENGINE  (exists, generic, unchanged)
│     workflow_modules · workflows · workflow_queries
│     workflow_stages  · workflow_user_replacements
│     "Which workflow applies, and what stages/users are configured?"
│
└── BKDT MODULE  (new `backdate` app, own `backdate` PG schema)
      request · flow · task · action history · HANA · notifications
      "What is the request, how does its flow run, when does OPEN_BKDT fire?"
```

The engine gains nothing BKDT-specific: no `workflow_task`, no `workflow_action`,
no BKDT columns in `workflow_modules`.

---

## 13. Workflow integration

Registration is identity only, from `backdate/apps.py` on `post_migrate`:

```python
register_module(code='BKDT', name='BackDate')
```

Submission calls the engine and builds its own runtime from the answer:

```python
selection = workflow.services.selection.select_for_module(
    module_code='BKDT', document_id=request.pk, company=request.company)
```

returning the workflow, the matched query and the ordered stages with both
configured and effective users. BKDT then creates its own `BackDateApprovalTask`.

`BackDateApprovalTask.stage_id` holds the **engine stage id**. Current
responsibility is resolved through
`workflow.services.assignments.get_stage_assignment(stage_id)` on every read —
never copied onto the task. That is what makes an administrator's stage-user
change apply to already-waiting requests with nothing migrated.

---

## 14. Permission design

Three separate capabilities:

| Key | Grants |
|---|---|
| `BackDate` | open the BackDate page; create and view own requests |
| `BackDate_Approval` | open the BackDate Approval page |
| `workflow.config.manage` | *existing* — configure workflows/stages. **Not** required to raise a request |

Approve/reject requires **both**, enforced server-side from `request.user`:

```
HasKey('BackDate_Approval')
AND get_stage_assignment(task.stage_id).effective_user_id == request.user.id
```

| User | `BackDate` | `BackDate_Approval` | Stage user | Create | Approve |
|---|---|---|---|---|---|
| A | ✔ | ✘ | ✔ | ✔ | ✘ |
| B | ✔ | ✔ | ✘ | ✔ | ✘ |
| C | ✔ | ✔ | ✔ | ✔ | ✔ |

Because the check uses `effective_user_id`, `workflow_user_replacements` works
with no BKDT-specific code.

---

## 15. BKDT data model

`backdate` PostgreSQL schema, `db_table = 'backdate"."<table>'`, schema created
by `RunSQL` in `0001_initial` — the `workflow` / `HAIS` pattern.

| Model | Purpose |
|---|---|
| `BackDateRequest` | the business document |
| `BackDateFlow` | execution state + HANA status (one per request) |
| `BackDateApprovalTask` | runtime, module-owned; references the engine `stage_id` |
| `BackDateApprovalAction` | append-only history |

### JSAP → OMS field mapping

| JSAP | Disposition | Why |
|---|---|---|
| `branch` `'1'/'2'/'3'` | **TRANSFORM** → `OIL`/`BEVERAGES`/`MART` | canonical `core.companies`; numeric codes never enter engine tables |
| `companyId` | **DROP** | JSAP tenant id, not document company |
| `username` | **KEEP** → `sap_username` | the SAP `OUSR` user being granted rights |
| `documentType` | **KEEP** as `int` | HANA `TRANSTYPE` is `INTEGER`; label is display-only |
| `fromDate`/`toDate`/`timeLimit` | **KEEP** as real date/datetime | removes the string round-trip and culture bug |
| `action` A/U | **KEEP** | and carry it through — see §21 |
| `createdBy` | **DERIVE** from `request.user` | never accepted from the client |
| `stageId`/`priority`/`assignedTo` | **REPLACE WITH ENGINE** | `workflow_stages` |
| `approvalRequired`/`rejectRequired` | **DROP** | always 1; engine has no quorum |
| `templateId` | **REPLACE WITH ENGINE** | `workflows.id` |
| `hanaStatus`/`hanastatusText` | **MOVE TO MODULE** | `BackDateFlow` |

---

## 16. API mapping

| JSAP | OMS |
|---|---|
| `/GetUserDetails`, `/GetMobjDetails` | `GET /api/backdate/sap-users/`, `/document-types/` |
| `/CreateDocument` | `POST /api/backdate/requests/` |
| the four list endpoints | `GET /api/backdate/requests/?status=&month=` |
| `/GetBKDTDocumentDetail*` | `GET /api/backdate/requests/<id>/` |
| `/GetBackDateApprovalFlow`, `/GetFlowStatus` | `GET /api/backdate/requests/<id>/history/` |
| `/ApproveBKDT`, `/RejectBKDT` | `POST /api/backdate/tasks/<id>/approve/`, `/reject/` |
| `/GetBKDTinsights`, `/GetUserDocumentInsights` | `GET /api/backdate/insights/` |
| `/SaveBKDT` | **NOT MIGRATED** |
| `/BackDateSaveInHana` | `POST /api/backdate/requests/<id>/retry-hana/` (approved flows only) |
| `/UpdateHanaStatus` | **NOT MIGRATED** — written by the module, never a client |
| the three notification endpoints | **NOT MIGRATED** — internal |

---

## 17. UI plan

Two pages, existing OMS primitives, one new sidebar section.

- **BackDate** (`/BackDate`, key `BackDate`) — request form + own-requests table
  with status filter, detail drawer showing approval history.
- **BackDate Approval** (`/BackDate_Approval`, key `BackDate_Approval`) —
  Pending / Approved / Rejected tabs, detail with history, Approve / Reject
  shown only when the backend would allow it.
- **APPROVALS** sidebar section — the existing approval pages move here from
  Orders (no duplicates), joined by BackDate Approval.

---

## 18. HANA mapping

Reuse `hana/services/connection.py` (`HANAConnection`) and its two rules: every
value binds; only a schema name is interpolated, and only from settings.

Fixes carried across:

- one request = one company → no partial multi-branch grant;
- real `date`/`datetime`/`int` binding → no `dd-MM-yyyy` round-trip, no culture bug;
- the real outcome is recorded, success or failure;
- a failed HANA write does not report approval as fully successful.

Testing targets `TEST_JIVO_*_HANADB` before live.

---

## 19. Notification mapping

Use `notifications.services.dispatcher.notify(...)`, registering handlers from
`backdate/apps.py` through `notifications/registry.py` — the inversion
`payments` already uses. Recipients come from the engine's effective stage user.
**Rejection now notifies the requester**, which JSAP never did.

---

## 20. Historical data migration

**None.** By decision, JSAP keeps its 1,093 requests / 996 flows / 1,335 actions
for reference; OMS starts empty. No importer is written, and no OMS table has a
JSAP id column.

---

## 21. Resubmission behaviour

JSAP has no resubmission and no return-to-creator path **[SRC]**. A rejected
request is terminal; the user raises a new one. OMS keeps that behaviour —
re-running selection on a new request is the engine's normal path.

---

## 22. User replacement behaviour

`workflow_user_replacements`, unchanged. The stage keeps its configured user;
only the effective actor changes, and only inside the date window. Approval
checks `effective_user_id`, so delegation needs no BKDT code.

---

## 23. Security improvements

Every gap in §11 is closed by construction: authentication required on every
endpoint; identity from JWT; approval gated on permission **and** effective
stage user; no un-approved path to HANA; status written only by the module;
reads scoped to the caller; generic error messages.

---

## 24. Testing plan

Permissions (the A/B/C matrix, server-side) · workflow selection (0 / 1 / >1) ·
company matching · stage progression · rejection · replacement before/during/after ·
permanent stage-user change with existing requests following · HANA success,
failure and retry against `TEST_JIVO_*` · notification dispatch.

> **Environment note:** `manage.py test` cannot create a test database here —
> role `mukesh` has neither `SUPERUSER` nor `CREATEDB`. Tests are written, and
> end-to-end verification uses the live-database acceptance-script approach
> already used for the Workflow Engine.

---

## 25. Rollout plan

Deploy the app (schema + module registration), grant `BackDate` /
`BackDate_Approval`, configure the six workflows through the existing Workflows
page, verify against `TEST_JIVO_*`, then enable live HANA.

---

## 26. Cutover plan

JSAP BKDT stays available read-only for history. New requests are raised in OMS
from the cutover date. No data moves; nothing is dual-written.

---

## 27. Risks

| Risk | Mitigation |
|---|---|
| Live HANA write during testing | `TEST_JIVO_*` schemas first |
| A request matching no workflow | OMS raises `WorkflowNotConfigured` and rolls back, instead of JSAP's silent orphan |
| Workflow config drifting into ambiguity | the six conditions are mutually exclusive; the engine fails loud if that stops being true |
| Test suite cannot run | see §24 |

---

## 28. Unknowns / confirmation needed

1. **[?]** `createdBy != 72` — every live condition excludes user 72, whose
   requests then match no template and silently get no workflow. Intentional or
   leftover?
2. **[?]** The `2026-06-01` boundary is hardcoded in SQL; policy changes require
   editing a query. Acceptable, or should it be configuration?
3. **[?]** Templates 307/428 ("backdate mart") have their only assigned user
   inactive — dead configuration, not ported.
4. **[?]** `action` (A/U) never reached HANA in JSAP. Should it now?
5. **[?]** Document types come from HANA `MOBJ` at runtime; no restricted list
   exists in JSAP source. Offer all of `MOBJ`, or a subset?

None blocks implementation; all are recorded rather than silently decided.

---

## 29. Exact implementation sequence

1. `backdate` app, schema, models, `0001_initial`
2. Module registration (`post_migrate`)
3. Permissions (registry, gates, routes, sidebar)
4. Workflow Engine integration (`select_for_module`)
5. Request APIs
6. Task / flow / action runtime (approve, reject)
7. HANA integration (+ retry)
8. Notifications
9. BackDate page
10. BackDate Approval page
11. Home/sidebar **APPROVALS** section
12. Tests
13. End-to-end verification
