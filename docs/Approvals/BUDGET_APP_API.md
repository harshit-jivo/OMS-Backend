# Budget approval — API for the OMS mobile app

What the app needs to let budget approvers decide from their phone, the same as on the web
(`/Budget_Approval`). Base path: **`/api/budget/`**. The app's normal OMS login token works as-is;
no user id is ever sent — the server acts as the signed-in user.

## What the feature is (one paragraph)

Someone saves an expense document in SAP (A/P invoice, payment, journal voucher…). SAP keeps it as
a **draft** and refuses to post it until its **budget owner approves**. OMS takes the draft in, splits
it by budget head into **items**, and each item waits at a workflow stage for one person. Approving
the last stage tells SAP the lines may post; rejecting blocks them. **One draft can have several
items** (e.g. a Factory part and an Electricity part), each with its own approver.

## Who sees what

| Permission key | Gives |
|---|---|
| `Budget_Approval` | the approval desk: your queue, your decisions, opening drafts |
| `Budget_Reports` | everyone's items (read-only) — web only for now |

Holding `Budget_Approval` only opens the desk. **Approving an item also needs being its current
stage's user today** (stand-ins included); the server checks it on every decision. Use
`item.can.approve` / `item.can.reject` to show the buttons — never decide that on the phone.

**Never show budget amounts or "remaining budget"** to approvers — a business rule. The API does not
send them.

## Responses

Every JSON response is `{ "success": bool, "message": str, "data": … }`. On failure `success` is
false and `message` is a sentence written for the user — show it as-is.

| Status | Means |
|---|---|
| 403 | not your item / no permission |
| 404 | not found |
| 409 | refused by a rule: already decided, or **"It changed since you opened it. Reload it and try again."** (stale `version`) |
| 503 | SAP or the attachment service could not be reached |

## Endpoints

### Lists

| Call | Returns |
|---|---|
| `GET queue/` | items waiting for **me** today, oldest first — `[Item]` |
| `GET history/` | items I approved or rejected — `[Item]` |

### One item / one draft

| Call | Returns |
|---|---|
| `GET items/<id>/` | one `Item` in full (with `stages` and `logs`) — **what a notification opens** |
| `GET drafts/<draft_id>/` | the draft + all its items in full: `{…Draft, items: [Item]}` |
| `GET drafts/<draft_id>/attachments/` | `[{line, file_name, date}]` — the draft's files in SAP (may be empty) |
| `GET drafts/<draft_id>/attachments/<line>/` | the file itself (PDF/image), `Content-Disposition: inline`; needs the auth header, so download it, don't hand the URL to a browser |

### Deciding

| Call | Body | Notes |
|---|---|---|
| `POST items/<id>/approve/` | `{"remarks": "", "version": 4}` | remarks optional |
| `POST items/<id>/reject/` | `{"remarks": "Not budgeted", "version": 4}` | **remarks required** |
| `POST items/approve-bulk/` | `{"items": [{"id": 1, "version": 4}, …], "remarks": ""}` | max 100; each item decided on its own |

**Always send the `version` you displayed.** If someone else moved the item meanwhile, the server
answers 409 and the app should reload it.

