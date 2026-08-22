# Payments & Deposits — Complete Flow

**Audience:** Tech Lead walkthrough.
**Scope:** every action a user can take on a Payment Receipt or a Bank Deposit, the exact table rows each action writes, and what happens in SAP before and after posting.

Every SQL statement below is **verified against the live schema**. Read queries are safe to run as-is; write examples are shown to explain what the code does — you do not run them by hand.

---

## 1. The two documents

| | Payment Receipt | Bank Deposit |
|---|---|---|
| Means | Money collected **from a party** | Collected money **handed to the bank** |
| Table | `payment_receipt` | `payment_bank_deposit` |
| Prefix | `RCP-OIL-20260821-000002` | `DEP-OIL-20260813-000006` |
| SAP object | Incoming Payment (`ORCT`) | Incoming Payment, **DocType 'A'** (`ORCT`) |
| Child table | `payment_method_entry` (tender lines) | `payment_bank_deposit_line` (which receipts) |

> **Point that surprises everyone:** a deposit is **not** a SAP `ODPS` Deposit. `ODPS` holds **0 rows** in all three live databases. A deposit posts as an *account-type transfer* Incoming Payment. Evidence is in `sap_payloads.py:196`.

### Table map

| Table | Holds |
|---|---|
| `payment_receipt` | Receipt header, status, SAP keys |
| `payment_method_entry` | One row per tender: CASH / UPI / CHEQUE |
| `payment_cash_denomination` | Note breakdown for cash |
| `payment_invoice_allocation` | Which SAP invoice each rupee settles |
| `payment_bank_deposit` | Deposit header |
| `payment_bank_deposit_line` | Receipts inside the deposit |
| `payment_status_history` | **The single audit timeline** for both documents |
| `payment_sap_call_log` | One row per HTTP call to SAP |
| `payment_sap_company_map` | `OIL/MART/BEVERAGES` → SAP DB, schema, cash G/L |

Live mapping:

| Company | SAP DB / HANA schema | Cash G/L |
|---|---|---|
| OIL | `TEST_JIVO_OIL_HANADB` | `1105003` |
| MART | `TEST_JIVO_MART_HANADB` | `1105003` |
| BEVERAGES | `TEST_JIVO_BEVERAGES_HANADB` | `1105003` |

---

## 2. Status lifecycle

Both documents share one set of statuses (`models.py:147` and `:430`):

```
DRAFT ──submit──> PENDING_APPROVAL ──approve──> APPROVED
  ^                      │                          │
  │                   reject                     post to SAP
  │                      v                          │
  └───────────────── REJECTED                       v
                                            POSTING_TO_SAP
                                          ┌───────┼────────┐
                                     SAP says   SAP says  SAP
                                       "yes"     "no"    silent
                                          │        │        │
                                          v        v        v
                                       POSTED  PENDING_  SAP_UNKNOWN
                                                ERROR
                                                  │
                                          fix & resubmit
                                                  │
                                                  v
                                          PENDING_APPROVAL
```

### The three failure states — the important distinction

| Status | SAP said | Document in SAP? | Can user resubmit? |
|---|---|---|---|
| `PENDING_ERROR` | **"No"** — explicit rejection | **No**, nothing committed | ✅ Yes — safe |
| `SAP_UNKNOWN` | **Nothing** — timeout / lost connection | **Unknown** | ❌ **Blocked** |
| `POSTING_TO_SAP` | Call in flight | Being decided | ❌ Blocked |

`SAP_UNKNOWN` exists to prevent **double-paying a party**. If SAP never answered, the payment may have committed; resubmitting could post it twice. Only reconciliation (`sap_poster.py:371`) resolves it, by asking SAP what actually happened.

A row **stuck** in `POSTING_TO_SAP` means the process died mid-call — treat it exactly like `SAP_UNKNOWN`.

---

## 3. Payment Receipt — action by action

### Action 1 — Create receipt

**Who:** collector with `Payments_Create`
**API:** `POST /api/payments/receipts/`
**Result:** status `DRAFT`. **No SAP contact whatsoever.**

Rows written, all in one transaction:

