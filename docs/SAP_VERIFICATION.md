# SAP VERIFICATION — Payments & Deposits

How to prove, end to end, that an OMS payment or deposit really reached SAP and
that the numbers are right. Written so another developer can verify the module
without reading the source.

Companion documents: [`PAYMENT_API.md`](PAYMENT_API.md),
[`DEPOSIT_API.md`](DEPOSIT_API.md), [`APPROVAL_API.md`](APPROVAL_API.md).

---

## Table of contents

1. [Prerequisites](#1-prerequisites-do-these-first)
2. [The posting flow](#2-the-posting-flow)
3. [SAP APIs called](#3-sap-apis-called)
4. [SAP tables affected](#4-sap-tables-affected)
5. [Verification queries — Incoming Payment](#5-verification-queries--incoming-payment)
6. [Verification queries — Deposit](#6-verification-queries--deposit)
7. [OMS-side queries](#7-oms-side-queries)
8. [Full end-to-end walkthrough](#8-full-end-to-end-walkthrough)
9. [Idempotency / duplicate testing](#9-idempotency--duplicate-testing)
10. [Verification checklist](#10-verification-checklist)
11. [Common issues](#11-common-issues)

---

## 1. Prerequisites (do these first)

> ### 🔴 Blocking — nothing works without these

**1.1 Create the UDFs in SAP.** On **`ORCT` and `ODPS`, in every company DB**:

| UDF | Type | Length |
|---|---|---|
| `U_OMS_IDEM` | Alphanumeric | 36 |
| `U_OMS_REF` | Alphanumeric | 50 |

SAP B1 client → *Tools → Customisation Tools → User-Defined Fields –
Management → Marketing Documents → Payments*.

`U_OMS_IDEM` carries the idempotency key. Without a **queryable** field there
is no way to answer "did that timed-out post actually commit?", and the choice
becomes duplicate payment or lost payment. This is not optional.

**1.2 Seed `payment_sap_company_map`** (Django admin → Payments → SAP company
mappings) — one row per company:

| company | company_db | hana_schema |
|---|---|---|
| OIL | `JIVO_OIL_HANADB` | `JIVO_OIL_HANADB` |
| BEVERAGES | `JIVO_BEVERAGES_HANADB` | `JIVO_BEVERAGES_HANADB` |
| MART | *(real MART db)* | *(same)* |

> `HANA_COMPANY_DB_MART` is referenced at `hana/services/connection.py:74,90`
> but has never been defined in settings, so MART data is invisible today.
> This table is where that is fixed.

**1.3 Seed `payment_bank_account`** with the real SAP GL codes (`_SYS…`).
Cash → `CashAccount`, UPI → `TransferAccount`, cheque → `CheckAccount`.

**1.4 Configure `CACHES` (Redis).** With no `CACHES` block Django falls back to
`LocMemCache`, so each Gunicorn worker holds its own SAP session and cache
invalidation reaches only one worker.

**1.5 Confirm the Service Layer user** can `POST /IncomingPayments` and
`/Deposits` in each company DB.

---

## 2. The posting flow

```
 OMS                                              SAP
 ───                                              ───
 1. POST /api/payments/receipts/
      └─ payment_receipt (DRAFT)
         idempotency_key = UUID  ← created BEFORE any network call

 2. POST /receipts/{id}/submit/
      └─ approval_request (PENDING, Level 1 of N)

 3. POST /api/approvals/requests/{id}/act/  (final level)
      ├─ approval_request → APPROVED
      ├─ payment_receipt  → QUEUED          ┐ ONE
      └─ payment_sap_outbox INSERT          ┘ transaction

 4. python manage.py drain_sap_outbox
      ├─ claim row (select_for_update skip_locked + 5-min lease)
      ├─ if attempts > 0:
      │     GET /IncomingPayments?$filter=U_OMS_IDEM eq '<uuid>'
      │       found    → adopt DocEntry, DO NOT post again
      │       missing  → safe to post
      │       query failed → NEEDS_REVIEW (never guess)
      ├─ POST /IncomingPayments  ─────────────────►  ORCT / RCT1 / RCT2
      │                          ◄───────────────── 201 { DocEntry, DocNum,
      │                                                   PaymentChecks[].CheckKey }
      └─ payment_receipt → POSTED, keys stored
```

The same shape applies to deposits, with `POST /Deposits` and `ODPS` / `DPS1`.

---

## 3. SAP APIs called

| # | Method | URL | When |
|---|---|---|---|
| 1 | `POST` | `/b1s/v2/Login` | Session per company DB, cached |
| 2 | `GET` | `/b1s/v2/IncomingPayments?$filter=U_OMS_IDEM eq '<uuid>'&$top=1` | Before every retry |
| 3 | `POST` | `/b1s/v2/IncomingPayments` | Post a receipt |
| 4 | `GET` | `/b1s/v2/IncomingPayments({DocEntry})` | Read back `CheckKey`s |
| 5 | `GET` | `/b1s/v2/Deposits?$filter=U_OMS_IDEM eq '<uuid>'&$top=1` | Before every retry |
| 6 | `POST` | `/b1s/v2/Deposits` | Post a deposit |

### 3.1 Login

```http
POST /b1s/v2/Login
Content-Type: application/json

{ "CompanyDB": "JIVO_OIL_HANADB", "UserName": "oms_service", "Password": "••••" }
```

```json
{ "SessionId": "b1s-9f3c2a…", "Version": "1000200", "SessionTimeout": 30 }
```

Response also sets `B1SESSION` and `ROUTEID` cookies.

> **`SessionTimeout` is in MINUTES.** The cache TTL is computed as
> `max(60, minutes * 60 - 120)`. The older `serviceLayer/service.py:35` treats
> it as seconds, which is why that client serves dead sessions; the payments
> client does not repeat it. The session cache key is also **per company DB** —
> a global key would hand an OIL session to a BEVERAGES post.

### 3.2 Incoming Payment — full request

```http
POST /b1s/v2/IncomingPayments
Content-Type: application/json
Cookie: B1SESSION=b1s-9f3c2a…; ROUTEID=.node0
```

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
    { "CheckNumber": 458812, "BankCode": "HDFC Bank", "CheckSum": 25000.00,
      "DueDate": "2026-08-05", "CheckAccount": "_SYS00000000131" }
  ],
  "PaymentInvoices": [
    { "LineNum": 0, "DocEntry": 45012, "InvoiceType": "it_Invoice", "SumApplied": 70000.00 },
    { "LineNum": 1, "DocEntry": 45188, "InvoiceType": "it_Invoice", "SumApplied": 55000.00 }
  ]
}
```

Returned fields consumed: **`DocEntry`**, **`DocNum`**, and
**`PaymentChecks[].CheckKey`** (needed to deposit that cheque later).

### 3.3 Deposit — full request

```http
POST /b1s/v2/Deposits
Content-Type: application/json
Cookie: B1SESSION=…; ROUTEID=…
```

```json
{
  "DepositType": "dt_Check",
  "DepositDate": "2026-07-31",
  "BankAccount": "_SYS00000000131",
  "DepositCurrency": "INR",
  "BankChargeAmount": 0.00,
  "Reference": "DEP-SLIP-88213",
  "U_OMS_IDEM": "d4e5f6a7-1234-4b8c-9d0e-1f2a3b4c5d77",
  "U_OMS_REF": "DEP-OIL-2026-27-000045",
  "DepositChecks": [ { "CheckKey": 901 }, { "CheckKey": 914 } ]
}
```

Returned: **`AbsoluteEntry`** and **`DepositNumber`** (not `DocEntry`/`DocNum`).

---

## 4. SAP tables affected

### Incoming Payment

| Table | Contents | Rows created |
|---|---|---|
| `ORCT` | Payment header | 1 |
| `RCT1` | **Cheque** lines | 1 per cheque (none for cash/UPI) |
| `RCT2` | **Invoice allocation** lines | 1 per `PaymentInvoices[]` |
| `OINV` | Invoices settled | *updated* — `PaidToDate` rises, `DocStatus` → `C` when clear |
| `OCRD` | Business partner | *updated* — `Balance` falls |
| `OJDT` / `JDT1` | Journal entry | Auto-created by SAP |

> **`RCT1` = cheques, `RCT2` = invoice allocations.** A cash-only payment writes
> `ORCT` + `RCT2` and **nothing** in `RCT1`. That is correct, not a missing write.

### Deposit

| Table | Contents | Rows created |
|---|---|---|
| `ODPS` | Deposit header | 1 |
| `DPS1` | Deposit cheque lines | 1 per cheque (none for cash) |
| `RCT1` | Cheque register | *updated* — `Deposited` → `Y`, `DpsNum` set |
| `OACT` | GL accounts | *updated* — bank balance rises |
| `OJDT` / `JDT1` | Journal entry | Auto-created |

---

## 5. Verification queries — Incoming Payment

Replace `<SCHEMA>` with the company DB, e.g. `JIVO_OIL_HANADB`.

### 5.1 Locate the payment

```sql
-- By our own reference (most reliable)
SELECT "DocEntry", "DocNum", "DocDate", "CardCode", "CardName",
       "DocTotal", "CashSum", "TrsfrSum", "Canceled",
       "U_OMS_REF", "U_OMS_IDEM"
FROM "<SCHEMA>"."ORCT"
WHERE "U_OMS_REF" = 'RCP-OIL-2026-27-000123';

-- Latest payments
SELECT "DocEntry", "DocNum", "DocDate", "CardCode", "DocTotal", "U_OMS_REF"
FROM "<SCHEMA>"."ORCT"
ORDER BY "DocEntry" DESC
LIMIT 20;
```

Expect exactly **one** row. `DocEntry` must equal
`payment_receipt.sap_doc_entry`.

### 5.2 Cheque lines

```sql
SELECT "DocNum" AS "PaymentDocEntry", "LineID", "CheckKey",
       "CheckNum", "BankCode", "CheckSum", "DueDate", "CheckAct",
       "Deposited", "DpsNum"
FROM "<SCHEMA>"."RCT1"
WHERE "DocNum" = <ORCT.DocEntry>;
```

### 5.3 Invoice allocations

```sql
SELECT "DocNum"  AS "PaymentDocEntry",
       "LineID",
       "DocEntry" AS "InvoiceDocEntry",
       "InvType", "SumApplied", "AppliedSys"
FROM "<SCHEMA>"."RCT2"
WHERE "DocNum" = <ORCT.DocEntry>;
```

> **The column naming is counter-intuitive and trips people up.**
> In `RCT2`, `"DocNum"` is the **payment's DocEntry** and `"DocEntry"` is the
> **invoice's DocEntry**. Getting this backwards is the most common cause of a
> "verification returned nothing" report.

### 5.4 Invoice balances updated

```sql
SELECT "DocEntry", "DocNum", "CardCode", "DocDate", "DocDueDate",
       "DocTotal", "PaidToDate",
       "DocTotal" - IFNULL("PaidToDate", 0) AS "BalanceDue",
       "DocStatus", "CANCELED"
FROM "<SCHEMA>"."OINV"
WHERE "DocEntry" IN (45012, 45188);
```

`PaidToDate` should have risen by exactly `SumApplied`; `DocStatus` becomes
`'C'` once the balance reaches zero.

### 5.5 Reconcile payment vs allocations

```sql
-- Header total must equal the sum of its allocations
SELECT T0."DocEntry", T0."DocTotal",
       SUM(T1."SumApplied") AS "TotalApplied",
       T0."DocTotal" - SUM(T1."SumApplied") AS "Unallocated"
FROM "<SCHEMA>"."ORCT" T0
LEFT JOIN "<SCHEMA>"."RCT2" T1 ON T1."DocNum" = T0."DocEntry"
WHERE T0."U_OMS_REF" = 'RCP-OIL-2026-27-000123'
GROUP BY T0."DocEntry", T0."DocTotal";
```

`Unallocated` should be 0 for an invoice-linked payment, or the full amount for
an advance.

### 5.6 Business-partner balance

```sql
SELECT "CardCode", "CardName", "Balance", "OrdersBal"
FROM "<SCHEMA>"."OCRD"
WHERE "CardCode" = 'CUSTA000878';
```

### 5.7 Journal entry

```sql
SELECT T0."DocEntry", T0."DocNum", T0."TransId",
       T1."Line_ID", T1."Account", T1."ShortName",
       T1."Debit", T1."Credit"
FROM "<SCHEMA>"."ORCT" T0
JOIN "<SCHEMA>"."JDT1" T1 ON T1."TransId" = T0."TransId"
WHERE T0."DocEntry" = <ORCT.DocEntry>
ORDER BY T1."Line_ID";
```

Debits and credits must balance.

---

## 6. Verification queries — Deposit

### 6.1 Locate the deposit

```sql
SELECT "AbsEntry", "DepositNum", "DeposDate", "DeposType",
       "TotalLC", "BankChrg", "Ref", "U_OMS_REF", "U_OMS_IDEM"
FROM "<SCHEMA>"."ODPS"
WHERE "U_OMS_REF" = 'DEP-OIL-2026-27-000045';

SELECT "AbsEntry", "DepositNum", "DeposDate", "TotalLC", "U_OMS_REF"
FROM "<SCHEMA>"."ODPS"
ORDER BY "DepositNum" DESC
LIMIT 20;
```

### 6.2 Deposit cheque lines

```sql
SELECT "AbsEntry", "LineNum", "CheckKey", "CheckNum", "CheckSum", "BankCode"
FROM "<SCHEMA>"."DPS1"
WHERE "AbsEntry" = <ODPS.AbsEntry>
ORDER BY "LineNum";

SELECT * FROM "<SCHEMA>"."DPS1" ORDER BY "AbsEntry" DESC LIMIT 20;
```

### 6.3 Cheques marked as deposited

```sql
SELECT "DocNum" AS "PaymentDocEntry", "LineID", "CheckKey",
       "CheckNum", "CheckSum", "Deposited", "DpsNum"
FROM "<SCHEMA>"."RCT1"
WHERE "CheckKey" IN (901, 914);
```

Expect `Deposited = 'Y'` and `DpsNum` = the deposit number.

### 6.4 Deposit total reconciles to its cheques

```sql
SELECT T0."AbsEntry", T0."DepositNum", T0."TotalLC",
       SUM(T1."CheckSum") AS "SumOfChecks",
       T0."TotalLC" - IFNULL(SUM(T1."CheckSum"), 0) AS "Difference"
FROM "<SCHEMA>"."ODPS" T0
LEFT JOIN "<SCHEMA>"."DPS1" T1 ON T1."AbsEntry" = T0."AbsEntry"
WHERE T0."U_OMS_REF" = 'DEP-OIL-2026-27-000045'
GROUP BY T0."AbsEntry", T0."DepositNum", T0."TotalLC";
```

### 6.5 Bank GL balance

```sql
SELECT "AcctCode", "AcctName", "CurrTotal"
FROM "<SCHEMA>"."OACT"
WHERE "AcctCode" = '_SYS00000000131';
```

---

## 7. OMS-side queries

Run against **PostgreSQL**.

```sql
-- The receipt and its SAP keys
SELECT id, receipt_no, company, company_db, status,
       total_amount, allocated_amount,
       sap_doc_entry, sap_doc_num, sap_posted_at, idempotency_key
FROM payment_receipt
WHERE receipt_no = 'RCP-OIL-2026-27-000123';

-- Children
SELECT id, method, amount, cheque_number, cheque_date, sap_check_key
FROM payment_method_entry WHERE receipt_id = <id>;

SELECT denomination, quantity, denomination * quantity AS line_total
FROM payment_cash_denomination
WHERE entry_id IN (SELECT id FROM payment_method_entry WHERE receipt_id = <id>);

SELECT sap_doc_entry, sap_doc_num, amount_applied, balance_at_selection
FROM payment_invoice_allocation WHERE receipt_id = <id>;

-- Outbox state
SELECT id, operation, status, attempts, max_attempts,
       next_attempt_at, last_http_status, last_error
FROM payment_sap_outbox
WHERE idempotency_key = '<uuid>';

-- Every SAP attempt (request bodies are redacted)
SELECT attempt_number, http_method, endpoint, http_status,
       sap_error_code, status, duration_ms, created_at
FROM payment_sap_call_log
WHERE idempotency_key = '<uuid>'
ORDER BY created_at;

-- Lifecycle
SELECT from_status, to_status, actor_kind, changed_by_username, reason, created_at
FROM payment_status_history
WHERE object_id = <id>
  AND content_type_id = (SELECT id FROM django_content_type
                         WHERE app_label='payments' AND model='paymentreceipt')
ORDER BY created_at;
```

### Cross-system reconciliation

```sql
-- OMS rows that claim POSTED but carry no SAP key (must return zero)
SELECT id, receipt_no, status, sap_doc_entry
FROM payment_receipt
WHERE status = 'POSTED' AND sap_doc_entry IS NULL;

-- Approved > 15 min ago but never posted — the sweep target
SELECT r.id, r.receipt_no, r.status, o.status AS outbox_status, o.last_error
FROM payment_receipt r
LEFT JOIN payment_sap_outbox o ON o.object_id = r.id
WHERE r.status IN ('QUEUED', 'FAILED')
  AND r.updated_at < NOW() - INTERVAL '15 minutes';
```

---

## 8. Full end-to-end walkthrough

Run against a **sandbox company DB**, never production.

**Step 1 — create.** `POST /api/payments/receipts/` (see PAYMENT_API §3).
Expect **201**. Note `receipt_no` and `id`.

```sql
SELECT receipt_no, status, total_amount, idempotency_key
FROM payment_receipt WHERE id = <id>;   -- status = DRAFT
```

**Step 2 — attach.** Upload a cheque image. Expect **201**, and the file to
exist on `\\JIVO-APP\Payments\Receive_Payments\<uuid>.jpg`.

**Step 3 — submit.** `POST /receipts/{id}/submit/`. Expect **200**,
`status = PENDING_APPROVAL`, `level_label = "Level 1 of 2"`.

**Step 4 — approve L1** as an accountant. Expect `Level 2 of 2`.

**Step 5 — self-approval check.** Try to approve as the submitter → **403**.

**Step 6 — approve L2** as a manager. Expect `APPROVED`.

```sql
SELECT status FROM payment_receipt WHERE id = <id>;      -- QUEUED
SELECT status, attempts FROM payment_sap_outbox
WHERE object_id = <id>;                                  -- PENDING, 0
```

**Step 7 — post.**

```bash
python manage.py drain_sap_outbox
```

Expect `outbox <n>: SUCCEEDED`.

**Step 8 — verify OMS.**

```sql
SELECT status, sap_doc_entry, sap_doc_num, sap_posted_at
FROM payment_receipt WHERE id = <id>;    -- POSTED + both keys
```

**Step 9 — verify SAP.** Run §5.1 → §5.7. Confirm one `ORCT` row, correct
`RCT2` allocations, `RCT1` cheque rows, `OINV.PaidToDate` increased, journal
balanced.

**Step 10 — deposit.** Create a deposit from that receipt, approve, drain, then
run §6.1 → §6.5. Confirm `RCT1.Deposited = 'Y'`.

---

## 9. Idempotency / duplicate testing

**This is the single most important test in the suite.** It proves a lost
response cannot become a duplicate payment.

### 9.1 Simulated crash

1. Create + approve a receipt so an outbox row exists.
2. Start the worker and kill the process mid-post (or pull the network).
3. Check state — the row will be `IN_FLIGHT` with an unexpired lease:

```sql
SELECT status, attempts, locked_by, lease_expires_at
FROM payment_sap_outbox WHERE object_id = <id>;
```

4. Check whether SAP actually committed:

```sql
SELECT "DocEntry", "U_OMS_REF" FROM "<SCHEMA>"."ORCT"
WHERE "U_OMS_IDEM" = '<uuid>';
```

5. Wait for the lease to expire (5 min) and re-run the worker.
6. **Expected:** the probe finds the existing document, adopts its `DocEntry`
   and does **not** post again. The log shows
   `Outbox <n> already present in SAP; adopting DocEntry <x>`.

### 9.2 Duplicate scan — must return zero rows

```sql
-- Payments
SELECT "U_OMS_IDEM", COUNT(*) AS "Copies"
FROM "<SCHEMA>"."ORCT"
WHERE "U_OMS_IDEM" IS NOT NULL AND "U_OMS_IDEM" <> ''
GROUP BY "U_OMS_IDEM" HAVING COUNT(*) > 1;

-- Deposits
SELECT "U_OMS_IDEM", COUNT(*) AS "Copies"
FROM "<SCHEMA>"."ODPS"
WHERE "U_OMS_IDEM" IS NOT NULL AND "U_OMS_IDEM" <> ''
GROUP BY "U_OMS_IDEM" HAVING COUNT(*) > 1;

-- Same reference posted twice
SELECT "U_OMS_REF", COUNT(*) FROM "<SCHEMA>"."ORCT"
WHERE "U_OMS_REF" LIKE 'RCP-%'
GROUP BY "U_OMS_REF" HAVING COUNT(*) > 1;
```

### 9.3 Concurrent workers

Run two `drain_sap_outbox` processes simultaneously. `select_for_update(
skip_locked=True)` means each row is claimed once; the duplicate scan must
still return zero rows.

### 9.4 The ambiguous case

If the **verification query itself** fails, the row goes to `NEEDS_REVIEW` and
is **not** retried:

```sql
SELECT id, status, last_error FROM payment_sap_outbox WHERE status = 'NEEDS_REVIEW';
```

Resolve by hand: run §5.1 by `U_OMS_REF`. If the document exists the payment
posted — reconcile the OMS row. **Never blind-retry a `NEEDS_REVIEW` row.**

---

## 10. Verification checklist

**Prerequisites**
- [ ] `U_OMS_IDEM` and `U_OMS_REF` exist on `ORCT` and `ODPS` in every company DB
- [ ] `payment_sap_company_map` seeded for all three companies
- [ ] `payment_bank_account` has real `_SYS…` GL codes
- [ ] `CACHES` configured (Redis)
- [ ] Service Layer user can post in each company DB

**API**
- [ ] Receipt created — **201**
- [ ] Submitted — **200**, approval at Level 1 of N
- [ ] Approved through every level — **200**
- [ ] Self-approval → **403**; blank reject remarks → **400**

**OMS**
- [ ] `payment_receipt` / `payment_method_entry` / `payment_cash_denomination` /
      `payment_invoice_allocation` rows correct
- [ ] `approval_request` + `approval_action` per step
- [ ] `payment_sap_outbox` created on final approval
- [ ] `payment_sap_call_log` records each attempt
- [ ] `payment_status_history` records each transition

**SAP — payment**
- [ ] `ORCT` — exactly 1 row, matching `U_OMS_REF`
- [ ] `RCT2` — 1 row per allocation, amounts match
- [ ] `RCT1` — 1 row per cheque (absent for cash-only, correctly)
- [ ] `OINV.PaidToDate` increased by `SumApplied`
- [ ] `OINV.DocStatus` → `C` when fully settled
- [ ] `OCRD.Balance` decreased
- [ ] `JDT1` debits = credits

**SAP — deposit**
- [ ] `ODPS` — 1 row, matching `U_OMS_REF`
- [ ] `DPS1` — 1 row per cheque
- [ ] `RCT1.Deposited = 'Y'`, `DpsNum` set
- [ ] `OACT` bank balance increased

**Write-back**
- [ ] `sap_doc_entry` / `sap_doc_num` stored on both documents
- [ ] `sap_check_key` stored per cheque
- [ ] Statuses are `POSTED`

**Idempotency**
- [ ] Kill-and-restart adopts the existing DocEntry — no second document
- [ ] Duplicate scans (§9.2) return **zero** rows
- [ ] Concurrent workers produce no duplicates

**Attachments**
- [ ] Files land flat in the configured shares under UUID names
- [ ] Download is permission-checked; unauthorised → **403**
- [ ] Oversize and wrong-type uploads rejected

---

## 11. Common issues

| Symptom | Likely cause | Resolution |
|---|---|---|
| **Two `ORCT` rows for one receipt** | `U_OMS_IDEM` UDF missing, so the probe silently matches nothing | **Create the UDF.** Cancel the duplicate in SAP and reconcile |
| **Verification query returns nothing** | Using `RCT2."DocEntry"` as the payment key | In `RCT2`, `"DocNum"` = payment DocEntry, `"DocEntry"` = invoice DocEntry |
| **`RCT1` empty** | Cash/UPI-only payment | Correct — cheque lines only exist for cheques |
| **`DPS1` empty** | Cash deposit | Correct — cash deposits carry `TotalLC` |
| **SAP -5002 "applied amount exceeds open balance"** | Invoice was settled elsewhere between selection and posting | Classified **permanent** → outbox `DEAD`. Re-query open invoices and re-enter. `balance_at_selection` exists to catch this earlier |
| **SAP -10 "invalid CardCode"** | Wrong company DB for that card code | Check `payment_sap_company_map`; the same code differs per company |
| **SAP login fails** | Bad credentials or wrong `CompanyDB` | Verify `HANA_USERNAME` / `HANA_PASSWORD` and the mapping |
| **401 from SAP mid-run** | Session expired | Handled automatically: per-company key cleared, one re-login, retry |
| **Outbox stuck `PENDING`** | Worker not scheduled | Run `drain_sap_outbox`; schedule it every 15–30 s |
| **Outbox `FAILED`, attempts rising** | Transient (5xx / timeout) | Backs off 30 s → 1 h over 6 attempts. Check `last_error` |
| **Outbox `DEAD`** | Permanent 4xx | Fix the data, then use the admin "Re-queue" action |
| **Outbox `NEEDS_REVIEW`** | Could not verify whether SAP committed | Run §5.1 by `U_OMS_REF`. **Do not blind-retry** |
| **Deposit stuck `QUEUED`** | Cheques not yet posted → no `CheckKey` | Expected; retries every 5 min once the receipts post |
| **SAP "Check already deposited"** | Cheque banked in SAP outside OMS | Check `RCT1.Deposited`; cancel the OMS deposit and reconcile |
| **MART data invisible** | `HANA_COMPANY_DB_MART` never defined in settings | Add the MART row to `payment_sap_company_map` |
| **Different workers show different sessions** | `LocMemCache` — no shared cache | Configure Redis |
| **`HANA_DB_NAME not found` on startup** | Pre-existing gap: `.env` defines `HANA_DB_OIL_NAME` | Add `HANA_DB_NAME=<oil db>` to `.env` |
| **502 on open invoices** | HANA unreachable or bad schema | Check `payment_sap_company_map.hana_schema` and connectivity |
