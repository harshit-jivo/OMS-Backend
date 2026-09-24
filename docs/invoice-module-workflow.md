# OMS-Backend `invoice` Module — Deep Notes

`invoice` is the **sales-invoice review desk**. A biller builds an invoice against
open sales orders, and instead of going straight to SAP it lands here as a
`PENDING` row. A reviewer approves or rejects it, and only then is it posted to
SAP B1. This module owns that queue, its audit trail, the credit-limit escape
hatch when SAP blocks a post, and the bill print.

It does **not** talk to SAP's Service Layer itself. The post is made by
[serviceLayer](serviceLayer-module-workflow.md) (`POST /api/service-layer/invoice/`);
`invoice` records the decision and the outcome.

Mounted in [OMS/urls.py](../OMS/urls.py) at **both** prefixes — `/api/invoice/`
(unversioned, what the SPA calls) and `/api/v1/invoice/` (namespaced `v1:`, what
the published OpenAPI schema describes). Same views, same behaviour.

## 1) Files

| File | Role |
| --- | --- |
| [invoice/models.py](../invoice/models.py) | 4 tables: `invoice_log`, `invoice_history`, `invoice_ref_logs`, `credit_limit_logs` |
| [invoice/serializers.py](../invoice/serializers.py) | DTOs + the derived read-only fields the review screen needs |
| [invoice/views.py](../invoice/views.py) | 11 views behind 14 routes |
| [invoice/urls.py](../invoice/urls.py) | Route map |
| [invoice/services/fg_stock.py](../invoice/services/fg_stock.py) | Live HANA on-hand stock per payload line |
| [invoice/services/item_names.py](../invoice/services/item_names.py) | Item code → product name, resolved per branch |
| [invoice/services/jsap_db.py](../invoice/services/jsap_db.py) | Read-only JSAP SQL Server lookup for the CL approval flow id |
| [invoice/tests.py](../invoice/tests.py) | JSAP config safety + "a status change records who made it" |

Frontend counterparts: [SalesInvoice](../../Frontend/src/pages/SalesInvoice/)
(builds and submits) and [invoiceReview](../../Frontend/src/pages/invoiceReview/)
(approves, rejects, posts, prints).

---

## 2) The four tables

### 2.1 `invoice_log` (`InvoiceLog`) — the live queue

One row per invoice attempt. This is the row a reviewer acts on.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | PK | |
| `so_number` | char(100) | **Comma-joined** DocNums — one invoice can draw from several SOs (`"512345, 512346"`) |
| `party_name` | char(255) | Customer name (display copy; `CardCode` lives in the payload) |
| `total_amount` | decimal(10,2) | Grand total |
| `branch` | char(25) null | `OIL` / `BEVERAGE` / `MART` — which company DB |
| `warehouse` | char(25) null | WHS code of the payload's **first** line |
| `status` | char(20) | See §3 |
| `rejection_reason` | text null | Required when status becomes `REJECTED` |
| `error_message` | text null | Latest SAP failure text |
| `invoice_payload` | jsonb | The **exact** SAP Service Layer body (§4) |
| `sap_doc_num` | char(50) null | Visible invoice number, stamped on a successful post |
| `sap_doc_entry` | char(50) null | Internal `OINV` key — what Crystal actually renders from |
| `supersedes` | FK self null | The rejected log this one reworks |
| `created_at` / `created_by` | ts / FK user | |
| `is_deleted`, `deleted_at`, `deleted_by`, `delete_reason` | soft delete | `deleted_by` is `SET_NULL` so removing a user doesn't take the rows with it |

Two things on the model are worth knowing before you touch it:

- **`has_sap_document`** — `bool(sap_doc_num or sap_doc_entry)`. This is the real
  invariant behind "can this be deleted"; `DELETABLE_STATUSES` is only a proxy
  for it. A post that stamped the identifiers but failed to save the status would
  slip past the status list, not past this.
- **`revision_chain()`** — this log plus every earlier version via `supersedes`,
  oldest first, cycle-guarded. The history endpoint returns the timeline of the
  whole chain, not just one row.

`save()` backfills text nobody supplied: `REJECTED` with no reason gets
`error_message = "Invoice rejected without specific reason."`, and `ERROR` with
no message gets a generic one.

### 2.2 `invoice_history` (`InvocieHistory`) — the append-only trail

> The class name is misspelled (`InvocieHistory`) and so is nothing else — the
> `db_table` is the correctly spelled `invoice_history`. Don't "fix" the class
> name casually; it is referenced in views, serializers, tests and migrations.

**Never updated, only inserted.** Every state change appends a full snapshot, so
the log's current row can be overwritten freely without losing what it used to
say. Fields mirror `invoice_log` — `so_number`, `party_name`, `total_amount`,
`status`, `rejection_reason`, `error_message`, `invoice_payload` — plus:

