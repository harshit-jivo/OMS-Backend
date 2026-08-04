# PAYMENT API — Receive Payment

Complete developer reference for the Receive Payment endpoints. Everything here
is testable from Postman without reading the source.

> **Base URL note.** The module is mounted at **`/api/payments/`** (not
> `/api/v1/payments/`). The project has no URL versioning today, and adding a
> `v1` prefix to one module only would have made the API surface less
> consistent, not more. Adjust if you decide to version project-wide.

---

## Table of contents

1. [API information](#1-api-information)
2. [Cascade reads (company → party → invoices)](#2-cascade-reads)
3. [Create receipt](#3-create-receipt)
4. [Submit for approval](#4-submit-for-approval)
5. [List / detail / history](#5-list--detail--history)
6. [Upload attachment](#6-upload-attachment)
7. [Database changes](#7-database-changes)
8. [SAP API called](#8-sap-api-called)
9. [SAP table verification](#9-sap-table-verification)
10. [SAP SQL / HANA queries](#10-sap-sql--hana-queries)
11. [Verification checklist](#11-verification-checklist)
12. [Postman collection](#12-postman-collection)
13. [Common issues](#13-common-issues)

---

## 1. API information

| | |
|---|---|
| **Feature** | Receive Payment |
| **Purpose** | Record cash / UPI / cheque received from a party, allocate it to open SAP invoices, route it through approval, and post it to SAP as an Incoming Payment |
| **Authentication** | **Required** — JWT bearer (`Authorization: Bearer <access>`) |
| **Permissions** | Any authenticated user may create. Data is scoped to the companies the user has `UserPartyAssignment` rows for. Approval is governed by `approvals.ApprovalLevel` |
| **Content type** | `application/json` (except attachment upload → `multipart/form-data`) |

### Endpoint summary

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/payments/companies/` | Companies available to the caller |
| `GET` | `/api/payments/parties/` | Parties in a company (paginated) |
| `GET` | `/api/payments/open-invoices/` | **Live** open invoices for a party |
| `GET` | `/api/payments/collection-persons/` | "Received From" people |
| `GET` | `/api/payments/bank-accounts/` | Bank / cash accounts |
| `POST` | `/api/payments/receipts/` | Create a receipt |
| `GET` | `/api/payments/receipts/` | List receipts |
| `GET` | `/api/payments/receipts/{id}/` | Receipt detail |
| `POST` | `/api/payments/receipts/{id}/submit/` | Send for approval |
| `GET` | `/api/payments/receipts/{id}/history/` | Lifecycle log |
| `POST` | `/api/payments/receipts/{id}/attachments/` | Upload proof |
| `GET` | `/api/payments/attachments/{id}/download/` | Download (permission-checked) |

All JSON responses use one envelope:

```json
{ "success": true, "message": "", "data": { } }
```

---

## 2. Cascade reads

The UI fills a receipt in a fixed order. Each step feeds the next.

```
Company  ──►  Party  ──►  Open invoices
(local)      (local)      (LIVE from SAP HANA)
```

### 2.1 `GET /api/payments/companies/`

Returns the companies (SAP categories) this user may transact in.

```json
{
  "success": true,
  "message": "",
  "data": [
    { "id": 1, "company": "OIL", "display_name": "Jivo Oil",
      "default_bpl_id": 2, "is_active": true },
    { "id": 2, "company": "BEVERAGES", "display_name": "Jivo Beverages",
      "default_bpl_id": 3, "is_active": true }
  ]
}
```

### 2.2 `GET /api/payments/parties/?company=OIL&search=puran&page=1&page_size=25`

| Param | Required | Notes |
|---|---|---|
| `company` | **Yes** | `OIL` \| `BEVERAGES` \| `MART` |
| `search` | No | Matches card name (contains) or card code (starts-with) |
| `page`, `page_size` | No | Default 25, max 200 |

> **Why `company` is mandatory.** `card_code` is **not** unique across
> categories — `sap_sync.Party` is unique on `(card_code, category)`. The same
> code is a *different* business partner per company: `CUSTA000878` is PURAN
> STORE under OIL and A ONE BEVERAGES under BEVERAGES. An unscoped lookup would
> credit the wrong customer.

```json
{
  "success": true, "message": "",
  "data": {
    "results": [
      { "card_code": "CUSTA000878", "card_name": "PURAN STORE",
        "label": "PURAN STORE (CUSTA000878)", "company": "OIL",
        "state": "PUNJAB" }
    ],
    "pagination": { "page": 1, "page_size": 25, "total": 1, "total_pages": 1 }
  }
}
```

### 2.3 `GET /api/payments/open-invoices/?company=OIL&card_code=CUSTA000878`

Read **live from SAP HANA** — never from a mirror, because a balance changes
whenever anyone takes a payment and a stale figure would let two collectors
over-apply against the same invoice.

| Param | Required | Notes |
|---|---|---|
| `company` | **Yes** | Selects the HANA schema |
| `card_code` | **Yes** | Authorised server-side against `UserPartyAssignment` |
| `search` | No | Matches `DocNum` or `NumAtCard` |
| `limit`, `offset` | No | Default 100 / 0 |

```json
{
  "success": true, "message": "",
  "data": {
    "results": [
      { "doc_entry": 45012, "doc_num": 12045,
        "doc_date": "2026-06-15", "due_date": "2026-07-15",
        "party_ref": "PO-4471", "card_code": "CUSTA000878",
        "card_name": "PURAN STORE", "currency": "INR",
        "doc_total": 70000.00, "paid_to_date": 0.00,
        "balance_due": 70000.00, "days_overdue": 15 }
    ],
    "count": 1
  }
}
```

Underlying SQL (bind-parameterised — see §10 for the full statement):

```sql
SELECT ... FROM "<schema>"."OINV" T0
WHERE T0."CardCode" = ?
  AND T0."DocStatus" = 'O' AND T0."CANCELED" = 'N' AND T0."DocType" = 'I'
  AND T0."DocTotal" - IFNULL(T0."PaidToDate", 0) > ?
```

### 2.4 `GET /api/payments/collection-persons/?company=OIL`

The manually-maintained "Received From" list — company staff, not SAP business
partners.

```json
{ "success": true, "message": "",
  "data": [ { "id": 3, "name": "Navneet", "code": "EMP-003",
              "company": "OIL", "phone": "9876543210",
              "sap_slp_code": null, "is_active": true } ] }
```

### 2.5 `GET /api/payments/bank-accounts/?company=OIL`

```json
{ "success": true, "message": "",
  "data": [ { "id": 1, "name": "HDFC Bank — ****4821", "company": "OIL",
              "account_type": "BANK", "masked_number": "****4821",
              "ifsc": "HDFC0001234", "branch_name": "Ludhiana",
              "is_active": true } ] }
```

---

## 3. Create receipt

```
POST /api/payments/receipts/
Authorization: Bearer <access_token>
Content-Type: application/json
```

### Request JSON

```json
{
  "company": "OIL",
  "card_code": "CUSTA000878",
  "card_name": "PURAN STORE",
  "received_from_type": "PARTY",
  "received_from_person": null,
  "payment_date": "2026-07-30",
  "is_advance": false,
  "currency": "INR",
  "remarks": "Payment against INV-12045",
  "methods": [
    {
      "method": "CASH",
      "amount": "80000.00",
      "denominations": [
        { "denomination": 500, "quantity": 100 },
        { "denomination": 200, "quantity": 100 },
        { "denomination": 100, "quantity": 100 }
      ]
    },
    {
      "method": "UPI",
      "amount": "20000.00",
      "upi_reference": "UPI2026073012345678"
    },
    {
      "method": "CHEQUE",
      "amount": "25000.00",
      "cheque_number": "458812",
      "bank_name": "HDFC Bank",
      "cheque_date": "2026-08-05"
    }
  ],
  "allocations": [
    {
      "sap_doc_entry": 45012,
      "sap_doc_num": 12045,
      "invoice_type": 13,
      "invoice_date": "2026-06-15",
      "invoice_due_date": "2026-07-15",
      "invoice_total": "70000.00",
      "balance_at_selection": "70000.00",
      "amount_applied": "70000.00"
    },
    {
      "sap_doc_entry": 45188,
      "sap_doc_num": 12188,
      "invoice_type": 13,
      "invoice_total": "55000.00",
      "balance_at_selection": "55000.00",
      "amount_applied": "55000.00"
    }
  ]
}
```

### Field reference — header

| Field | Type | Required | Notes |
|---|---|---|---|
| `company` | string | **Yes** | `OIL` \| `BEVERAGES` \| `MART`. Determines the SAP company DB, frozen onto the row at creation |
| `card_code` | string | **Yes** | SAP `CardCode`. Only meaningful together with `company` |
| `card_name` | string | No | Denormalised snapshot for lists |
| `received_from_type` | string | No | `PARTY` (default) or `PERSON` |
| `received_from_person` | int | **Conditional** | Required when `received_from_type = "PERSON"`. FK to `payment_collection_person` |
| `payment_date` | date | **Yes** | `YYYY-MM-DD` → SAP `DocDate` |
| `is_advance` | bool | No | `true` = on-account, no invoice allocation |
| `currency` | string | No | Default `INR` |
| `remarks` | string | No | Free text |
| `methods` | array | **Yes** | ≥ 1 tender line |
| `allocations` | array | Conditional | Required unless `is_advance = true` |

> `total_amount` is **not** accepted from the client. It is derived from
> `methods` inside the create transaction — otherwise a partial write leaves
> the header disagreeing with `SUM(children)`.

### Field reference — `methods[]`

| Field | Type | Required | Notes |
|---|---|---|---|
| `method` | string | **Yes** | `CASH` \| `UPI` \| `CHEQUE` |
| `amount` | decimal | **Yes** | > 0 |
| `upi_reference` | string | No | UPI only |
| `cheque_number` | string | **Cheque** | Mandatory when method = CHEQUE |
| `bank_name` | string | No | Cheque |
| `cheque_date` | date | **Cheque** | Mandatory when method = CHEQUE |
| `denominations` | array | No | **Cash only.** If supplied, must total `amount` exactly |

### Field reference — `denominations[]`

| Field | Type | Required | Allowed |
|---|---|---|---|
| `denomination` | int | **Yes** | 10, 20, 50, 100, 200, 500 |
| `quantity` | int | **Yes** | > 0 |

### Field reference — `allocations[]`

| Field | Type | Required | Notes |
|---|---|---|---|
| `sap_doc_entry` | int | **Yes** | From the open-invoice call |
| `sap_doc_num` | int | No | Display |
| `invoice_type` | int | No | `13` = A/R Invoice (default), `14` = Credit Note |
| `invoice_date`, `invoice_due_date` | date | No | Snapshot |
| `invoice_total` | decimal | No | Snapshot |
| `balance_at_selection` | decimal | No | **Recommended.** Lets the worker detect that SAP changed underneath us before posting |
| `amount_applied` | decimal | **Yes** | > 0. Sum across allocations must be ≤ receipt total |

### Success response

```
HTTP 201 Created
```

```json
{
  "success": true,
  "message": "Receipt created.",
  "data": {
    "id": 41,
    "receipt_no": "RCP-OIL-2026-27-000123",
    "company": "OIL",
    "card_code": "CUSTA000878",
    "card_name": "PURAN STORE",
    "received_from_type": "PARTY",
    "received_from_person": null,
    "received_from_name": "",
    "payment_date": "2026-07-30",
    "is_advance": false,
    "total_amount": "125000.00",
    "allocated_amount": "125000.00",
    "unallocated_amount": "0.00",
    "currency": "INR",
    "remarks": "Payment against INV-12045",
    "status": "DRAFT",
    "status_display": "Draft",
    "sap_doc_entry": null,
    "sap_doc_num": null,
    "sap_posted_at": null,
    "methods": [
      { "id": 88, "method": "CASH", "amount": "80000.00",
        "upi_reference": "", "cheque_number": "", "bank_name": "",
        "cheque_date": null, "sap_check_key": null,
        "denominations": [
          { "id": 20, "denomination": 500, "quantity": 100, "line_total": "50000.00" },
          { "id": 21, "denomination": 200, "quantity": 100, "line_total": "20000.00" },
          { "id": 22, "denomination": 100, "quantity": 100, "line_total": "10000.00" }
        ] }
    ],
    "allocations": [
      { "id": 55, "sap_doc_entry": 45012, "sap_doc_num": 12045,
        "invoice_type": 13, "invoice_date": "2026-06-15",
        "invoice_due_date": "2026-07-15", "invoice_total": "70000.00",
        "balance_at_selection": "70000.00", "amount_applied": "70000.00" }
    ],
    "attachments": [],
    "approval": null,
    "created_at": "2026-07-30T09:12:44.221Z",
    "updated_at": "2026-07-30T09:12:44.221Z"
  }
}
```

`receipt_no` is allocated from a row-locked counter (`core_document_counter`),
so concurrent creates cannot collide.

---

## 4. Submit for approval

```
POST /api/payments/receipts/{id}/submit/
Authorization: Bearer <access_token>
```

No request body. Valid from `DRAFT` or `REJECTED` (resubmit).

```
HTTP 200 OK
```

```json
{
  "success": true,
  "message": "Submitted for approval.",
  "data": {
    "id": 41,
    "receipt_no": "RCP-OIL-2026-27-000123",
    "status": "PENDING_APPROVAL",
    "status_display": "Pending approval",
    "approval": {
      "id": 17, "status": "PENDING",
      "current_level": 1, "total_levels": 2,
      "level_label": "Level 1 of 2"
    }
  }
}
```

On resubmit after rejection the approval restarts at level 1 and
`round_number` increments — prior `approval_action` rows are **kept**, not
deleted, so the earlier attempt stays auditable.

---

## 5. List / detail / history

### `GET /api/payments/receipts/?company=OIL&status=POSTED&page=1`

| Param | Notes |
|---|---|
| `company` | Filter by company |
| `status` | `DRAFT` \| `PENDING_APPROVAL` \| `APPROVED` \| `REJECTED` \| `QUEUED` \| `POSTED` \| `FAILED` \| `CANCELLED` |
| `card_code` | Filter by party |
| `mine=true` | Only receipts created by the caller |

Paginated envelope, same shape as §2.2.

### `GET /api/payments/receipts/{id}/`

Full detail — identical to the create response, plus populated `attachments`
and `approval`.

### `GET /api/payments/receipts/{id}/history/`

Append-only lifecycle log (distinct from the approval log: this includes
system transitions such as the worker moving `QUEUED → POSTED`).

```json
{
  "success": true, "message": "",
  "data": [
    { "id": 91, "from_status": "QUEUED", "to_status": "POSTED",
      "reason": "Posted to SAP as DocEntry 3187",
      "actor_kind": "SAP_WORKER", "changed_by_username": "",
      "created_at": "2026-07-30T09:31:02.441Z" },
    { "id": 90, "from_status": "DRAFT", "to_status": "PENDING_APPROVAL",
      "reason": "Submitted for approval.", "actor_kind": "USER",
      "changed_by_username": "navneet",
      "created_at": "2026-07-30T09:15:10.002Z" }
  ]
}
```

---

## 6. Upload attachment

```
POST /api/payments/receipts/{id}/attachments/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data
```

| Form field | Required | Notes |
|---|---|---|
| `file` | **Yes** | jpg / jpeg / png / pdf, ≤ 5 MB |
| `attachment_type` | **Yes** | `CHEQUE_IMAGE` \| `UPI_SCREENSHOT` |

Validation is enforced on extension, size **and magic bytes** — a `.jpg` whose
content is actually HTML is rejected.

```
HTTP 201 Created
```

```json
{
  "success": true,
  "message": "File uploaded.",
  "data": {
    "id": 12,
    "attachment_type": "CHEQUE_IMAGE",
    "type_display": "Cheque image",
    "stored_name": "9f0c2dbe8d8f4f7cb91f.jpg",
    "original_name": "cheque_458812.jpg",
    "uploaded_by": 7,
    "uploaded_by_name": "Navneet",
    "download_url": "/api/payments/attachments/12/download/",
    "created_at": "2026-07-30T09:20:31.118Z"
  }
}
```

The file is written **flat** to the configured share under a UUID name:

```
\\JIVO-APP\Payments\Receive_Payments\9f0c2dbe8d8f4f7cb91f.jpg
```

The network path is never returned to the client. Download goes through
`GET /api/payments/attachments/{id}/download/`, which re-checks permission,
returns a `FileResponse` with `Content-Disposition: attachment` and
`X-Content-Type-Options: nosniff`.

---

## 7. Database changes

### `POST /api/payments/receipts/` (all inside ONE transaction)

| Table | Operation | Detail |
|---|---|---|
| `core_document_counter` | **UPDATE** | Row-locked (`SELECT … FOR UPDATE`); `last_number += 1` |
| `payment_receipt` | **INSERT** | 1 row. `total_amount` / `allocated_amount` derived from children; `idempotency_key` (UUID) and `company_db` set here |
| `payment_method_entry` | **INSERT** | 1 per tender line |
| `payment_cash_denomination` | **INSERT** | 1 per note row (bulk) |
| `payment_invoice_allocation` | **INSERT** | 1 per invoice (bulk) |
| `payment_status_history` | **INSERT** | 1 row: `→ DRAFT` |

### `POST /api/payments/receipts/{id}/submit/`

| Table | Operation | Detail |
|---|---|---|
| `approval_request` | **INSERT** (or UPDATE on resubmit) | `status=PENDING`, `current_level=1`, `total_levels` snapshotted |
| `approval_action` | **INSERT** | `SUBMIT` (or `RESUBMIT`), with IP + user agent |
| `payment_receipt` | **UPDATE** | `status → PENDING_APPROVAL` |
| `payment_status_history` | **INSERT** | `DRAFT → PENDING_APPROVAL` |

### `POST /api/approvals/requests/{id}/act/` — final approval

| Table | Operation | Detail |
|---|---|---|
| `approval_action` | **INSERT** | `APPROVE` |
| `approval_request` | **UPDATE** | `status → APPROVED`, `decided_at` |
| `payment_receipt` | **UPDATE** | `status → QUEUED` |
| `payment_sap_outbox` | **INSERT** | Operation `POST_INCOMING_PAYMENT`, payload frozen |
| `payment_status_history` | **INSERT** | `→ QUEUED` |

All five commit together. There is no window where a receipt is approved but
not queued.

### Outbox worker (`drain_sap_outbox`)

| Table | Operation | Detail |
|---|---|---|
| `payment_sap_outbox` | **UPDATE** | `→ IN_FLIGHT`, lease set; then `SUCCEEDED` / `FAILED` / `DEAD` / `NEEDS_REVIEW` |
| `payment_sap_call_log` | **INSERT** | 1 per HTTP attempt, request redacted |
| `payment_receipt` | **UPDATE** | `sap_doc_entry`, `sap_doc_num`, `sap_posted_at`, `status → POSTED` |
| `payment_method_entry` | **UPDATE** | `sap_check_key` per cheque, from the SAP response |
| `payment_status_history` | **INSERT** | `QUEUED → POSTED` |

**No DELETEs anywhere.** Financial rows are never removed; cancellation is a
status transition.

---

## 8. SAP API called

### `POST /b1s/v2/IncomingPayments`

**Headers**

```
Content-Type: application/json
Cookie: B1SESSION=<session-id>; ROUTEID=<route>
```

The session is obtained by `POST /b1s/v2/Login` with
`{"CompanyDB","UserName","Password"}` and cached **per company DB** (key
`payments:sap_session:<company_db>`).

**Payload** — mixed tender with two invoice allocations:

```json
{
  "CardCode": "CUSTA000878",
  "DocDate": "2026-07-30",
  "DocType": "rCustomer",
  "DocCurrency": "INR",
  "Remarks": "OMS RCP-OIL-2026-27-000123",
  "U_OMS_IDEM": "8f14e45f-ea1b-4c2f-9a3d-7b0c5d2e1f44",
  "U_OMS_REF": "RCP-OIL-2026-27-000123",
  "CashAccount": "_SYS00000000098",
  "CashSum": 80000.00,
  "TransferAccount": "_SYS00000000123",
  "TransferSum": 20000.00,
  "TransferDate": "2026-07-30",
  "TransferReference": "UPI2026073012345678",
  "PaymentChecks": [
    { "CheckNumber": 458812, "BankCode": "HDFC Bank",
      "CheckSum": 25000.00, "DueDate": "2026-08-05",
      "CheckAccount": "_SYS00000000131" }
  ],
  "PaymentInvoices": [
    { "LineNum": 0, "DocEntry": 45012, "InvoiceType": "it_Invoice", "SumApplied": 70000.00 },
    { "LineNum": 1, "DocEntry": 45188, "InvoiceType": "it_Invoice", "SumApplied": 55000.00 }
  ]
}
```

**Tender → SAP field mapping** (not uniform — a frequent source of bugs):

| Method | Account field | Sum field | Extra |
|---|---|---|---|
| Cash | `CashAccount` | `CashSum` | — |
| UPI / NEFT | `TransferAccount` | `TransferSum` | `TransferDate`, `TransferReference` |
| Cheque | `CheckAccount` | *(from the check lines)* | `PaymentChecks[]` |

> **Advance payments omit `PaymentInvoices` entirely.** Sending `[]` is a
> common cause of SAP rejecting the document.

**Expected response — HTTP 201**

```json
{
  "DocEntry": 3187,
  "DocNum": 3187,
  "DocType": "rCustomer",
  "DocDate": "2026-07-30",
  "CardCode": "CUSTA000878",
  "CardName": "PURAN STORE",
  "CashSum": 80000.0,
  "TransferSum": 20000.0,
  "DocTotal": 125000.0,
  "Canceled": "tNO",
  "U_OMS_IDEM": "8f14e45f-ea1b-4c2f-9a3d-7b0c5d2e1f44",
  "PaymentChecks": [
    { "LineNum": 0, "CheckKey": 901, "CheckNumber": 458812, "CheckSum": 25000.0 }
  ],
  "PaymentInvoices": [
    { "LineNum": 0, "DocEntry": 45012, "SumApplied": 70000.0 }
  ]
}
```

**Fields consumed**

| SAP field | Stored in |
|---|---|
| `DocEntry` | `payment_receipt.sap_doc_entry` |
| `DocNum` | `payment_receipt.sap_doc_num` |
| `PaymentChecks[].CheckKey` | `payment_method_entry.sap_check_key` (required later to deposit that cheque) |

### `GET /b1s/v2/IncomingPayments?$filter=U_OMS_IDEM eq '<uuid>'&$top=1`

The **idempotency probe**, run before every retry. If it returns a row the
worker adopts that `DocEntry` and never posts again.

---

## 9. SAP table verification

### Incoming Payment

| Table | Meaning | Expected |
|---|---|---|
| `ORCT` | Payment header | 1 new row; `DocEntry` = returned value; `U_OMS_REF` = our receipt no |
| `RCT1` | Cheque lines | 1 row per cheque (only when `PaymentChecks` was sent) |
| `RCT2` | Invoice allocations | 1 row per `PaymentInvoices[]` entry |
| `OINV` | Invoices paid | `PaidToDate` increased; `DocStatus` → `C` when fully settled |
| `OCRD` | Business partner | `Balance` decreases by the applied amount |
| `OJDT` / `JDT1` | Journal entry | Auto-created by SAP; `TransId` referenced on `ORCT` |

> `RCT1` holds **cheque** lines and `RCT2` holds **invoice allocation** lines.
> A cash-only payment has rows in `ORCT` and `RCT2` but **none** in `RCT1` —
> that is correct, not a missing write.

---

## 10. SAP SQL / HANA queries

Replace `<SCHEMA>` with the company DB (`JIVO_OIL_HANADB`, etc.).

### 10.1 Find the payment we just posted

```sql
-- By our own reference — the most reliable lookup
SELECT "DocEntry", "DocNum", "DocDate", "CardCode", "CardName",
       "DocTotal", "CashSum", "TrsfrSum", "Canceled",
       "U_OMS_REF", "U_OMS_IDEM"
FROM "<SCHEMA>"."ORCT"
WHERE "U_OMS_REF" = 'RCP-OIL-2026-27-000123';

-- Most recent payments
SELECT "DocEntry", "DocNum", "DocDate", "CardCode", "DocTotal", "U_OMS_REF"
FROM "<SCHEMA>"."ORCT"
ORDER BY "DocEntry" DESC
LIMIT 20;
```

### 10.2 Cheque lines (RCT1)

```sql
SELECT "DocNum", "LineID", "CheckNum", "BankCode", "CheckSum",
       "DueDate", "CheckAct", "CheckKey"
FROM "<SCHEMA>"."RCT1"
WHERE "DocNum" = <ORCT.DocEntry>;
```

### 10.3 Invoice allocations (RCT2)

```sql
SELECT "DocNum", "LineID", "DocEntry" AS "InvoiceDocEntry",
       "InvType", "SumApplied", "AppliedSys"
FROM "<SCHEMA>"."RCT2"
WHERE "DocNum" = <ORCT.DocEntry>;
```

> `RCT2."DocNum"` is the **payment's DocEntry**, and `RCT2."DocEntry"` is the
> **invoice's DocEntry**. The naming is counter-intuitive and is a common
> source of wrong verification queries.

### 10.4 Invoice balance updated

```sql
SELECT "DocEntry", "DocNum", "CardCode",
       "DocTotal", "PaidToDate",
       "DocTotal" - IFNULL("PaidToDate", 0) AS "BalanceDue",
       "DocStatus"
FROM "<SCHEMA>"."OINV"
WHERE "DocEntry" IN (45012, 45188);
```

Expect `PaidToDate` to have risen by `SumApplied`, and `DocStatus = 'C'` once
the balance reaches zero.

### 10.5 Business-partner balance

```sql
SELECT "CardCode", "CardName", "Balance", "OrdersBal"
FROM "<SCHEMA>"."OCRD"
WHERE "CardCode" = 'CUSTA000878';
```

### 10.6 Duplicate check — the important one

```sql
-- MUST return exactly one row. More than one means a double-post.
SELECT "U_OMS_IDEM", COUNT(*) AS "Copies"
FROM "<SCHEMA>"."ORCT"
WHERE "U_OMS_IDEM" IS NOT NULL AND "U_OMS_IDEM" <> ''
GROUP BY "U_OMS_IDEM"
HAVING COUNT(*) > 1;
```

### 10.7 Journal entry

```sql
SELECT T0."DocEntry", T0."TransId", T1."TransId", T1."Account",
       T1."Debit", T1."Credit"
FROM "<SCHEMA>"."ORCT" T0
JOIN "<SCHEMA>"."JDT1" T1 ON T1."TransId" = T0."TransId"
WHERE T0."DocEntry" = <ORCT.DocEntry>;
```

### 10.8 OMS-side verification (PostgreSQL)

```sql
SELECT id, receipt_no, company, status, total_amount,
       sap_doc_entry, sap_doc_num, sap_posted_at, idempotency_key
FROM payment_receipt
WHERE receipt_no = 'RCP-OIL-2026-27-000123';

SELECT id, status, attempts, last_http_status, last_error, next_attempt_at
FROM payment_sap_outbox
WHERE idempotency_key = '<uuid>';

SELECT attempt_number, http_status, sap_error_code, duration_ms, status
FROM payment_sap_call_log
WHERE idempotency_key = '<uuid>'
ORDER BY created_at;

SELECT from_status, to_status, actor_kind, reason, created_at
FROM payment_status_history
WHERE object_id = <receipt_id>
ORDER BY created_at;
```

---

## 11. Verification checklist

Run in order. Every box must pass before the feature is signed off.

**API**
- [ ] `POST /receipts/` returns **201** with a `receipt_no`
- [ ] `total_amount` equals the sum of `methods[].amount`
- [ ] Cash denominations total exactly the cash amount
- [ ] `POST /receipts/{id}/submit/` returns **200**, `status = PENDING_APPROVAL`

**OMS database**
- [ ] `payment_receipt` — 1 row, correct company / card_code / totals
- [ ] `payment_method_entry` — 1 row per tender
- [ ] `payment_cash_denomination` — 1 row per note
- [ ] `payment_invoice_allocation` — 1 row per invoice
- [ ] `payment_status_history` — a row per transition
- [ ] `core_document_counter.last_number` incremented once

**Approval**
- [ ] `approval_request` created, `level_label` reads "Level 1 of N"
- [ ] `approval_action` has a `SUBMIT` row with IP and user agent
- [ ] Approving at each level advances `current_level`
- [ ] Self-approval is rejected with **403**
- [ ] Rejecting with blank remarks is rejected with **400**

**Outbox → SAP**
- [ ] `payment_sap_outbox` row created on final approval
- [ ] Receipt status is `QUEUED`
- [ ] `drain_sap_outbox` posts and reports `SUCCEEDED`
- [ ] `payment_sap_call_log` records the attempt (request redacted)

**SAP**
- [ ] `ORCT` has 1 row with matching `U_OMS_REF`
- [ ] `RCT2` allocation rows match the request
- [ ] `RCT1` cheque rows present when a cheque was tendered
- [ ] `OINV.PaidToDate` increased by the applied amount
- [ ] `OCRD.Balance` decreased
- [ ] Duplicate query (§10.6) returns **zero** rows

**Write-back**
- [ ] `sap_doc_entry` and `sap_doc_num` stored on the receipt
- [ ] Receipt status is `POSTED`
- [ ] `sap_check_key` populated for each cheque

**Attachments**
- [ ] Upload returns **201**; the file exists on the share under its UUID name
- [ ] Download returns the file for an authorised user
- [ ] Download returns **403** for an unauthorised user
- [ ] A 6 MB file is rejected; a `.txt` renamed to `.jpg` is rejected

**Idempotency (the critical test)**
- [ ] Kill the worker mid-post, restart → the probe adopts the existing
      `DocEntry` and does **not** create a second `ORCT` row

---

## 12. Postman collection

Set collection variables: `base_url` = `http://<host>:8000`, `token`.

### 12.1 Login (to get a token)

```
POST {{base_url}}/api/auth/login/
Content-Type: application/json

{ "username": "navneet", "password": "••••••" }
```

Save `data.tokens.access` → `{{token}}`.

### 12.2 Companies

```
GET {{base_url}}/api/payments/companies/
Authorization: Bearer {{token}}
```
Expect **200**.

### 12.3 Parties

```
GET {{base_url}}/api/payments/parties/?company=OIL&search=puran
Authorization: Bearer {{token}}
```
Expect **200**, paginated.

### 12.4 Open invoices

```
GET {{base_url}}/api/payments/open-invoices/?company=OIL&card_code=CUSTA000878
Authorization: Bearer {{token}}
```
Expect **200** with `balance_due` per invoice.

### 12.5 Create receipt

```
POST {{base_url}}/api/payments/receipts/
Authorization: Bearer {{token}}
Content-Type: application/json
```
Body: see §3. Expect **201**. Save `data.id` → `{{receipt_id}}`.

### 12.6 Upload cheque image

```
POST {{base_url}}/api/payments/receipts/{{receipt_id}}/attachments/
Authorization: Bearer {{token}}
Content-Type: multipart/form-data

file            : <cheque.jpg>
attachment_type : CHEQUE_IMAGE
```
Expect **201**.

### 12.7 Submit

```
POST {{base_url}}/api/payments/receipts/{{receipt_id}}/submit/
Authorization: Bearer {{token}}
```
Expect **200**, `status = PENDING_APPROVAL`.

### 12.8 Approve (as an approver — see APPROVAL_API.md)

```
POST {{base_url}}/api/approvals/requests/{{approval_id}}/act/
Authorization: Bearer {{approver_token}}
Content-Type: application/json

{ "decision": "APPROVE", "remarks": "Verified against bank statement." }
```

### 12.9 Drain the outbox (server-side)

```bash
python manage.py drain_sap_outbox
```

### 12.10 Confirm posted

```
GET {{base_url}}/api/payments/receipts/{{receipt_id}}/
Authorization: Bearer {{token}}
```
Expect `status = POSTED` with `sap_doc_entry` / `sap_doc_num` populated.

---

## 13. Common issues

| Symptom | Cause | Fix |
|---|---|---|
| **401 Unauthorized** | Missing / expired access token | Re-login. Access tokens last 1 day; call `/api/auth/refresh/` |
| **400 "A company is required."** | `company` omitted from parties / open-invoices | Always pass `company` — `card_code` alone is ambiguous across companies |
| **400 "No active SAP company mapping for X"** | `payment_sap_company_map` not seeded | Add a row in Admin → Payments → SAP company mappings |
| **400 "Payment methods total X but the receipt total is Y"** | Client sent inconsistent children | Amounts are derived server-side; check the tender lines |
| **400 "Denominations total X but the cash amount is Y"** | Note breakdown doesn't balance | Fix quantities, or omit `denominations` (optional) |
| **400 "Select at least one invoice, or mark as an advance"** | No allocations and `is_advance` false | Add allocations or set `is_advance: true` |
| **403 "You are not assigned to this party."** | No `UserPartyAssignment` for `(card_code, company)` | Grant the assignment in Admin → Users |
| **403 "You cannot approve a document you submitted."** | Self-approval attempted | Another user must approve, or set `forbid_self_approval=false` on the workflow |
| **404** | Wrong id, or the record is outside the caller's companies | Verify the id and the user's company scope |
| **409-like: "This document already has an open approval request"** | Double submit | Check `approval_request`; a partial unique index prevents two open chains |
| **502 "Could not read open invoices from SAP right now."** | HANA unreachable / schema wrong | Check `payment_sap_company_map.hana_schema` and HANA connectivity |
| **502 "The file store is currently unavailable."** | Share unreachable or credentials wrong | Verify `PAYMENTS_IMAGES` and `PAYMENTS_SMB_USERNAME/PASSWORD`; test with the `test_qr_share` command pattern |
| **Outbox stuck in `FAILED`** | Retryable SAP error | Inspect `last_error`; it retries with backoff (30s→1h, 6 attempts) |
| **Outbox `DEAD`** | Permanent SAP error (`-5002` over-application, `-10` bad CardCode) | Fix the data; re-queue via the admin action |
| **Outbox `NEEDS_REVIEW`** | **Ambiguous** — could not verify whether SAP committed | **Do not blindly retry.** Run §10.1 by `U_OMS_REF`. If present, the payment posted; reconcile manually |
| **SAP "Invoice already closed"** | Another payment settled it first | Re-query open invoices; `balance_at_selection` exists to detect this |
| **SAP login fails** | Bad credentials, or wrong `CompanyDB` | Check `HANA_USERNAME` / `HANA_PASSWORD` and the mapping's `company_db` |
| **Two `ORCT` rows for one receipt** | `U_OMS_IDEM` UDF missing in SAP | **Create the UDF** — the idempotency probe cannot work without it |
| **Django won't start: `HANA_DB_NAME not found`** | Pre-existing config gap: `.env` defines `HANA_DB_OIL_NAME` | Add `HANA_DB_NAME=<oil db>` to `.env` |