```sql
-- 1) header
INSERT INTO payment_receipt
  (receipt_no, company, company_db, card_code, card_name,
   payment_date, total_amount, allocated_amount, status, currency,
   created_by_id, created_at, updated_at)
VALUES
  ('RCP-OIL-20260821-000002', 'OIL', 'TEST_JIVO_OIL_HANADB',
   'CUSTA000567', 'AAKASH KUMAR',
   '2026-08-21', 1000.00, 0.00, 'DRAFT', 'INR',
   42, NOW(), NOW());

-- 2) one row per tender line
INSERT INTO payment_method_entry (receipt_id, method, amount)
VALUES (127, 'UPI', 1000.00);

-- 3) audit timeline
INSERT INTO payment_status_history
  (content_type_id, object_id, action, from_status, to_status,
   actor_kind, changed_by_id, changed_by_username, created_at)
VALUES
  (<ct payment_receipt>, 127, 'CREATED', '', 'DRAFT',
   'USER', 42, 'ramesh', NOW());
```

Two rules enforced here:

- **`company_db` is frozen at creation.** Deriving it later would let an `.env` edit between draft and post silently send the payment to a different SAP company (`models.py:176`).
- **`total_amount` is computed from the method rows**, never taken from the client payload.

Real row after this action:

| receipt_no | card_code | total | status | sap_doc_entry |
|---|---|---|---|---|
| `RCP-OIL-20260821-000002` | `CUSTA000567` | 1000.00 | `DRAFT` | `NULL` |

---

### Action 2 — Allocate to invoices *(optional)*

Says which SAP invoices this money settles. Skip it and the receipt is an **advance**.

```sql
INSERT INTO payment_invoice_allocation
  (receipt_id, sap_doc_entry, sap_doc_num, amount)
VALUES (127, 18402, 810023, 1000.00);

UPDATE payment_receipt
   SET allocated_amount = 1000.00, updated_at = NOW()
 WHERE id = 127;
```

A DB constraint (`payment_receipt_allocated_within_total`) guarantees `allocated_amount <= total_amount`.

**Which invoices are open** — run this to see what the picker offers:

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT T0."DocEntry", T0."DocNum", T0."DocDate", T0."CardCode",
         T0."DocTotal", T0."PaidToDate",
         T0."DocTotal" - T0."PaidToDate" AS "BalanceDue"
  FROM "TEST_JIVO_OIL_HANADB"."OINV" T0
  WHERE T0."CardCode" = ''CUSTA000567''
    AND T0."DocStatus" = ''O''
    AND T0."DocTotal" - T0."PaidToDate" > 0.01
  ORDER BY T0."DocDate"
');
```

---

### Action 3 — Submit for approval

**Code:** `services.py:317 submit_receipt()` — `@transaction.atomic`
**Still no SAP contact.**

Guards, in order:

1. Already has `sap_doc_entry` → refuse (already in SAP)
2. Open approval exists → refuse, naming who holds it
3. Status must be `DRAFT`, `REJECTED` or `PENDING_ERROR`
4. `validate_receipt()` — amounts, G/L config, party

```sql
UPDATE payment_receipt
   SET status = 'PENDING_APPROVAL', updated_at = NOW()
 WHERE id = 127;

INSERT INTO approval_request
  (content_type_id, object_id, status, company, amount,
   document_number, document_type, current_level, created_at)
VALUES
  (<ct payment_receipt>, 127, 'PENDING', 'OIL', 1000.00,
   'RCP-OIL-20260821-000002', 'PAYMENT', 1, NOW());

INSERT INTO payment_status_history
  (content_type_id, object_id, action, from_status, to_status,
   reason, actor_kind, changed_by_id, created_at)
VALUES
  (<ct payment_receipt>, 127, 'SUBMITTED', 'DRAFT', 'PENDING_APPROVAL',
   'Submitted for approval.', 'USER', 42, NOW());
```

Approver notifications fire from the approval engine's `submitted` hook **inside this same transaction** — so a notification can never exist for a submission that rolled back.

---

### Action 4 — Approve

Approval moves up levels. On the **final** approval the document becomes `APPROVED` and posting is triggered.

```sql
INSERT INTO approval_action
  (request_id, approver_id, level, decision, remarks, created_at)
