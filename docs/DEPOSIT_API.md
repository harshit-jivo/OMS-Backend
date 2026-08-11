# DEPOSIT API — Bank Deposit

Developer reference for the Bank Deposit endpoints. Fully testable from Postman
without reading the source.

Companion documents: [`PAYMENT_API.md`](PAYMENT_API.md),
[`APPROVAL_API.md`](APPROVAL_API.md), [`SAP_VERIFICATION.md`](SAP_VERIFICATION.md).

---

## 1. API information

| | |
|---|---|
| **Feature** | Bank Deposit |
| **Purpose** | Bundle already-posted payment receipts into a single bank deposit, route it through approval, and post it to SAP as a Deposit |
| **Authentication** | **Required** — JWT bearer |
| **Permissions** | Authenticated; scoped to the caller's companies. Approval governed by the `DEPOSIT` workflow |
| **Base path** | `/api/payments/` |

### Endpoint summary

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/payments/depositable-receipts/` | Posted receipts not yet banked |
| `POST` | `/api/payments/deposits/` | Create a deposit |
| `GET` | `/api/payments/deposits/` | List deposits |
| `GET` | `/api/payments/deposits/{id}/` | Deposit detail |
| `POST` | `/api/payments/deposits/{id}/submit/` | Send for approval |
| `POST` | `/api/payments/deposits/{id}/attachments/` | Upload slip / receipt |

> **Ordering constraint.** A receipt can only be deposited once it is `POSTED`
> in SAP. For cheques this is not merely procedural: SAP's `Deposits` object
> references existing cheques by `CheckKey`, and that key only exists after the
> Incoming Payment has posted.

---

## 2. Request JSON

### 2.1 List depositable receipts

```
GET /api/payments/depositable-receipts/?company=OIL
Authorization: Bearer <access_token>
```

Returns receipts where `status = POSTED` **and** `deposit IS NULL`.

```json
{
  "success": true, "message": "",
  "data": {
    "results": [
      { "id": 41, "receipt_no": "RCP-OIL-2026-27-000123",
        "card_name": "PURAN STORE", "payment_date": "2026-07-30",
        "total_amount": "125000.00", "status": "POSTED",
        "sap_doc_num": 3187 }
    ],
    "pagination": { "page": 1, "page_size": 25, "total": 1, "total_pages": 1 }
  }
}
```

### 2.2 Create deposit

```
POST /api/payments/deposits/
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "company": "OIL",
  "deposit_date": "2026-07-31",
  "deposited_by": 3,
  "bank_account": 1,
  "deposit_type": "CHEQUE",
  "deposit_amount": "120000.00",
  "shortfall_reason": "₹5,000 retained as branch petty cash float.",
  "bank_charge": "0.00",
  "currency": "INR",
  "slip_number": "DEP-SLIP-88213",
  "remarks": "Deposited at HDFC Ludhiana Mall Road branch.",
  "receipt_ids": [41, 42, 43]
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `company` | string | **Yes** | `OIL` \| `BEVERAGES` \| `MART`. Determines the SAP company DB |
| `deposit_date` | date | **Yes** | `YYYY-MM-DD` → SAP `DepositDate` |
| `deposited_by` | int | No | FK to `payment_collection_person` |
| `bank_account` | int | **Yes** | FK to `payment_bank_account`; supplies the SAP GL code |
| `deposit_type` | string | No | `CASH` (default) \| `CHEQUE` \| `MIXED` |
| `deposit_amount` | decimal | **Yes** | What was actually banked. Must be > 0 and ≤ collected |
| `shortfall_reason` | string | **Conditional** | **Mandatory** when `deposit_amount < collected_amount` |
| `bank_charge` | decimal | No | Deducted by the bank → SAP `BankChargeAmount` |
| `currency` | string | No | Default `INR` |
| `slip_number` | string | No | Bank slip reference → SAP `Reference` |
| `remarks` | string | No | Free text |
| `receipt_ids` | int[] | **Yes** | ≥ 1. Must be `POSTED`, in the same company, and not already banked |

> `collected_amount` is **not** accepted from the client — it is summed from
> the selected receipts inside the create transaction.

---

## 3. Success response

```
HTTP 201 Created
```

