# APPROVAL API — Generic Multi-Level Approval Engine

Developer reference for the approval endpoints. The engine is **generic**: it
serves Payment Receipts, Bank Deposits and (later) any other document type via
a `GenericForeignKey`, so nothing below is payment-specific.

Companion documents: [`PAYMENT_API.md`](PAYMENT_API.md),
[`DEPOSIT_API.md`](DEPOSIT_API.md), [`SAP_VERIFICATION.md`](SAP_VERIFICATION.md).

---

## 1. API information

| | |
|---|---|
| **Feature** | Generic approval engine |
| **Purpose** | Route any document through an ordered chain of approval levels, record every decision immutably, and fire a domain callback on the outcome |
| **Authentication** | **Required** — JWT bearer |
| **Permissions** | Approver endpoints: any authenticated user (results are filtered to what they may act on). Configuration endpoints: **admin only** (`IsApprovalAdmin`) |
| **Base path** | `/api/approvals/` |

### Endpoint summary

**Approver-facing**

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/approvals/inbox/` | Requests awaiting *this* user |
| `GET` | `/api/approvals/requests/` | All visible requests (filterable) |
| `GET` | `/api/approvals/requests/{id}/` | Detail + full action history |
| `POST` | `/api/approvals/requests/{id}/act/` | Approve / reject / cancel |

**Admin configuration**

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` `POST` | `/api/approvals/workflows/` | List / create workflows |
| `GET` `PUT` `DELETE` | `/api/approvals/workflows/{id}/` | Workflow detail |
| `GET` | `/api/approvals/workflows/{id}/preview/` | **Ladder + resolved approvers** |
| `GET` `POST` | `/api/approvals/levels/` | List / create levels |
| `GET` `PUT` `DELETE` | `/api/approvals/levels/{id}/` | Level detail |
| `GET` `POST` | `/api/approvals/levels/{id}/approvers/` | Grant named approvers |
| `GET` `PUT` `DELETE` | `/api/approvals/approvers/{id}/` | Grant detail |

---

## 2. How the engine works

### Data model

```
ApprovalWorkflow          one active per (document_type, company)
   └── ApprovalLevel      ordered by `sequence` — 1, 2, 3 …
         ├── role         FK to users.UserRole  ─┐  either or both
         └── ApprovalLevelApprover (named users) ┘

ApprovalRequest           attached to ANY document via GenericForeignKey
   └── ApprovalAction     APPEND-ONLY log, never updated
```

### "Level 2 of 3"

`total_levels` is **snapshotted onto the request at submit time**. If an admin
edits the workflow mid-flight, an in-progress request keeps the ladder it
started on — otherwise "Level 2 of 3" could silently become "Level 2 of 2" and
a document would skip an approval it was supposed to receive.

```
level_label = f"Level {current_level} of {total_levels}"
```

### State machine

```
          submit()
  DRAFT ──────────► PENDING
                      │
        approve() ────┼──► PENDING            (quorum not yet met at this level)
                      │
        approve() ────┼──► PENDING            (level cleared, current_level += 1)
                      │
        approve() ────┼──► APPROVED           (final level → fires on_approved)
                      │
        reject()  ────┼──► REJECTED           (remarks MANDATORY)
                      │
        cancel()  ────┴──► CANCELLED          (submitter only)

  REJECTED ──resubmit──► PENDING at level 1, round_number += 1
                          (prior actions KEPT, not deleted)
```

### Guard rails

| Rule | Behaviour |
|---|---|
| Self-approval | Blocked when `workflow.forbid_self_approval` (default **on**) → **403** |
| Duplicate approval | The same user cannot approve the same level twice → **400** |
| Blank reject remarks | Rejected → **400** |
| Two open chains | Prevented by a partial unique index on `(content_type, object_id)` |
| Concurrent approvers | `select_for_update()` row lock — exactly one advances |
| Level with no eligible approver | Submission fails **at submit**, not later as a deadlock |