VALUES (77, 51, 1, 'APPROVED', 'Verified against UPI statement', NOW());

UPDATE approval_request SET status = 'APPROVED' WHERE id = 77;

UPDATE payment_receipt
   SET status = 'APPROVED', updated_at = NOW()
 WHERE id = 127;

INSERT INTO payment_status_history
  (content_type_id, object_id, action, from_status, to_status,
   level, level_label, actor_kind, changed_by_id, created_at)
VALUES
  (<ct payment_receipt>, 127, 'APPROVED', 'PENDING_APPROVAL', 'APPROVED',
   1, 'Accounts', 'USER', 51, NOW());
```

**Rejection** instead writes `decision='REJECTED'`, sets the receipt to `REJECTED`, and the creator may correct and resubmit on the same record.

---

### Action 5 — Post to SAP ← the critical step

**Code:** `sap_poster.py:192 post_document()`

#### 5a. BEFORE the call

```sql
-- under SELECT ... FOR UPDATE, so two requests cannot both post
UPDATE payment_receipt
   SET status = 'POSTING_TO_SAP', updated_at = NOW()
 WHERE id = 127;

INSERT INTO payment_status_history (..., action, to_status, actor_kind, reason)
VALUES (..., 'SAP_POST_STARTED', 'POSTING', 'SAP',
        'Posting request sent to SAP.');

INSERT INTO payment_sap_call_log
  (content_type_id, object_id, endpoint, request_payload, status, started_at)
VALUES (<ct>, 127, 'IncomingPayments', '<redacted JSON>', 'PENDING', NOW());
```

The row lock plus `already_posted()` is the **duplicate guard**. Cheque numbers and bank references are redacted in the log (`sap_payloads.py:244`).

#### 5b. The payload

`POST /b1s/v1/IncomingPayments` against `TEST_JIVO_OIL_HANADB`:

```json
{
  "CardCode": "CUSTA000567",
  "DocDate": "2026-08-21",
  "DocCurrency": "INR",
  "BPLID": 1,
  "Series": 91,
  "TransferAccount": "1104107",
  "TransferSum": 1000.00,
  "Remarks": "OMS RCP-OIL-20260821-000002",
  "PaymentInvoices": [
    { "DocEntry": 18402, "SumApplied": 1000.00 }
  ]
}
```

**No user-defined fields are sent.** `U_OMS_REF` / `U_OMS_IDEM` were removed — they do not exist on `ORCT` and SAP rejects any payload carrying them (`sap_payloads.py:88`). The link back to OMS is the `DocEntry` SAP returns.

#### 5c. What changes IN SAP

| SAP table | Effect |
|---|---|
| `ORCT` | **New row** — the Incoming Payment header. Gives `DocEntry`, `DocNum`, `TransId` |
| `RCT2` | Invoice-settlement lines (one per allocation) |
| `OINV` | `PaidToDate` **increases**; `DocStatus` flips `O`→`C` when fully paid |
| `JDT1` | Journal: **DR** bank/cash G/L, **CR** customer receivable |
| `OCRD` | Party `Balance` **decreases** |

This is a **real financial posting**. It cannot be deleted — only cancelled in SAP, which writes a further reversing entry.

#### 5d. AFTER — success

```sql
UPDATE payment_receipt
   SET status        = 'POSTED',
       sap_doc_entry = 21947,
       sap_doc_num   = 826246657,
       sap_trans_id  = 227476,
       sap_posted_at = NOW(),
       sap_response  = 'Payment posted to SAP as document 826246657.',
       updated_at    = NOW()
 WHERE id = 127;

UPDATE payment_sap_call_log
   SET status='SUCCESS', http_status=201, duration_ms=1840, completed_at=NOW()
 WHERE id = 908;

INSERT INTO payment_status_history
  (..., action, to_status, actor_kind, sap_doc_entry, sap_doc_num, reason)
VALUES (..., 'SAP_POSTED', 'POSTED', 'SAP', 21947, 826246657,
        'Payment posted to SAP as document 826246657.');