| Column | Notes |
| --- | --- |
| `invoice_log` | FK, `related_name='history'`, nullable |
| `created_by` | **CharField, not an FK** — a username string, so the trail survives a deleted account |
| `created_at` | auto |
| `device_id`, `device_name` | Client-reported telemetry; corroborating evidence only, not proof |

`rejection_reason` and `error_message` are archived here deliberately: a
reviewer's reason survives even after the live log's copy is cleared or
overwritten by the next attempt.

**Two statuses exist only in history, never on the log:** `DELETED` and
`RESTORED`. When an entry is soft-deleted, the log keeps its real status
(`PENDING`, `ERROR`, …) — that is what it *was* — and the removal is recorded as
a history row instead, with the delete reason in the `rejection_reason` field.

History rows are written by **five** call sites: create, the retire half of an
edit, status update, delete, and restore. `UpdateInvoiceLogView.perform_update`
is the sixth (a PUT/PATCH on the payload itself).

The device columns have a churned migration history — 0019 added them, 0022
dropped them, 0024 put them back — because `views.py` passes
`**describe_request_device(request)` on write. Removing the fields while those
call sites stand raises `TypeError: InvocieHistory() got unexpected keyword
arguments: 'device_id', 'device_name'`.

### 2.3 `invoice_ref_logs` (`InvoiceRefLogs`) — SAP draft references

A separate, older trail for **drafts** stamped with an OMS ref number in
`ODRF.U_OMS_REF`. Columns: `ref_id`, `card_name`, `doc_date`, `so_number`,
`status`, `error_message`, `posted_by` (FK), `posted_at`. Unrelated to the
review queue; see §5.6 for why it exists.

### 2.4 `credit_limit_logs` (`CreditLimitLogs`)

One credit-limit request per invoice, enforced structurally:
`invoice_log` is both the FK **and the primary key**. Also `jsap_doc_id` (the
`creditDocumentId` JSAP returned), `party_name`, `created_at`, `created_by`.

---

## 3) Status lifecycle

```
                  ┌──────────────── POST /pending/ ────────────────┐
                  ▼                                                │
              PENDING ──approve──► APPROVED ──post to SAP──► POSTED_TO_SAP
                  │                                  │
                  │                                  └──fail──► ERROR
                  │                                                │
                  └──reject──► REJECTED ──edit+resubmit──► EDITED  │
                                   (new log: supersedes = old id)  │
                                                                   │
                        ERROR (credit limit) ──raise CL──► CL_RAISED
```

`STATUS_CHOICES`: `PENDING`, `APPROVED`, `REJECTED`, `EDITED`, `ERROR`,
`POSTED_TO_SAP`, `CL_RAISED`.

**There is no server-side transition table.** `PATCH /<id>/update-status/`
accepts any valid status from any other, and the ordering above is what the
review screen offers, not what the backend enforces. The guards that *are*
enforced: a deleted log rejects any status change (409), and `REJECTED` requires
a reason (400).

Three status groupings drive behaviour, and the differences between them are
deliberate:

| Set | Members | Used for |
| --- | --- | --- |
| `DELETABLE_STATUSES` | PENDING, ERROR, REJECTED, EDITED, CL_RAISED, **APPROVED** | Can be cleared off the screen. `APPROVED` is in, `POSTED_TO_SAP` is out — the difference is whether a real SAP document exists |
| `UsedSalesOrdersView.BLOCKING_STATUSES` | + POSTED_TO_SAP, − REJECTED | An SO already carried by one of these shouldn't be invoiced again |
| `ReservedBatchesView.HOLDING_STATUSES` | PENDING, APPROVED, EDITED, ERROR, CL_RAISED | Batch stock held pre-SAP. `POSTED_TO_SAP` is **absent** — SAP has already taken that stock out of `OIBT`, so holding it here too would subtract the same pieces twice |

A `REJECTED` or soft-deleted log **releases** its batches and its SOs: that
invoice isn't going to post, so the stock is free again.

---

## 4) `invoice_payload` — the shape

Built by `buildInvoicePayload` in
[salesInvoice.utils.ts](../../Frontend/src/pages/SalesInvoice/salesInvoice.utils.ts)
and stored **verbatim**. It is the record of what went to SAP, so nothing in this
module ever rewrites it — derived data (item names, stock) travels *beside* it in
the response.