---

## 3. Request JSON

### 3.1 Inbox

```
GET /api/approvals/inbox/?page=1&page_size=25
Authorization: Bearer <access_token>
```

Returns only requests the caller may actually act on — matched on
`(workflow, sequence)` **pairs**, so a level-2 grant in one workflow does not
expose level 2 of another. Documents the caller submitted are excluded when
self-approval is forbidden.

### 3.2 Act on a request

```
POST /api/approvals/requests/{id}/act/
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "decision": "APPROVE",
  "remarks": "Verified against the bank statement."
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `decision` | string | **Yes** | `APPROVE` \| `REJECT` \| `CANCEL` |
| `remarks` | string | **Conditional** | **Mandatory for `REJECT`.** Optional otherwise |

Reject example:

```json
{ "decision": "REJECT", "remarks": "Cheque date is stale — please re-issue." }
```

Cancel (submitter withdrawing their own document):

```json
{ "decision": "CANCEL", "remarks": "Entered against the wrong party." }
```

### 3.3 Create a workflow (admin)

```
POST /api/approvals/workflows/
Authorization: Bearer <admin_token>
Content-Type: application/json
```

```json
{
  "code": "PAYMENT_OIL_V1",
  "name": "Oil — Receive Payment",
  "document_type": "PAYMENT",
  "company": "OIL",
  "restart_on_reject": true,
  "forbid_self_approval": true,
  "is_active": true
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `code` | string | **Yes** | Unique key |
| `name` | string | **Yes** | Display |
| `document_type` | string | **Yes** | `PAYMENT` \| `DEPOSIT` \| `ORDER` |
| `company` | string | No | Blank = applies to all companies |
| `restart_on_reject` | bool | No | Default `true` |
| `forbid_self_approval` | bool | No | Default `true` |
| `is_active` | bool | No | Only **one** active per (type, company) |

### 3.4 Create a level (admin)

```
POST /api/approvals/levels/
Authorization: Bearer <admin_token>
Content-Type: application/json
```

```json
{
  "workflow": 1,
  "sequence": 1,
  "name": "Accountant Review",
  "role": 4,
  "min_approvals": 1,
  "threshold_hours": 24,
  "is_active": true
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `workflow` | int | **Yes** | FK |
| `sequence` | int | **Yes** | 1, 2, 3 … unique **within** the workflow |
| `name` | string | **Yes** | Shown in the UI and the log |
| `role` | int | No | FK to `users.UserRole`. Anyone with this role qualifies |
| `min_approvals` | int | No | Default 1. Higher = N-of-many quorum |
| `threshold_hours` | int | No | SLA before the level is considered stuck |

### 3.5 Grant a named approver (admin)

```
POST /api/approvals/levels/{level_id}/approvers/
Authorization: Bearer <admin_token>
Content-Type: application/json

{ "user": 12, "company": "OIL", "is_active": true }
```

Additive to the level's role — a user qualifies via role **or** an explicit
grant. `company` blank = the grant applies in every company.

---

## 4. Success responses

### Inbox — HTTP 200

```json
{
  "success": true, "message": "",
  "data": {
    "results": [
      {
        "id": 17,
        "workflow": 1,
        "workflow_code": "PAYMENT_OIL_V1",
        "document_type": "PAYMENT",
        "company": "OIL",
        "amount": "125000.00",
        "document_number": "RCP-OIL-2026-27-000123",
        "status": "PENDING",
        "status_display": "Pending approval",
        "current_level": 1,
        "total_levels": 2,
        "level_label": "Level 1 of 2",
        "round_number": 1,
        "submitted_by": 7,
        "submitted_by_name": "Navneet",
        "submitted_at": "2026-07-30T09:15:10.002Z",
        "level_entered_at": "2026-07-30T09:15:10.002Z",
        "decided_at": null,
        "created_at": "2026-07-30T09:15:10.002Z"
      }
    ],
    "pagination": { "page": 1, "page_size": 25, "total": 1, "total_pages": 1 }
  }
}
```

### Detail — HTTP 200

Adds the full append-only history plus a `can_act` flag for the caller:

```json
{
  "success": true, "message": "",
  "data": {
    "id": 17,
    "document_number": "RCP-OIL-2026-27-000123",
    "status": "PENDING",
    "level_label": "Level 2 of 2",
    "current_level": 2,
    "total_levels": 2,
    "can_act": true,
    "actions": [
      { "id": 40, "sequence": 1, "round_number": 1, "level": 1,
        "level_name": "Accountant Review", "action": "SUBMIT",
        "action_display": "Submitted", "remarks": "",
        "approver": 7, "approver_username": "navneet",
        "approver_role": "manager", "acted_at": "2026-07-30T09:15:10.002Z" },
      { "id": 41, "sequence": 2, "round_number": 1, "level": 1,
        "level_name": "Accountant Review", "action": "APPROVE",
        "action_display": "Approved", "remarks": "Cash counted, tallies.",
        "approver": 9, "approver_username": "goldy",
        "approver_role": "accountant", "acted_at": "2026-07-30T09:22:41.556Z" }
    ]
  }
}
```

> `ip_address` and `user_agent` are recorded on every action but are **not**
> returned by the API — they are for forensic review in the admin. The existing
> `audit` app captures neither, which is why the engine records them itself.

### Act — HTTP 200

```json
{
  "success": true,
  "message": "Request approved.",
  "data": {
    "id": 17,
    "status": "APPROVED",
    "status_display": "Approved",
    "current_level": 2,
    "total_levels": 2,
    "level_label": "Level 2 of 2",
    "decided_at": "2026-07-30T09:28:03.771Z",
    "actions": [ "…full history…" ]
  }
}
```

Intermediate approval (more levels remain) returns the same shape with
`status = "PENDING"` and `current_level` incremented.

### Workflow preview — HTTP 200

```
GET /api/approvals/workflows/1/preview/?company=OIL
Authorization: Bearer <admin_token>
```

```json
{
  "success": true, "message": "",
  "data": {
    "workflow": "PAYMENT_OIL_V1",
    "company": "OIL",
    "total_levels": 2,
    "levels": [
      { "sequence": 1, "name": "Accountant Review", "role": "accountant",
        "min_approvals": 1,
        "eligible_approvers": ["Goldy", "Randeep"],
        "eligible_count": 2, "blocked": false },
      { "sequence": 2, "name": "Manager Approval", "role": "manager",
        "min_approvals": 1,
        "eligible_approvers": [], "eligible_count": 0, "blocked": true }
    ]
  }
}
```

**Use this before going live.** `blocked: true` means no one can approve that
level, and any document reaching it would be stuck. Submission is refused up
front for exactly this reason, but the preview lets an admin see it while
configuring rather than when a payment fails.

---

## 5. Error responses

### 400 — validation

```json
{ "success": false, "message": "Invalid decision payload.", "data": null,
  "errors": { "remarks": ["Remarks are mandatory when rejecting."] } }
```
```json
{ "success": false, "message": "You have already approved this level.", "data": null }
```
```json
{ "success": false,
  "message": "This request is approved; no action possible.", "data": null }
```
```json
{ "success": false,
  "message": "No eligible approver for \"Manager Approval\". Assign a role or a user to that level first.",
  "data": null }
```

### 401

```json
{ "detail": "Authentication credentials were not provided." }
```

### 403

```json
{ "success": false,
  "message": "You cannot approve a document you submitted.", "data": null }
```
```json
{ "success": false,
  "message": "You are not an approver for \"Manager Approval\".", "data": null }
```
```json
{ "detail": "Only an administrator may configure approval workflows." }
```

### 404

```json
{ "success": false, "message": "Approval request not found.", "data": null }
```

### 409 — conflict (returned as 400)

```json
{ "success": false,
  "message": "This document already has an open approval request.",
  "data": null }
```

### 500

```json
{ "detail": "A server error occurred." }
```

---

## 6. Database changes

### `POST /requests/{id}/act/` — APPROVE, level cleared but more remain

| Table | Operation | Detail |
|---|---|---|
| `approval_action` | **INSERT** | `APPROVE`, with `sequence`, `round_number`, IP, user agent |
| `approval_request` | **UPDATE** | `current_level += 1`, `level_entered_at` refreshed |

### APPROVE — final level

| Table | Operation | Detail |
|---|---|---|
| `approval_action` | **INSERT** | `APPROVE` |
| `approval_request` | **UPDATE** | `status → APPROVED`, `decided_at` |
| *(domain hook fires — same transaction)* | | |
| `payment_receipt` / `payment_bank_deposit` | **UPDATE** | `status → QUEUED` |
| `payment_sap_outbox` | **INSERT** | The SAP post is queued |
| `payment_status_history` | **INSERT** | `→ QUEUED` |

> All of this commits **atomically**. There is no window in which a document is
> approved but not queued for SAP.

### REJECT

| Table | Operation |
|---|---|
| `approval_action` | **INSERT** — `REJECT` with mandatory remarks |
| `approval_request` | **UPDATE** — `status → REJECTED`, `decided_at` |
| `payment_receipt` / `payment_bank_deposit` | **UPDATE** — `status → REJECTED` |
| `payment_status_history` | **INSERT** |

### Resubmit after rejection

| Table | Operation | Detail |
|---|---|---|
| `approval_request` | **UPDATE** | `round_number += 1`, `status → PENDING`, `current_level = 1` |
| `approval_action` | **INSERT** | `RESUBMIT` |

**Prior actions are never deleted** — `round_number` distinguishes attempts, so
the earlier chain stays fully auditable.

### Admin configuration

| Table | Operation |
|---|---|
| `approval_workflow` | INSERT / UPDATE / DELETE |
| `approval_level` | INSERT / UPDATE / DELETE |
| `approval_level_approver` | INSERT / UPDATE / DELETE |
| `audit_log` | **INSERT** — all three are registered in `AUDITED_MODELS`, so every config change is field-level audited automatically |

**`approval_action` is never updated or deleted.** The admin exposes it
read-only.

---

## 7. SAP API called

**None directly.** The approval engine is SAP-agnostic by design — it knows
nothing about payments, deposits or the Service Layer.

What happens instead: on final approval it fires the registered domain hook,
which writes a `payment_sap_outbox` row in the same transaction. The outbox
worker (`python manage.py drain_sap_outbox`) makes the actual SAP call.

See [`PAYMENT_API.md` §8](PAYMENT_API.md#8-sap-api-called) and
[`DEPOSIT_API.md` §6](DEPOSIT_API.md#6-sap-api-called) for those payloads.

---

## 8. SAP table verification

Not applicable to this API. Approval itself writes nothing to SAP; verification
belongs to the document being approved.

The one thing worth checking after a final approval is that the outbox row was
created:

```sql
SELECT o.id, o.operation, o.status, o.attempts, o.next_attempt_at
FROM payment_sap_outbox o
JOIN approval_request a
  ON a.object_id = o.object_id AND a.content_type_id = o.content_type_id
WHERE a.id = <approval_request_id>;
```

---

## 9. SAP SQL / HANA queries

None. For OMS-side verification of the approval trail:

```sql
-- Full approval history for a document, oldest first
SELECT ac.sequence, ac.round_number, ac.level, ac.level_name,
       ac.action, ac.remarks, ac.approver_username, ac.approver_role,
       ac.ip_address, ac.acted_at
FROM approval_action ac
JOIN approval_request ar ON ar.id = ac.request_id
WHERE ar.document_number = 'RCP-OIL-2026-27-000123'
ORDER BY ac.sequence;

-- Current position
SELECT id, document_number, status, current_level, total_levels,
       round_number, submitted_by_id, submitted_at, decided_at
FROM approval_request
WHERE document_number = 'RCP-OIL-2026-27-000123';

-- Never more than ONE open request per document (must return zero rows)
SELECT content_type_id, object_id, COUNT(*)
FROM approval_request
WHERE status IN ('DRAFT', 'PENDING')
GROUP BY content_type_id, object_id
HAVING COUNT(*) > 1;

-- Who can approve level N of a workflow
SELECT l.sequence, l.name, u.username, u.name
FROM approval_level l
LEFT JOIN approval_level_approver la ON la.level_id = l.id AND la.is_active
LEFT JOIN users_user u ON u.id = la.user_id
WHERE l.workflow_id = 1
ORDER BY l.sequence;

-- Requests sitting past their level SLA
SELECT ar.id, ar.document_number, ar.current_level,
       ar.level_entered_at, l.threshold_hours
FROM approval_request ar
JOIN approval_level l
  ON l.workflow_id = ar.workflow_id AND l.sequence = ar.current_level
WHERE ar.status = 'PENDING'
  AND ar.level_entered_at < NOW() - (l.threshold_hours || ' hours')::interval;


  ---all tables in sql

  SELECT * FROM payment_receipt

SELECT * FROM payment_sap_outbox

SELECT * FROM payment_sap_call_log

SELECT * FROM payment_status_history 

SELECT * FROM branches

SELECT * FROM audit_log

SELECT * FROM payment_attachment

SELECT * FROM approval_workflow

SELECT * FROM approval_request

SELECT * FROM approval_level

SELECT * FROM approval_level_approver

SELECT * FROM approval_action

SELECT * FROM payment_method_entry

SELECT * FROM payment_sap_company_map

SELECT * FROM payment_bank_account

SELECT * FROM payment_collection_person

SELECT * FROM payment_bank_deposit

SELECT * FROM payment_cash_denomination

SELECT * FROM payment_receipt

SELECT * FROM payment_invoice_allocation

SELECT * FROM payment_bank_deposit_line

SELECT * FROM payment_status_history



```

---

## 10. Verification checklist

**Configuration**
- [ ] One active workflow exists per (document_type, company)
- [ ] Levels are numbered 1..N with no gaps
- [ ] `preview/` shows `blocked: false` on **every** level
- [ ] Creating a second active workflow for the same pair is rejected

**Submit**
- [ ] Submitting creates an `approval_request` at level 1
- [ ] `total_levels` matches the active level count
- [ ] A `SUBMIT` action row exists with IP and user agent
- [ ] Submitting a second time while one is open is rejected

**Approve**
- [ ] Inbox shows the request for an eligible approver
- [ ] Inbox does **not** show it to an ineligible user
- [ ] Approving at level 1 advances to level 2, `level_label` updates
- [ ] Approving the final level sets `APPROVED` and fires the hook
- [ ] The outbox row and `QUEUED` status appear in the same transaction

**Guard rails**
- [ ] Self-approval → **403**
- [ ] Approving the same level twice → **400**
- [ ] A non-approver acting → **403**
- [ ] Reject with blank remarks → **400**
- [ ] Reject with remarks → `REJECTED`, document also `REJECTED`

**Resubmit**
- [ ] A rejected document can be resubmitted
- [ ] `round_number` increments; `current_level` resets to 1
- [ ] Prior actions are still present in the history

**Concurrency (the important one)**
- [ ] Two approvers hit the final level simultaneously → exactly **one**
      `APPROVED`, exactly **one** outbox row

**Audit**
- [ ] `approval_action` rows are immutable in the admin
- [ ] Workflow / level / grant edits appear in `audit_log`

---

## 11. Postman collection

Variables: `base_url`, `admin_token`, `approver_token`, `submitter_token`.

### 11.1 Create a workflow (admin)

```
POST {{base_url}}/api/approvals/workflows/
Authorization: Bearer {{admin_token}}
Content-Type: application/json

{ "code": "PAYMENT_OIL_V1", "name": "Oil — Receive Payment",
  "document_type": "PAYMENT", "company": "OIL", "is_active": true }
```
Expect **201**. Save `data.id` → `{{workflow_id}}`.

### 11.2 Add level 1

```
POST {{base_url}}/api/approvals/levels/
Authorization: Bearer {{admin_token}}
Content-Type: application/json

{ "workflow": {{workflow_id}}, "sequence": 1,
  "name": "Accountant Review", "role": 4, "min_approvals": 1 }
```

### 11.3 Add level 2

```
{ "workflow": {{workflow_id}}, "sequence": 2,
  "name": "Manager Approval", "role": 2, "min_approvals": 1 }
```

### 11.4 Grant a named approver

```
POST {{base_url}}/api/approvals/levels/{{level_id}}/approvers/
Authorization: Bearer {{admin_token}}
Content-Type: application/json

{ "user": 12, "company": "OIL", "is_active": true }
```

### 11.5 Preview the ladder

```
GET {{base_url}}/api/approvals/workflows/{{workflow_id}}/preview/?company=OIL
Authorization: Bearer {{admin_token}}
```
Every level must show `"blocked": false`.

### 11.6 Inbox

```
GET {{base_url}}/api/approvals/inbox/
Authorization: Bearer {{approver_token}}
```
Save a `data.results[0].id` → `{{approval_id}}`.

### 11.7 Approve

```
POST {{base_url}}/api/approvals/requests/{{approval_id}}/act/
Authorization: Bearer {{approver_token}}
Content-Type: application/json

{ "decision": "APPROVE", "remarks": "Cash counted, tallies." }
```
Expect **200**.

### 11.8 Reject (negative test)

```
POST {{base_url}}/api/approvals/requests/{{approval_id}}/act/
Authorization: Bearer {{approver_token}}
Content-Type: application/json

{ "decision": "REJECT", "remarks": "" }
```
Expect **400** — blank remarks.

### 11.9 Self-approval (negative test)

Approve using `{{submitter_token}}` — expect **403**.

### 11.10 History

```
GET {{base_url}}/api/approvals/requests/{{approval_id}}/
Authorization: Bearer {{approver_token}}
```
`data.actions[]` must show every step in order.

---

## 12. Common issues

| Symptom | Cause | Fix |
|---|---|---|
| **400 "No active approval workflow for PAYMENT / OIL"** | Not configured | Create one and set `is_active` |
| **400 "Workflow X has no active levels"** | Workflow exists but has no levels | Add at least one level |
| **400 "No eligible approver for …"** | Level has neither a role nor a grant, or nobody holds the role | Use `preview/` to find the blocked level; assign a role or user |
| **400 "This document already has an open approval request"** | Double submit | Check `approval_request`; a partial unique index blocks a second open chain |
| **400 "You have already approved this level"** | Same user acting twice | Another approver is needed, or raise `min_approvals` deliberately |
| **400 "This request is approved; no action possible."** | Acting on a closed request | Refresh the inbox |
| **403 "You cannot approve a document you submitted."** | Self-approval | Have someone else approve, or set `forbid_self_approval: false` |
| **403 "You are not an approver for X"** | Role mismatch or missing grant | Check the user's role and `approval_level_approver` |
| **403 "Only an administrator may configure…"** | Non-admin hitting a config endpoint | Use an admin token |
| **Inbox is empty though the user should see items** | Grants point at a different workflow's level | Matching is on `(workflow, sequence)` pairs — check the level belongs to the right workflow |
| **Approved but nothing queued for SAP** | Hooks not registered | `PaymentsConfig.ready()` calls `hooks.register()`; confirm `payments` is in `INSTALLED_APPS` |
| **`level_label` shows "of 0"** | `total_levels` never snapshotted | The request predates submit; resubmit it |
| **Two approvals recorded for one level** | Expected when `min_approvals > 1` | Check the level's quorum setting |