```json
{
  "success": true,
  "message": "Deposit created.",
  "data": {
    "id": 9,
    "deposit_no": "DEP-OIL-2026-27-000045",
    "company": "OIL",
    "deposit_date": "2026-07-31",
    "deposited_by": 3,
    "deposited_by_name": "Navneet",
    "bank_account": 1,
    "bank_account_name": "HDFC Bank — ****4821",
    "deposit_type": "CHEQUE",
    "collected_amount": "125000.00",
    "deposit_amount": "120000.00",
    "shortfall": "5000.00",
    "shortfall_reason": "₹5,000 retained as branch petty cash float.",
    "bank_charge": "0.00",
    "currency": "INR",
    "slip_number": "DEP-SLIP-88213",
    "remarks": "Deposited at HDFC Ludhiana Mall Road branch.",
    "status": "DRAFT",
    "status_display": "Draft",
    "sap_doc_entry": null,
    "sap_doc_num": null,
    "sap_posted_at": null,
    "lines": [
      { "id": 15, "receipt": 41, "receipt_no": "RCP-OIL-2026-27-000123",
        "card_name": "PURAN STORE", "amount": "125000.00" }
    ],
    "attachments": [],
    "approval": null,
    "created_at": "2026-07-31T10:02:11.884Z",
    "updated_at": "2026-07-31T10:02:11.884Z"
  }
}
```

### Submit — `POST /api/payments/deposits/{id}/submit/`

```
HTTP 200 OK
```

```json
{
  "success": true,
  "message": "Submitted for approval.",
  "data": {
    "id": 9,
    "deposit_no": "DEP-OIL-2026-27-000045",
    "status": "PENDING_APPROVAL",
    "approval": { "id": 18, "status": "PENDING",
                  "current_level": 1, "total_levels": 2,
                  "level_label": "Level 1 of 2" }
  }
}
```

### Attachment upload — `POST /api/payments/deposits/{id}/attachments/`

`multipart/form-data`, fields `file` and `attachment_type`
(`DEPOSIT_SLIP` \| `DEPOSIT_RECEIPT`). Same validation as payments: jpg / jpeg /
png / pdf, ≤ 5 MB, magic-byte checked. Written flat to
`\\JIVO-APP\Payments\Deposit_Payments\<uuid>.<ext>`.

```
HTTP 201 Created
```

```json
{
  "success": true, "message": "File uploaded.",
  "data": { "id": 14, "attachment_type": "DEPOSIT_SLIP",
            "type_display": "Bank deposit slip",
            "stored_name": "d8129abc9134be21ac02.jpg",
            "original_name": "slip_88213.jpg",
            "download_url": "/api/payments/attachments/14/download/",
            "created_at": "2026-07-31T10:05:02.114Z" }
}
```

---

## 4. Error responses

### 400 — validation

```json
{
  "success": false,
  "message": "Could not create the deposit.",
  "data": null,
  "errors": {
    "shortfall_reason": ["A reason is required when depositing less than collected."]
  }
}
```

Other 400s:

```json
{ "errors": { "deposit_amount": ["Cannot deposit 500000; only 125000 was collected."] } }
```
```json
{ "errors": { "receipt_ids": ["Already deposited: RCP-OIL-2026-27-000101."] } }
```
```json
{ "errors": { "receipt_ids": ["One or more payments do not exist in the selected company."] } }
```
```json
{ "success": false, "message": "A draft deposit cannot be submitted.", "data": null }
```

### 401 — unauthenticated

```json
{ "detail": "Authentication credentials were not provided." }
```

### 403 — forbidden

```json
{ "success": false,
  "message": "You do not have permission to view this file.", "data": null }
```

### 404 — not found

```json
{ "detail": "No BankDeposit matches the given query." }
```

### 409 — conflict (surfaced as 400 with this message)

```json
{ "success": false,
  "message": "This document already has an open approval request.",
  "data": null }
```

A partial unique index on `approval_request(content_type, object_id)` where
`status IN ('DRAFT','PENDING')` makes two concurrent chains impossible.

### 500 / 502

```json
{ "success": false,
  "message": "The file store is currently unavailable.", "data": null }
```

---

## 5. Database changes

### `POST /api/payments/deposits/` (one transaction)

| Table | Operation | Detail |
|---|---|---|
| `core_document_counter` | **UPDATE** | Row-locked; allocates `DEP-<COMPANY>-<FY>-NNNNNN` |
| `payment_bank_deposit` | **INSERT** | 1 row; `collected_amount` summed from receipts; `idempotency_key` + `company_db` set |
| `payment_bank_deposit_line` | **INSERT** | 1 per receipt (bulk) |
| `payment_receipt` | **UPDATE** | `deposit_id` set on each selected receipt |
| `payment_status_history` | **INSERT** | `→ DRAFT` |