Approve/reject answer `{success, message, data: Item}` — show `message` (it says e.g. "Approved. It
now waits at Director Approval." or that SAP could not be reached but the approval stands).

Bulk answers `data: {approved: n, refused: n, results: [{id, ok, approved, message}]}` — show the
refused ones with their `message`.

Rejection is final for that version of the draft. If the creator edits the draft in SAP, it comes
back as a new item on its own.

## Shapes

**Item**

```json
{
  "id": 427, "company": "OIL", "route": "TRANSPRT", "budget_code": "Transprt",
  "amount": "27143.00", "status": "PENDING", "status_label": "Pending",
  "version": 4,
  "workflow": "BUD_OIL_TRANSPRT", "current_stage": "Budget Owner Approval",
  "current_user": {"id": 26800, "name": "Paramdeep Singh", "username": "paramdeep"},
  "total_stage": 1, "waiting_since": "2026-10-06T09:12:00+00:00",
  "sap_status": null, "sap_status_text": "", "sap_written_at": null,
  "draft": { …Draft },
  "lines": [{"line_num": 0, "acct_code": "5650015", "acct_name": "Freight", "budget_code": "Transprt",
             "sub_budget_code": "", "effect_month": "Oct", "amount": "17319.38", "remarks": ""}],
  "can": {"approve": true, "reject": true, "retry_sap": false},
  "stages": [{"stage_id": 9099, "name": "Budget Owner Approval", "sequence": 1,
              "state": "CURRENT", "user_name": "paramdeep", "acted_at": null}],
  "logs": [{"action": "SYNC", "label": "Taken in from SAP", "actor": null, "stage": "",
            "remarks": "…", "data": null, "at": "2026-10-06T06:42:03+00:00"}]
}
```

`stages` and `logs` come only from `items/<id>/` and `drafts/<id>/`.
`status`: `PENDING`, `APPROVED`, `REJECTED`, `GONE` (posted/deleted in SAP before a decision),
`SUPERSEDED` (draft changed in SAP; replaced by a new item).
Stage `state`: `CURRENT`, `UPCOMING`, `APPROVE`, `AUTO_APPROVE`, `REJECT`.

**Draft**

```json
{
  "id": 912, "company": "OIL", "obj_type": 18, "obj_type_label": "A/P invoice",
  "draft_entry": 51588, "doc_num": null, "doc_date": "2026-10-01",
  "card_code": "V001", "card_name": "Hindustan Petroleum Corporation Limited",
  "sap_created_by": "manager", "comments": "", "status": "PENDING",
  "synced_at": "…", "created_at": "…"
}
```

Show a draft as `"{obj_type_label} {doc_num or draft_entry}"`, e.g. "A/P invoice 51588" — that is
how the web app labels it.

## Push notifications

Sent through the existing `notifications` framework, so they already arrive on the app with the
standard payload:

```json
{"notification_id": 991, "event_type": "BUDGET_AWAITING_APPROVAL",
 "title": "Budget approval waiting for you",
 "message": "A/P invoice 51588 · OIL · Transprt · Hindustan Petroleum… · ₹27,143.00 — Budget Owner Approval",
 "entity_type": "budgetitem", "entity_id": 427, "company_id": 1, "screen": "notification"}
```

| `event_type` | Sent to | When |
|---|---|---|
| `BUDGET_AWAITING_APPROVAL` | the next stage's user | an item moved on to them |
| `BUDGET_NEW_ITEMS` | each approver, once per sync | new drafts came in from SAP (one message, however many items) |
| `BUDGET_PENDING_REMINDER` | each approver with anything waiting | once a day |
| `BUDGET_APPROVED` / `BUDGET_REJECTED` | people who approved earlier in the chain | the item finished |

**Routing: every `entity_type: "budgetitem"` opens item `entity_id`** — call `GET items/<entity_id>/`
and show it, with Approve/Reject only if `can.approve`. (Summary messages point at the approver's
oldest waiting item.) The web app does exactly this: `OMS-Frontend/src/utils/notificationRouting.ts`
→ `/Budget_Approval?itemId=<id>`.

## Suggested screens

1. **Queue** — `GET queue/`: draft label, party (`draft.card_name`), budget head, amount, waiting
   since. Multi-select → bulk approve.
2. **Item** — `GET items/<id>/`: header, lines, stages, history, attachments
   (`drafts/<draft.id>/attachments/`), Approve / Reject (reason required).
3. **Decided** — `GET history/`.

Reference implementation (web): `OMS-Frontend/src/pages/Budget_Approval.tsx`,
`OMS-Frontend/src/services/budgetService.ts`. Backend: `OMS-Backend/budget/views.py`.