```json
{
  "DocObjectCode": "13",
  "Series": 0,
  "CardCode": "CUSTA000512",
  "DocDate": "2026-09-18",
  "DocDueDate": "2026-10-18",
  "TaxDate": "2026-09-18",
  "NumAtCard": "PO-4471",
  "SalesPersonCode": 17,
  "ShipToCode": "SHIP-01",
  "PayToCode": "BILL-01",
  "BPL_IDAssignedToInvoice": 3,
  "DocumentLines": [
    {
      "LineNum": 0,
      "BaseType": 17,
      "BaseEntry": 88231,
      "BaseLine": 0,
      "ItemCode": "FG0000123",
      "Quantity": 240,
      "WarehouseCode": "DL-MP",
      "TaxCode": "GST18",
      "ShipDate": "2026-09-18",
      "U_SchemeAgst": "OLIVE",
      "BatchNumbers": [
        { "BatchNumber": "B2609A", "Quantity": 120 },
        { "BatchNumber": "B2609B", "Quantity": 120 }
      ]
    }
  ],
  "DocumentAdditionalExpenses": [
    { "ExpenseCode": 1, "LineTotal": 1500, "VatGroup": "GST18" }
  ]
}
```

Notes that cost real debugging time:

- **`LineNum` is the position in *this* invoice**, not in the source order. An
  invoice drawn from two SOs would otherwise get `0,1,2` then `0,1`, and SAP
  cannot tell which `BatchNumbers` block belongs to which line —
  *"Cannot add row without complete selection of batch/serial numbers."*
  `BaseLine` still carries the source order's line number.
- **`U_SchemeAgst` must be on every line, and it is a profit-centre CODE.**
  SAP's transaction validation rejects an invoice line with it blank —
  *"(1310325) Please Select the SchemeAgst Column"*. On every posted line it
  equals the costing code (`OPRC.PrcCode`: `SUNFLOWR`, not `SUNFLOWER`).
  `/api/hana/so/` returns it per order line as the order line's own value,
  else the line's `OcrCode`, else the dimension-1 OPRC code for the item's
  sub-group; `/api/hana/fg-items/` returns the same OPRC code per item for
  item-sourced lines. The frontend passes it through, so a sales order keyed
  without the field still invoices.
- `BaseType: 17` + `BaseEntry`/`BaseLine` link the line to its sales order. A
  free-text (non-SO) line omits all three and carries `UnitPrice` instead.
- `DocObjectCode: "13"` is SAP's A/R Invoice.

---

## 5) Endpoints — `/api/invoice/`

Every view is explicitly `permission_classes = [AllowAny]`. That is not an
oversight: it was audited in phase 2.4 and made explicit across the module. Two
consequences you must know about — see §7.

| Method + path | View | Purpose |
| --- | --- | --- |
| `POST /pending/` | `InvoiceLogCreateView` | Submit an invoice for review |
| `GET /all/?whs=` | `InvoiceLogListView` | Review queue for one warehouse |
| `GET /logs/all/` | `InvoiceLogListwoWhsView` | Same, all warehouses |
| `GET /history/<pk>/` | `InvoiceHistoryView` | Full timeline of the revision chain |
| `PATCH /<pk>/update-status/` | `InvoicelogStatusUpdateView` | Approve / reject / record SAP outcome |
| `DELETE /<pk>/delete/` | `InvoiceLogDeleteView.delete` | Soft-delete off the screen |
| `POST /<pk>/delete/` | `InvoiceLogDeleteView.post` | Restore a soft-deleted entry |
| `GET·PUT·PATCH /log/<id>/` | `UpdateInvoiceLogView` | Retrieve / edit one log |
| `POST /refLogs/` | `InvoiceRefLogCreateView` | Log an SAP draft ref, HANA-verified |
| `GET /credit-limit/cards/` | `CreditLimitCardsView` | DSR customer balance/limit cards |
| `POST /credit-limit/request/` | `CreditLimitRequestView` | Raise a CL request in JSAP |
| `GET /credit-limit/flow/` | `GetCreditLimitJSAPFlow` | Approval stages of that request |
| `GET /crystal/` | `GetPrintReport` | Bill print PDF |
| `GET /used-sales-orders/` | `UsedSalesOrdersView` | SOs already on an in-flight log |
| `GET /reserved-batches/` | `ReservedBatchesView` | Batch qty held by in-flight logs |

### 5.1 `POST /pending/` — submit for review

```json
{
  "so_number": "512345, 512346",
  "party_name": "SHREE TRADERS",
  "total_amount": 184320.50,
  "status": "PENDING",
  "branch": "OIL",
  "warehouse": "DL-MP",
  "created_by": 42,
  "edited_from": 118,
  "invoice_payload": { "...": "see §4" }
}
```

`edited_from` is **not a model field** — it is popped off the body before the
serializer sees it, and acted on only after the replacement is saved, inside one
transaction:

1. Save the new log, `created_by=request.user`.
2. Append a history row for it.
3. `_close_edited_source(edited_from)`: lock the source row; **only if** it is
   genuinely `REJECTED` and not deleted, flip it to `EDITED` and append a
   history row for it. Its `rejection_reason` is left in place — that is the
   record of why the invoice was reworked.
4. Set `supersedes` on the new log to the retired one.

An edit that is started and abandoned therefore leaves the original `REJECTED`
and visible. Lineage is **never** taken from the request body — `supersedes` is
`read_only` on the serializer and set only after that status check passes.

**201** with the full serialized log (including `fg_stock`, `item_names`,
`can_delete`).

### 5.2 `GET /all/?whs=DL-MP&status=PENDING&include_deleted=false`

`whs` is **required** (400 without it). `status` and `include_deleted` are
optional; soft-deleted rows are excluded unless `include_deleted` is
`1|true|yes`.

The response is a plain array. Each row is the model plus derived fields:

```json
[
  {
    "id": 214,
    "so_number": "512345, 512346",
    "party_name": "SHREE TRADERS",
    "total_amount": "184320.50",
    "branch": "OIL",
    "warehouse": "DL-MP",
    "status": "PENDING",
    "rejection_reason": null,
    "error_message": null,
    "invoice_payload": { "...": "..." },
    "sap_doc_num": null,
    "sap_doc_entry": null,
    "created_at": "2026-09-18T11:04:22.117Z",
    "created_by": 42,
    "is_deleted": false,
    "deleted_by_name": null,
    "supersedes": 118,
    "supersedes_so_number": "512345",
    "supersedes_status": "EDITED",
    "supersedes_rejection_reason": "Wrong ship-to address",
    "superseded_by_id": null,
    "can_delete": true,
    "item_names": { "FG0000123": "JIVO CANOLA 1L 20 PCS" },
    "fg_stock": [
      {
        "line_num": 0,
        "item_code": "FG0000123",
        "item_name": "JIVO CANOLA 1L 20 PCS",
        "quantity": 240.0,
        "warehouse_code": "DL-MP",
        "warehouse_stock": 5820.0
      }
    ]
  }
]
```

Three things this endpoint does carefully:

- **The queryset is resolved to a list once** (`list(invoice_logs)`), so the
  stock and name maps are built for the whole page — **one** HANA query per
  (branch, warehouse) pair and **one** Postgres query for all names, not one per
  row. `select_related('supersedes')` + `prefetch_related('superseded_by')` keep
  the lineage fields from costing a query each.
- **A HANA outage does not take the list down.** `build_fg_stock_map` logs and
  skips the failing pair; `fg_stock` then serializes as `null` / empty.
  `warehouse_stock` is `None` rather than `0` when the item has no `OITW` row —
  "not stocked here" stays distinguishable from "stocked, empty".
- **Item names are resolved per branch, not per code.** The synced
  `sap_sync.Product` catalogue holds one row per `(item_code, category)`, and
  ~614 of ~1770 codes carry a *different* name in each — `CG0000001` is "PREFORM
  SAMPLE" under OIL, "TAPE ROLL" under MART. Matching on code alone would show
  the wrong product on an audit screen, so the log's `branch` supplies the
  category order (`OIL → (OIL, MART)`, `BEVERAGE → (BEVERAGES,)`).

`GET /logs/all/` is the same thing without the warehouse requirement.

### 5.3 `PATCH /<pk>/update-status/`

```json
{ "status": "APPROVED" }
```
```json
{ "status": "REJECTED", "rejection_reason": "Batch B2609A already invoiced" }
```
```json
{ "status": "POSTED_TO_SAP", "sap_doc_num": "1204417", "sap_doc_entry": "88431" }
```
```json
{ "status": "ERROR", "error_message": "(13000316) Credit Limit Exceeded! Current Limit is 10.00, Balance Amount is 5,379.00" }
```

Order of operations: 404 if missing → **409 if `is_deleted`** → 400 on an
unknown status → 400 if `REJECTED` without a reason → apply fields → append
history → save.

`error_message` is recorded **whatever the target status**. A failed repost is
worth logging even when the status doesn't become `ERROR`: a credit-limit
rejection on an invoice with a request already in flight stays `CL_RAISED`, but
the reviewer still needs to see what SAP said on the latest attempt.

Returns `{"message": "Status updated successfully"}` — **not** the updated row.

**Who gets recorded.** `_history_actor(request)` prefers
`request.user.get_username()` and falls back to `request.data['user']` only for
an unauthenticated caller — a name such a caller declares about itself is worth
keeping as a hint and nothing as proof, which is exactly why it must not
override a real identity. `None` when there is neither, so "nobody knows" stays
distinct from the literal string `AnonymousUser`.