### `POST /api/payments/deposits/{id}/submit/`

| Table | Operation |
|---|---|
| `approval_request` | **INSERT** (UPDATE on resubmit) |
| `approval_action` | **INSERT** — `SUBMIT` |
| `payment_bank_deposit` | **UPDATE** — `→ PENDING_APPROVAL` |
| `payment_status_history` | **INSERT** |

### Final approval

| Table | Operation |
|---|---|
| `approval_action` | **INSERT** — `APPROVE` |
| `approval_request` | **UPDATE** — `→ APPROVED` |
| `payment_bank_deposit` | **UPDATE** — `→ QUEUED` |
| `payment_sap_outbox` | **INSERT** — `POST_DEPOSIT` |
| `payment_status_history` | **INSERT** |

### Outbox worker

| Table | Operation |
|---|---|
| `payment_sap_outbox` | **UPDATE** — `IN_FLIGHT` → terminal state |
| `payment_sap_call_log` | **INSERT** — per attempt |
| `payment_bank_deposit` | **UPDATE** — `sap_doc_entry`, `sap_doc_num`, `→ POSTED` |
| `payment_status_history` | **INSERT** |

**No DELETEs.** `BankDepositLine` has a unique constraint on `receipt`, so a
receipt can never be banked twice.

---

## 6. SAP API called

### `POST /b1s/v2/Deposits`

**Headers**

```
Content-Type: application/json
Cookie: B1SESSION=<session-id>; ROUTEID=<route>
```

**Payload — cheque deposit** (references cheques SAP already holds):

```json
{
  "DepositType": "dt_Check",
  "DepositDate": "2026-07-31",
  "BankAccount": "_SYS00000000131",
  "DepositCurrency": "INR",
  "BankChargeAmount": 0.00,
  "Reference": "DEP-SLIP-88213",
  "Remarks": "OMS DEP-OIL-2026-27-000045",
  "U_OMS_IDEM": "d4e5f6a7-1234-4b8c-9d0e-1f2a3b4c5d77",
  "U_OMS_REF": "DEP-OIL-2026-27-000045",
  "DepositChecks": [
    { "CheckKey": 901 },
    { "CheckKey": 914 }
  ]
}
```

**Payload — cash deposit** (no cheque keys, so a total is sent instead):

```json
{
  "DepositType": "dt_Cash",
  "DepositDate": "2026-07-31",
  "BankAccount": "_SYS00000000131",
  "DepositCurrency": "INR",
  "TotalLC": 120000.00,
  "BankChargeAmount": 150.00,
  "Reference": "DEP-SLIP-88214",
  "U_OMS_IDEM": "e5f6a7b8-2345-4c9d-8e1f-2a3b4c5d6e88",
  "U_OMS_REF": "DEP-OIL-2026-27-000046"
}
```

**Expected response — HTTP 201**

```json
{
  "AbsoluteEntry": 771,
  "DepositNumber": 771,
  "DepositType": "dt_Check",
  "DepositDate": "2026-07-31",
  "BankAccount": "_SYS00000000131",
  "TotalLC": 120000.0,
  "U_OMS_IDEM": "d4e5f6a7-1234-4b8c-9d0e-1f2a3b4c5d77",
  "DepositChecks": [ { "CheckKey": 901 }, { "CheckKey": 914 } ]
}
```

**Fields consumed**

| SAP field | Stored in |
|---|---|
| `AbsoluteEntry` | `payment_bank_deposit.sap_doc_entry` |
| `DepositNumber` | `payment_bank_deposit.sap_doc_num` |

> Deposits return `AbsoluteEntry` / `DepositNumber`, **not** `DocEntry` /
> `DocNum` as Incoming Payments do. The worker reads both key names.

### Deferred posting

If a cheque deposit is approved before its underlying receipts have posted,
the outbox row is scheduled 5 minutes out rather than failed — it is an
ordering dependency, not an error. It retries until the `CheckKey`s exist.

---

## 7. SAP table verification

| Table | Meaning | Expected |
|---|---|---|
| `ODPS` | Deposit header | 1 new row; `AbsEntry` = returned `AbsoluteEntry`; `U_OMS_REF` = our deposit no |
| `DPS1` | Deposit cheque lines | 1 row per `DepositChecks[]` entry (cheque deposits only) |
| `RCT1` | Cheque register | `Deposited` flips to `Y`; `DpsNum` points at the deposit |
| `OACT` | GL accounts | Bank account balance increases |
| `OJDT` / `JDT1` | Journal entry | Auto-created; debit bank, credit the clearing account |