```

Real posted row:

| receipt_no | total | status | doc_entry | doc_num | trans_id |
|---|---|---|---|---|---|
| `RCP-OIL-20260821-000002` | 1000.00 | `POSTED` | 21947 | 826246657 | 227476 |

**Why `sap_trans_id` matters:** `DocEntry` identifies the *payment*; only `TransId` reaches `JDT1`. Without it, "show me the accounting for this receipt" means hunting through SAP by hand (`models.py:203`).

#### 5e. AFTER — SAP answered "no"

```sql
UPDATE payment_receipt
   SET status = 'PENDING_ERROR',
       sap_response = 'SAP rejected the payment: ...',
       sap_raw_error = '<SAP''s literal words>',
       sap_raw_error_code = '-5002'
 WHERE id = 127;
```

Nothing was committed in SAP. The approval is **reopened at its final level** — SAP said no, so the approval never really stood.

Two error fields are kept deliberately: `sap_response` is written for the collector (plain language + what to do); `sap_raw_error` is SAP's literal string, never rewritten, so a SAP administrator can search their logs against it.

#### 5f. AFTER — SAP silent

```sql
UPDATE payment_receipt
   SET status = 'SAP_UNKNOWN',
       sap_response = '... SAP did not respond, so it is not yet known whether
                       the document was created. Do not resubmit.'
 WHERE id = 127;
```

The approval is **NOT** reopened — a second approval could post the payment twice.

A 2xx response carrying **no DocEntry** is also treated as `SAP_UNKNOWN`, not as failure: the document probably exists.

---

## 4. Bank Deposit — action by action

### Action 1 — Create deposit

Select `APPROVED`/`POSTED` receipts not yet banked.

```sql
INSERT INTO payment_bank_deposit
  (deposit_no, company, company_db, deposit_date, deposit_type,
   bank_key, bank_code, bank_gl_account, bank_display_name,
   collected_amount, deposit_amount, shortfall_reason, status,
   created_by_id, created_at, updated_at)
VALUES
  ('DEP-OIL-20260813-000006', 'OIL', 'TEST_JIVO_OIL_HANADB',
   '2026-08-13', 'CHEQUE',
   'ICICI01', 'ICIC', '2201102', 'ICICI BANK - 2201102',
   91800.00, 91800.00, '', 'DRAFT', 42, NOW(), NOW());

INSERT INTO payment_bank_deposit_line (deposit_id, receipt_id, amount)
VALUES (6, 118, 91800.00);
```

Three constraints protect the money:

| Constraint | Rule |
|---|---|
| `bank_deposit_receipt_once_uq` | **A receipt can never be in two deposits** |
| `bank_deposit_not_over_collected` | `deposit_amount <= collected_amount` |
| `bank_deposit_shortfall_requires_reason` | Short-banking demands a written reason |

Bank details are **snapshotted, not FK'd** — SAP is the master, and the deposit stays readable if the account is later removed there.

### Actions 2–3 — Submit and approve

Identical shape to the receipt: `DRAFT → PENDING_APPROVAL → APPROVED`, writing `approval_request`, `approval_action` and `payment_status_history` rows.

---

### Action 4 — Post deposit to SAP ← **read this carefully**

**Code:** `services.py:587 post_deposit_to_sap()`

**Only the CASH share of a deposit ever reaches SAP.**

`sap_postable_amount()` (`services.py:567`) sums the **method lines** of the linked receipts and counts cash only:

> A cheque already debited the bank when its **receipt** posted (DR bank / CR receivable — verified). The cheque is therefore already in SAP. The OMS deposit records the physical hand-over and nothing more. Posting it again would **debit the bank twice**.

#### Case A — cheque-only deposit → **no SAP call at all**

```sql
UPDATE payment_bank_deposit
   SET status = 'POSTED',
       sap_posted_at = NOW(),
       sap_response = 'Recorded in OMS. No SAP posting is required: the cheques
                       in this deposit were already accounted for in SAP when
                       their receipts posted.'
 WHERE id = 6;