This path previously read the actor from the body alone, and no client has ever
sent that key — so per the code's own measurement, 369 live history rows (192
`POSTED_TO_SAP`, 162 `ERROR`, 9 `APPROVED`, 6 `CL_RAISED`) carry a NULL actor.
Those are **not recoverable**: `audit.AuditLog` does not cover `/api/invoice/`
paths (`audit/pages.py:_PATH_RULES`), so nothing else in the system recorded who
made those decisions.

### 5.4 `DELETE /<pk>/delete/` and `POST /<pk>/delete/`

```json
{ "delete_reason": "Duplicate of #212" }
```

Soft delete only — nothing is erased. The row is stamped
(`is_deleted/deleted_at/deleted_by/delete_reason`), its history is untouched, and
a `DELETED` history row is appended so the removal is itself part of the trail.
The log's own `status` is **left alone**: the entry was `PENDING` when it was
removed and that is what the record should say.

- **Idempotent** — deleting an already-deleted row returns 200, not an error, so
  a double-click or retry doesn't read as a failure.
- **409** for a status outside `DELETABLE_STATUSES`, with the allowed list in
  `detail`.
- `POST` to the same path is the undo: clears the stamps and appends a
  `RESTORED` history row.

`can_delete` on the serializer mirrors this view exactly, including the
SAP-document guard — offering a button the endpoint then refuses with a 409 is
the worse of the two failures.

### 5.5 `GET /history/<pk>/`

Returns the timeline of the **whole revision chain**, not one row: `chain_ids =
[log.pk for log in invoice_log.revision_chain()]`, then every `invoice_history`
row for those ids ordered by `created_at, id`. A reworked invoice reads as one
continuous timeline ending at this log, instead of a stump that starts after the
rejection.

```json
[
  {
    "id": 901,
    "invoice_log": 118,
    "so_number": "512345",
    "party_name": "SHREE TRADERS",
    "total_amount": "184320.50",
    "status": "PENDING",
    "rejection_reason": null,
    "error_message": null,
    "invoice_payload": { "...": "..." },
    "created_at": "2026-09-17T09:12:03.881Z",
    "created_by": "kp.billing",
    "device_id": "",
    "device_name": ""
  },
  { "id": 907, "invoice_log": 118, "status": "REJECTED",
    "rejection_reason": "Wrong ship-to address", "created_by": "preshit", "...": "..." },
  { "id": 908, "invoice_log": 118, "status": "EDITED", "created_by": "kp.billing", "...": "..." },
  { "id": 909, "invoice_log": 214, "status": "PENDING", "created_by": "kp.billing", "...": "..." }
]
```

### 5.6 `POST /refLogs/` — draft reference, HANA-verified

```json
{
  "ref_id": "OMS-2026-0914",
  "card_name": "SHREE TRADERS",
  "doc_date": "2026-09-18",
  "so_number": "512345",
  "status": "ERROR",
  "error_message": "matching record not found (ODBC -2028)",
  "posted_by": 42
}
```

This exists because **SAP's Service Layer sometimes returns "matching record not
found" even though the draft was actually created.** Every draft is stamped with
our ref number in `ODRF.U_OMS_REF`, so the view verifies against HANA before
trusting that error:

| HANA says | Result |
| --- | --- |
| draft exists | `status` forced to `SUCCESS`, `error_message` cleared → *"Invoice created"* |
| no draft | row saved as submitted → *"No matching draft found in HANA for this ref number."* |
| HANA unreachable | row saved as submitted, `verification_error` set → *"…HANA verification is currently unavailable."* |

Idempotent by `ref_id` via `update_or_create` — one ref number maps to exactly
one draft, so a double-click updates rather than duplicating. **201** when
created, **200** when it updated an existing row:

```json
{
  "message": "Invoice created",
  "verified": true,
  "duplicate": false,
  "verification_error": null,
  "data": { "id": 77, "ref_id": "OMS-2026-0914", "status": "SUCCESS", "...": "..." }
}
```

### 5.7 Credit limit — the escape hatch

When a post fails SAP's credit-limit check, the reviewer can raise a request in
JSAP without leaving the screen. Both calls proxy through OMS because the DSR
API has no CORS support (`settings.DSR_API_BASE`, from `JSAP_API_BASE`).

**Detecting the block** ([helpers.ts](../../Frontend/src/pages/invoiceReview/helpers.ts)):
keyed on SAP error **code `13000316`**, not the wording. The message shipped with
that code has been rewritten more than once on this install and only the newest
phrasing contains the words "credit limit" at all —

```
(13000316) Credit Limit Exceeded! Current Limit is 10.00, Balance Amount is 5,379.00
(13000316) Limit is Over, Current Limit is 10.00 Balance Amount Is 9022.00
(13000316) Limit if Over By, Current Limit is 45000.00 Balance Amount Is 46000.00
```