A **cash** deposit writes `ODPS` and the journal but **no** `DPS1` rows — that
is correct, not a missing write.

---

## 8. SAP SQL / HANA queries

Replace `<SCHEMA>` with the company DB.

### 8.1 Find the deposit

```sql
SELECT "AbsEntry", "DepositNum", "DeposDate", "DeposType",
       "TotalLC", "BankChrg", "Ref", "U_OMS_REF", "U_OMS_IDEM"
FROM "<SCHEMA>"."ODPS"
WHERE "U_OMS_REF" = 'DEP-OIL-2026-27-000045';

-- Most recent deposits
SELECT "AbsEntry", "DepositNum", "DeposDate", "TotalLC", "U_OMS_REF"
FROM "<SCHEMA>"."ODPS"
ORDER BY "DepositNum" DESC
LIMIT 20;
```

### 8.2 Deposit cheque lines

```sql
SELECT "AbsEntry", "LineNum", "CheckKey", "CheckSum", "CheckNum", "BankCode"
FROM "<SCHEMA>"."DPS1"
WHERE "AbsEntry" = <ODPS.AbsEntry>
ORDER BY "LineNum";

-- Or the most recent overall
SELECT * FROM "<SCHEMA>"."DPS1" ORDER BY "AbsEntry" DESC LIMIT 20;
```

### 8.3 Cheques marked deposited

```sql
SELECT "DocNum" AS "PaymentDocEntry", "LineID", "CheckKey",
       "CheckNum", "CheckSum", "Deposited", "DpsNum"
FROM "<SCHEMA>"."RCT1"
WHERE "CheckKey" IN (901, 914);
```

Expect `Deposited = 'Y'` and `DpsNum` = the deposit number.

### 8.4 Bank GL balance

```sql
SELECT "AcctCode", "AcctName", "CurrTotal"
FROM "<SCHEMA>"."OACT"
WHERE "AcctCode" = '_SYS00000000131';
```

### 8.5 Journal entry

```sql
SELECT T0."AbsEntry", T0."DepositNum", T1."TransId",
       T1."Account", T1."Debit", T1."Credit"
FROM "<SCHEMA>"."ODPS" T0
JOIN "<SCHEMA>"."JDT1" T1 ON T1."TransId" = T0."TransId"
WHERE T0."AbsEntry" = <ODPS.AbsEntry>;
```

### 8.6 Duplicate check

```sql
-- MUST return zero rows.
SELECT "U_OMS_IDEM", COUNT(*) AS "Copies"
FROM "<SCHEMA>"."ODPS"
WHERE "U_OMS_IDEM" IS NOT NULL AND "U_OMS_IDEM" <> ''
GROUP BY "U_OMS_IDEM"
HAVING COUNT(*) > 1;
```

### 8.7 OMS side (PostgreSQL)

```sql
SELECT id, deposit_no, company, status, collected_amount, deposit_amount,
       collected_amount - deposit_amount AS shortfall,
       shortfall_reason, sap_doc_entry, sap_doc_num
FROM payment_bank_deposit
WHERE deposit_no = 'DEP-OIL-2026-27-000045';

-- The receipts inside it, and their cheque keys
SELECT l.id, r.receipt_no, l.amount, m.method, m.cheque_number, m.sap_check_key
FROM payment_bank_deposit_line l
JOIN payment_receipt r      ON r.id = l.receipt_id
LEFT JOIN payment_method_entry m ON m.receipt_id = r.id
WHERE l.deposit_id = <deposit_id>;

-- No receipt banked twice (must return zero rows)
SELECT receipt_id, COUNT(*) FROM payment_bank_deposit_line
GROUP BY receipt_id HAVING COUNT(*) > 1;
```

---

## 9. Verification checklist

**API**
- [ ] `GET /depositable-receipts/` lists only `POSTED`, un-banked receipts
- [ ] `POST /deposits/` returns **201** with a `deposit_no`
- [ ] `collected_amount` equals the sum of the selected receipts
- [ ] Depositing more than collected is rejected (**400**)
- [ ] Short deposit with no reason is rejected (**400**)
- [ ] Re-using an already-banked receipt is rejected (**400**)

**OMS database**
- [ ] `payment_bank_deposit` — 1 row, correct amounts
- [ ] `payment_bank_deposit_line` — 1 per receipt
- [ ] `payment_receipt.deposit_id` set on each
- [ ] Duplicate-line query returns zero rows
- [ ] `payment_status_history` — a row per transition