-- sap_doc_entry / sap_doc_num / sap_trans_id stay NULL, deliberately.
```

Real example — note the NULL SAP keys:

| deposit_no | type | collected | deposited | status | doc_entry |
|---|---|---|---|---|---|
| `DEP-OIL-20260813-000006` | CHEQUE | 91800.00 | 91800.00 | `POSTED` | **NULL** |

> Fabricating identifiers for a document that does not exist would be worse than leaving the truth visible.

#### Case B — cash / mixed deposit → SAP receives the **cash share only**

```json
{
  "DocType": "A",
  "CardCode": "1105003",
  "DocDate": "2026-08-13",
  "DocCurrency": "INR",
  "BPLID": 1,
  "TransferAccount": "2201102",
  "TransferSum": 50000.00,
  "TransferDate": "2026-08-13",
  "Remarks": "OMS DEP-OIL-20260813-000006"
}
```

- `DocType: 'A'` — **account-type** payment, not a party payment
- `CardCode` — the **cash G/L being emptied** (`1105003`), not a customer
- `TransferAccount` — destination bank G/L
- `CashSum` is **not** used: money moves account-to-account, which SAP models as a transfer

SAP effect (verified against `TransId 224988`):

```
ORCT  DocEntry 21789  DocType A
      CardCode 1105003 CASH SALE     <- source G/L emptied
      TrsfrAcct 2201102  TrsfrSum 50,000
JDT1  DR 2201102 ICICI BANK   50,000
      CR 1105003 CASH SALE    50,000
```

The clearing account **provably nets to zero**: receipts debited `1105003`, the deposit credits it back.

For a mixed deposit, OMS keeps the **full physical amount** (cash + cheques) while SAP gets cash only. The two figures answer different questions and must not be reconciled into one.

---

## 5. SAP verification queries

Replace the schema for MART / BEVERAGES.

**Find a receipt's SAP payment:**

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT T0."DocEntry", T0."DocNum", T0."TransId", T0."DocDate",
         T0."CardCode", T0."CardName", T0."DocType",
         T0."DocTotal", T0."TrsfrAcct", T0."TrsfrSum", T0."Comments"
  FROM "TEST_JIVO_OIL_HANADB"."ORCT" T0
  WHERE T0."DocEntry" = 21947
');
```

**The journal entry it created** (needs `sap_trans_id`):

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT T0."TransId", T0."Line_ID", T0."Account", T0."ShortName",
         T0."Debit", T0."Credit", T0."RefDate", T0."LineMemo"
  FROM "TEST_JIVO_OIL_HANADB"."JDT1" T0
  WHERE T0."TransId" = 227476
  ORDER BY T0."Line_ID"
');
```

Debits must equal credits.

**Which invoices a payment settled:**

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT T0."DocNum" AS "PaymentDocNum", T1."DocEntry" AS "InvoiceDocEntry",
         T1."SumApplied", T2."DocNum" AS "InvoiceDocNum",
         T2."DocTotal", T2."PaidToDate", T2."DocStatus"
  FROM "TEST_JIVO_OIL_HANADB"."ORCT" T0
  INNER JOIN "TEST_JIVO_OIL_HANADB"."RCT2" T1 ON T0."DocEntry" = T1."DocNum"
  INNER JOIN "TEST_JIVO_OIL_HANADB"."OINV" T2 ON T1."DocEntry" = T2."DocEntry"
  WHERE T0."DocEntry" = 21947
');
```

**Party balance:**

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT T0."CardCode", T0."CardName", T0."Balance", T0."CreditLine"
  FROM "TEST_JIVO_OIL_HANADB"."OCRD" T0
  WHERE T0."CardCode" = ''CUSTA000567''
');
```

**Proof `ODPS` is unused** — explains why deposits post as `ORCT`:

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT COUNT(*) AS "OdpsRows" FROM "TEST_JIVO_OIL_HANADB"."ODPS"
');
-- returns 0 in all three live databases
```

---

## 6. Reconciliation — when SAP data changes

OMS does **not** poll SAP. Two mechanisms keep the sides honest.

### Resolving `SAP_UNKNOWN`

`sap_poster.py:371 reconcile_unknown()` asks SAP whether the document exists:

- **Found** → OMS adopts SAP's keys and becomes `POSTED`
- **Not found** → nothing was committed; the document becomes `PENDING_ERROR` and is safely resubmittable

### If someone cancels the payment inside SAP

SAP writes a **reversing journal entry**; the original `ORCT` row stays. OMS still shows `POSTED` with the original `DocEntry` — **OMS does not learn about it automatically.** Detect it with:

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT T0."DocEntry", T0."DocNum", T0."Canceled", T0."CANCELED"
  FROM "TEST_JIVO_OIL_HANADB"."ORCT" T0
  WHERE T0."DocEntry" = 21947
');
```

`Canceled = 'Y'` while OMS says `POSTED` is a genuine divergence needing manual correction.

**Rule of thumb:** OMS is the system of record **up to** posting; SAP is the system of record **after** it.

### Daily reconciliation query

```sql
SELECT r.receipt_no, r.card_code, r.total_amount, r.status,
       r.sap_doc_entry, r.sap_doc_num, r.sap_posted_at
  FROM payment_receipt r
 WHERE r.status IN ('SAP_UNKNOWN','POSTING_TO_SAP','PENDING_ERROR')
    OR (r.status = 'POSTED' AND r.sap_doc_entry IS NULL)
 ORDER BY r.updated_at DESC;
```

Any row here needs a human. A `POSTING_TO_SAP` older than a few minutes means the process died mid-call.

---

## 7. Reading the audit trail

`payment_status_history` is the **single timeline** — approvals, edits and SAP attempts interleave in one ordered list.

```sql
SELECT h.created_at, h.action, h.from_status, h.to_status,
       h.actor_kind, h.changed_by_username, h.level_label,
       h.sap_doc_entry, h.reason
  FROM payment_status_history h
  JOIN django_content_type ct ON ct.id = h.content_type_id
 WHERE ct.model = 'paymentreceipt'
   AND h.object_id = 127
 ORDER BY h.created_at;
```

Typical successful receipt:

| time | action | from → to | actor |
|---|---|---|---|
| 09:12 | `CREATED` | → DRAFT | USER ramesh |
| 09:15 | `SUBMITTED` | DRAFT → PENDING_APPROVAL | USER ramesh |
| 10:02 | `APPROVED` | PENDING_APPROVAL → APPROVED | USER priya |
| 10:02 | `SAP_POST_STARTED` | → POSTING | SAP |
| 10:02 | `SAP_POSTED` | → POSTED | SAP (DocEntry 21947) |

`actor_kind` distinguishes `USER`, `SAP` and `SYSTEM`. Rows are **never updated after insert**.

Every SAP HTTP call is separately in `payment_sap_call_log` with payload, response, status and duration.

---

## 8. Talking points for the walkthrough

1. **A deposit is not an ODPS Deposit.** It is an account-type Incoming Payment. `ODPS` has 0 rows.
2. **Cheques never post twice.** They hit SAP at *receipt* time; the deposit records only the physical hand-over. Only the cash share is posted.
3. **`SAP_UNKNOWN` ≠ failure.** Silence is not rejection. Blocking resubmission prevents double-paying a party.
4. **`company_db` is frozen at creation** so a config change can never redirect a payment mid-flight.
5. **A receipt can be banked exactly once** — a DB `UniqueConstraint`, not application logic.
6. **Two error fields on purpose:** one for the collector, one verbatim for the SAP administrator.
7. **`sap_trans_id` is what links to the journal.** `DocEntry` alone cannot reach `JDT1`.
8. **A SAP rejection reopens the approval** at its final level; a SAP timeout deliberately does not.

---

## 9. Code reference

| Concern | File |
|---|---|
| Models, statuses, constraints | `payments/models.py` |
| Submit / approve / validate | `payments/services.py` |
| SAP posting + reconciliation | `payments/sap_poster.py` |
| SAP payload construction | `payments/sap_payloads.py` |
| HANA read queries | `payments/hana_queries.py` |
| Permissions | `payments/permissions.py` |
| Cheque/ORCT evidence | `docs/CHEQUE_RECEIPT_SAP_EVIDENCE.md` |
| SAP verification notes | `docs/SAP_VERIFICATION.md` |