Matching the phrase hid "Raise CL" on every invoice stopped by the two older
messages. The phrase and the `Limit is/if Over` wording remain as fallbacks for
a message arriving without the code.

**`GET /credit-limit/cards/?company=1`** — proxies DSR
`GetCustomerCards` for the customer's live balance and limit. `company` is `1`
for OIL, `2` for BEVERAGE (`companyForBranch`). 502 on a request failure.

**`POST /credit-limit/request/`** — `multipart/form-data`, three parts:

```
documentData   {"cardCode":"CUSTA000512","requestedLimit":250000,
                "reason":"Festive season order","companyId":1}
attachment     approval-mail.pdf
invoice_log_id 214
```

`createdBy` is **overwritten server-side** with `settings.OMS_JSAP_USER_ID` —
the client's value is discarded. Guard order matters here:

1. 400 without `documentData` / `attachment` / `invoice_log_id`.
2. 404 if the log doesn't exist; **409 if it is soft-deleted** — a real approval
   request in JSAP pointing at nothing is worse than a refusal.
3. **409 if a request already exists — checked *before* calling DSR.** Otherwise
   a duplicate attempt creates a second CL document in JSAP and only *then*
   fails on the insert.
4. 400 if `documentData` isn't valid JSON.
5. POST to DSR `CreateCLDocumentV2`. **502 if no `creditDocumentId` comes back**
   (logged) — the alternative was recording a request with no id.
6. Insert `CreditLimitLogs`. An `IntegrityError` here means it lost a race; the
   CL document exists in JSAP either way, so it reports the same 409 rather than
   a 500.

The 409 body carries what the reviewer needs to chase the existing one:

```json
{
  "error": "A credit-limit request has already been raised for this invoice.",
  "detail": "Credit-limit document #4471 was raised for this invoice on 17 Sep 2026 14:22. Track that request instead of raising a new one.",
  "jsap_doc_id": 4471,
  "created_at": "2026-09-17T14:22:10Z",
  "invoice_log_id": 214
}
```

**`GET /credit-limit/flow/?invoice_id=214`** — the approval stages.
`invoice_log_id → jsap_doc_id → flow_id → DSR GetApprovalFlow`. The middle hop
goes **straight to the JSAP SQL Server** (`get_credit_flow_id`), because DSR
offers no document→flow mapping over HTTP: the old route pulled the *current
month's* `GetAllDocuments` and scanned it, which silently failed for any request
raised in an earlier month — exactly when a reviewer wants to check a pending
approval. The mapping is
`cl.jsCreditDocument.id → cl.jsFlow.userDocumentId`, and `cl.jsFlow.id` **is**
the flowId.

`company` is still accepted (callers send it) but unused — document ids are
unique across companies. 404 with no CL log or no flow yet, 502 if JSAP is
unreachable, 504 on a DSR timeout. The frontend sorts the returned stages by
`priority` and summarises them (`A`=Approved, `R`=Rejected, `P`=Pending).

### 5.8 `GET /crystal/?docNum=1204417&docEntry=88431&branch=OIL&party=SHREE+TRADERS`

Returns the **PDF itself**, `Content-Disposition: inline` as
`"1204417 SHREE TRADERS.pdf"` (the party name is sanitised, not trusted).

- `branch` is **required** and must be `OIL` | `BEVERAGE` | `MART` — it picks
  both the schema the DocNum resolves against and the Crystal path the PDF
  renders from (`api/billprint`, `api/billprint/bev`, `api/billprint/mart`).
- `docEntry` short-circuits the lookup. Prefer it: the review screen keeps it
  from the post response, it's what Crystal renders from anyway, and the DocNum
  lookup only searches the OIL company database.
- Without `docEntry`, `docNum` is required and resolved via
  `hana.utils.resolve_doc_entry` (404 if nothing matches).

This must be **fetched, not navigated to**. These were `<a href>` links, which
cannot work — a navigation carries no `Authorization` header and this project
authenticates with JWT alone, so the request arrived anonymous.
[useBillPrint](../../Frontend/src/hooks/useBillPrint.ts) opens the tab
*synchronously* before the request so the browser still attributes it to the
click, and falls back to a download if a popup blocker refuses.

### 5.9 The two double-invoicing guards

Both exist for the same window: **SAP does not know an OMS invoice is in flight.**
It only closes the order and reduces the batch once the invoice actually posts,
so between submit and post a second user can pick the same SO or the same batch
stock.

**`GET /used-sales-orders/?card_code=CUSTA000512&branch=OIL`** — splits the
comma-joined `so_number` back out and returns, newest attempt first:

```json
{
  "success": true,
  "card_code": "CUSTA000512",
  "data": [
    { "so_number": "512345", "log_id": 214, "status": "PENDING",
      "sap_doc_num": "", "created_at": "2026-09-18T11:04:22Z" }
  ],
  "total": 1
}
```

**`GET /reserved-batches/?branch=OIL`** — a **quantity** per
`(item_code, warehouse, batch_number)`, not a flag:

```json
{
  "success": true,
  "data": [
    { "item_code": "FG0000123", "warehouse_code": "DL-MP",
      "batch_number": "B2609A", "quantity": 120.0,
      "log_id": 214, "status": "PENDING" }
  ],
  "total": 1
}
```

A quantity because one batch holds thousands of pieces and is normally split
across many invoices — the caller subtracts what's held and keeps the rest
usable. Treating a batch as taken outright meant twenty pieces on someone else's
draft locked the whole batch. Keyed by item *and* warehouse as well as batch
number, because the same batch number can exist for a different item.

---

## 6) End-to-end flow

```
SalesInvoice page                  invoice module                    external
─────────────────                  ──────────────                    ────────
pick branch + party
  ├─ GET /used-sales-orders/  ───►  in-flight SOs (badge)
  └─ GET /reserved-batches/   ───►  held batch qty (auto-allocation skips)
select SO lines, batches, freight
buildInvoicePayload()
POST /api/invoice/pending/    ───►  invoice_log  (PENDING)
                                    invoice_history (PENDING)
                                    [if edited_from] source → EDITED + history

invoiceReview page
  GET /all/?whs=…             ───►  rows + fg_stock ──────────────►  HANA OITW
                                         + item_names ─────────────►  sap_sync.Product
  GET /history/<id>/          ───►  full revision-chain timeline

  Approve  → PATCH update-status {APPROVED}
  Reject   → PATCH update-status {REJECTED, rejection_reason}
  Edit     → reopens SalesInvoice with edited_from=<id>
  Delete   → DELETE /<id>/delete/   (history row DELETED)

  Post to SAP ──────────────────────────────────────────────────────►  serviceLayer
                                                                        → SAP B1
      success → PATCH {POSTED_TO_SAP, sap_doc_num, sap_doc_entry}
      failure → PATCH {ERROR | CL_RAISED, error_message}

  code 13000316?
      POST /credit-limit/request/  ──► credit_limit_logs  ──────────►  DSR/JSAP
      GET  /credit-limit/flow/     ──► jsap_db → flow id  ──────────►  DSR
      PATCH update-status {CL_RAISED}

  Print → GET /crystal/?docEntry=…&branch=…  ───────────────────────►  Crystal → PDF
```

A detail worth noting in the post step: on failure the status is
`statusAfterFailedPost(record)`, which keeps `CL_RAISED` rather than demoting to
`ERROR`. The request isn't withdrawn because this attempt failed, and until JSAP
clears it a repost keeps failing the same check. Demoting it would take away
"Show Flow" — the only route back to the stages the reviewer already raised —
and offer "Raise CL" again, which the backend refuses with a 409.

---

## 7) Access control — read this before assuming anything

**Every view is `AllowAny`.** This was audited deliberately: create/approve/
reject/delete here are ordinary reviewer actions on the billing desk, not
org-wide admin functions, so none is gated with `core.permissions.IsAdminRole` —
the same distinction `sap_sync` draws for its own order-approval views.
Authentication still comes from the project-wide JWT default; what is *not*
enforced is a permission class.

Two consequences:

1. **`created_by` is a non-null FK to the user.** An unauthenticated `POST
   /pending/` passes `AnonymousUser` into it and fails. The endpoint is reachable
   anonymously; it is not *usable* anonymously.
2. **`_history_actor` treats a body-supplied name as a hint, never proof** — see
   §5.3. Same for `device_id` / `device_name`, which are client-reported.

**Branch scoping** (`branches_for_user`) is the real access logic, applied by
`scope_logs_to_user` on `/all/`, `/logs/all/` and `/reserved-batches/`:

| User's categories | Sees branches |
| --- | --- |
| OIL or MART | `OIL` |
| BEVERAGES | `BEVERAGE` |
| admin (role, `extra_roles`, or `is_staff`) | **all** |
| no category at all | **all** |

That last row is intentional: admins and auditors are set up without categories,
and scoping them to nothing would empty the review screen for the very people
who have to work it.

This uses `core.permissions.is_admin`, not `is_superuser`. The `is_superuser`
check that was here is exactly the bug class `core.permissions` exists to close —
an admin holding the role via `extra_roles` or `is_staff` wasn't recognised and
could have been scoped down to their own categories. **Note the two list views
are not scoped identically to the two guard endpoints:** `/used-sales-orders/`
filters by the `branch` query param only and does **not** call
`scope_logs_to_user`, while `/reserved-batches/` does.