**Approval**
- [ ] `approval_request` created against the `DEPOSIT` workflow
- [ ] `level_label` reads "Level 1 of N"
- [ ] Approving advances / completes correctly

**Outbox → SAP**
- [ ] Outbox row created on final approval
- [ ] Cheque deposit waits when `sap_check_key` is missing, then posts
- [ ] `drain_sap_outbox` reports `SUCCEEDED`

**SAP**
- [ ] `ODPS` has 1 row with matching `U_OMS_REF`
- [ ] `DPS1` has 1 row per cheque (cheque deposits)
- [ ] `RCT1.Deposited = 'Y'` for those cheques
- [ ] Bank GL balance increased
- [ ] Duplicate query (§8.6) returns zero rows

**Write-back**
- [ ] `sap_doc_entry` (= `AbsoluteEntry`) and `sap_doc_num` stored
- [ ] Deposit status is `POSTED`

**Attachments**
- [ ] Slip uploads to `Deposit_Payments` with a UUID name
- [ ] Download is permission-checked

---

## 10. Postman collection

Variables: `base_url`, `token`, `approver_token`.

### 10.1 List depositable receipts

```
GET {{base_url}}/api/payments/depositable-receipts/?company=OIL
Authorization: Bearer {{token}}
```
Expect **200**. Note the ids for the next call.

### 10.2 Create deposit

```
POST {{base_url}}/api/payments/deposits/
Authorization: Bearer {{token}}
Content-Type: application/json
```
Body: see §2.2. Expect **201**. Save `data.id` → `{{deposit_id}}`.

### 10.3 Upload the slip

```
POST {{base_url}}/api/payments/deposits/{{deposit_id}}/attachments/
Authorization: Bearer {{token}}
Content-Type: multipart/form-data

file            : <slip.jpg>
attachment_type : DEPOSIT_SLIP
```
Expect **201**.

### 10.4 Submit

```
POST {{base_url}}/api/payments/deposits/{{deposit_id}}/submit/
Authorization: Bearer {{token}}
```
Expect **200**, `status = PENDING_APPROVAL`. Save `data.approval.id`.

### 10.5 Approve

```
POST {{base_url}}/api/approvals/requests/{{approval_id}}/act/
Authorization: Bearer {{approver_token}}
Content-Type: application/json

{ "decision": "APPROVE", "remarks": "Slip verified." }
```

### 10.6 Drain and confirm

```bash
python manage.py drain_sap_outbox
```

```
GET {{base_url}}/api/payments/deposits/{{deposit_id}}/
Authorization: Bearer {{token}}
```
Expect `status = POSTED` with `sap_doc_entry` populated.

---

## 11. Common issues

| Symptom | Cause | Fix |
|---|---|---|
| **400 "Already deposited: RCP-…"** | A selected receipt is already in another deposit | Refresh the depositable list; `BankDepositLine.receipt` is unique |
| **400 "One or more payments do not exist in the selected company"** | Mixed companies, or a bad id | All receipts must share the deposit's company |
| **400 "Cannot deposit X; only Y was collected"** | `deposit_amount` > sum of receipts | Reduce the amount |
| **400 "A reason is required…"** | Short deposit with blank `shortfall_reason` | Supply a reason — the approver sees it |
| **Deposit stuck in `QUEUED`** | Cheques not yet posted, so no `CheckKey` | Expected. It retries every 5 min. Confirm the receipts reach `POSTED` |
| **SAP error: "Check is already deposited"** | The cheque was banked in SAP outside OMS | Check `RCT1.Deposited`; cancel the OMS deposit and reconcile |
| **SAP error: invalid `BankAccount`** | Wrong GL code on the bank account master | Fix `payment_bank_account.sap_gl_account` (must be the `_SYS…` code) |
| **`ODPS` row created but `DPS1` empty** | Cash deposit | Correct — cash deposits carry `TotalLC`, not cheque lines |
| **Outbox `NEEDS_REVIEW`** | Could not verify whether SAP committed | Run §8.1 by `U_OMS_REF`. If the row exists the deposit posted — reconcile, do **not** blind-retry |
| **401 / 403** | Token expired, or company not assigned | Re-login; check `UserPartyAssignment` |
| **502 file store unavailable** | Share unreachable | Verify `DEPOSIT_PAYMENTS_IMAGES` and SMB credentials |