Note also that the **status tabs on the review screen are no longer role-split**.
They used to hide the SAP-side tabs from approvers on the premise that "the
approver's workflow ends at the decision" — wrong twice over: `POSTED_TO_SAP` was
152 of 158 live rows, every `ERROR` row sat in DL-MP whose approver was the one
person shown neither, and the rule keyed off `canApproveReject`, which was
admin-only for most of the app's life so nobody noticed. Actions are still
gated (`canApproveReject`, `canPostToSap`); a tab is a view, and hiding a view
only hid the work.

---

## 8) Two live discrepancies in this module

Both are latent rather than breaking, but neither is intentional:

1. **`InvoiveHistorySerializer.created_by_name` never renders.**
   It is declared `CharField(source='created_by.name')`, but
   `InvocieHistory.created_by` is a **CharField, not an FK** — there is no
   `.name` to traverse. DRF raises `SkipField` for a `read_only` field whose
   source is unreachable, so the key is silently dropped from every history
   response. [InvoiceHistoryDialog.tsx:105](../../Frontend/src/pages/invoiceReview/components/InvoiceHistoryDialog.tsx#L105)
   renders `By: {entry.created_by_name}` conditionally, so the timeline just
   never shows who acted. Verified against the installed DRF, not inferred.
   The fix is to drop the field and read `created_by` directly — it already
   *is* the username string.

2. **`CreditLimitLogSerializer` lists a column that no longer exists.**
   Its `fields` include `credit_raised`, removed from the model by migration
   `0012_remove_creditlimitlogs_credit_raised`. It would raise
   `ImproperlyConfigured` on instantiation — it never does, because nothing
   instantiates it. It is imported into `views.py` and never used. Either delete
   it or drop the stale field before something starts using it.

---

## 9) Debugging checklist

```sql
-- Where is the queue?
SELECT status, count(*), count(sap_doc_num) AS with_docnum
FROM invoice_log WHERE NOT is_deleted GROUP BY status;

-- One invoice's whole story (follow supersedes for the earlier versions)
SELECT id, status, created_by, created_at, rejection_reason, error_message
FROM invoice_history WHERE invoice_log_id = :id ORDER BY created_at, id;

-- Rejected/errored with no explanation (save() should have backfilled)
SELECT id, so_number, status, rejection_reason, error_message
FROM invoice_log WHERE status IN ('REJECTED','ERROR')
  AND coalesce(rejection_reason,'') = '' AND coalesce(error_message,'') = '';

-- Posted but unprintable — no identifiers, so /crystal/ has nothing to resolve
SELECT id, so_number, branch FROM invoice_log
WHERE status = 'POSTED_TO_SAP'
  AND coalesce(sap_doc_num,'') = '' AND coalesce(sap_doc_entry,'') = '';

-- Credit-limit requests in flight
SELECT c.invoice_log_id, c.jsap_doc_id, c.party_name, c.created_at, l.status
FROM credit_limit_logs c JOIN invoice_log l ON l.id = c.invoice_log_id
ORDER BY c.created_at DESC;

-- Who removed things, and why
SELECT id, so_number, status, deleted_at, deleted_by_id, delete_reason
FROM invoice_log WHERE is_deleted ORDER BY deleted_at DESC;
```

Symptom → first place to look:

| Symptom | Look at |
| --- | --- |
| Review screen empty for one user | `branches_for_user` — their categories vs. the logs' `branch` (§7) |
| `/all/` returns 400 | `whs` is required |
| `fg_stock` null / `item_names` empty | HANA reachable? log has a `warehouse`? `branch` set (NULL defaults to OIL)? |
| Stock looks wrong | `warehouse_stock: null` ≠ `0` — null means no `OITW` row for that warehouse |
| Wrong product name shown | Code exists under several categories — check the log's `branch` (§5.2) |
| "Raise CL" not offered | `error_message` must carry `13000316` or the fallback wording |
| CL request 409s | A row already exists in `credit_limit_logs` for that log — one per invoice, enforced by the PK |
| "Show Flow" 404s | No `cl.jsFlow` row yet, or `JSAP_DB_*` unset (a blank host disables JSAP by design) |
| Bill print 404s | Missing `docEntry` **and** a `docNum` that only resolves in the OIL DB |
| Bill print returns HTML/401 | Being navigated to instead of fetched — no JWT header (§5.8) |
| SAP: "Cannot add row without complete selection of batch/serial numbers" | Duplicate `LineNum` across a multi-SO invoice (§4) |
| Timeline shows no "By:" | Known — see §8.1 |
| Batch double-allocated | `/reserved-batches/` excludes `POSTED_TO_SAP` on purpose; after posting, `OIBT` is the truth |
