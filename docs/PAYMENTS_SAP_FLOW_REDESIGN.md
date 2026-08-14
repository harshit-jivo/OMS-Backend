# Payments → SAP Flow Redesign

**Status:** Design document. No code changed.
**Method:** Every accounting claim below is quoted from live SAP `ORCT` + `JDT1` rows. Nothing is inferred from business assumptions.
**Databases inspected:** `JIVO_BEVERAGES_HANADB`, `JIVO_OIL_HANADB`, `JIVO_MART_HANADB`, `TEST_OIL_15122025`

---

## 1. Executive summary

OMS posts Bank Deposits to SAP's `/Deposits` endpoint (ODPS). **The company has never used that document type** — `ODPS` has 0 rows in all three live company databases. Deposits are posted as a second Incoming Payment with `DocType='A'`.

Consequently **no OMS deposit has ever posted**: all 3 sit in `PENDING_ERROR` with no `sap_doc_entry`. Receipts are unaffected (15 `POSTED`) because they already use the correct endpoint.

Three findings materially change the design beyond "swap the endpoint":

| # | Finding | Consequence |
|---|---|---|
| 1 | **Transfer/UPI receipts debit the bank directly**, not a clearing account | A UPI receipt must never enter a deposit — the bank would be debited twice. Only CASH is depositable. |
| 2 | **Zero cheque receipts** and **zero RCT1 lines** in all 3 companies | The entire cheque path — receipt and deposit — is unproven. Cannot be designed from evidence. |
| 3 | **The cash G/L differs per company** (BEVERAGES `1105001`, OIL/MART `1105003`) | Per-company configuration is required; a global constant would post to the wrong ledger. |

---

## 2. Verified SAP accounting

### 2.1 Customer receipt — CASH

```
ORCT  DocEntry 6322  DocNum 826248088  DocType C  TransId 41483
      CardCode CUSTA000072 (KULDEEP SINGH)
      CashAcct 1105001   CashSum 10,000   TrsfrAcct NULL

JDT1  DR 1105001 CASH SALE           10,000
      CR 1101001 SUNDRY DEBTORS GT   10,000
```
Confirmed on DocEntry 6344 (42,500) and 6333 (153,000 → `1101015 SUNDRY DEBTORS STAFF`). The credit account follows the customer's control account; the debit is always the cash G/L.

### 2.2 Customer receipt — TRANSFER / UPI  ⚠️ differs from cash

```
ORCT  DocEntry 6351  DocType C  TransId 41601  CardCode CUSTA001205
      CashAcct NULL   TrsfrAcct 1104106   TrsfrSum 10,000

JDT1  DR 1104106 INDIAN BANK-7051847887  10,000
      CR 1101001 SUNDRY DEBTORS GT       10,000
```
Confirmed on DocEntry 6350 and 6349.

**The money is already in the bank.** There is no clearing account and therefore no deposit step. This is the single most important consequence in this document.

### 2.3 Bank deposit — ACCOUNT-type Incoming Payment

```
ORCT  DocEntry 6310  DocNum 826248077  DocType A  TransId 41464
      CardCode  1105001   ← a G/L, not a customer (the source being emptied)
      TrsfrAcct 1104107   ← destination bank
      TrsfrSum  247,500
      CashAcct  NULL

JDT1  DR 1104107 ICICI BANK-629305042545  247,500
      CR 1105001 CASH SALE                247,500
```

Verified identical on the five most recent transfer-type account receipts:

| DocEntry | TransId | CardCode | TrsfrAcct | Amount | JDT1 |
|---|---|---|---|---|---|
| 6345 | 41573 | 1105001 | 1104107 | 185,500 | DR bank / CR cash |
| 6334 | 41533 | 1105001 | 1104107 | 368,250 | DR bank / CR cash |
| 6310 | 41464 | 1105001 | 1104107 | 247,500 | DR bank / CR cash |
| 6264 | 41251 | 1105001 | 1104107 | 398,500 | DR bank / CR cash |
| 6248 | 41177 | 1105001 | 1104107 | 50,000 | DR bank / CR cash |

Comments on all five read "BY CASH – RAJOURI GARDEN": cash collected at a location, banked as one deposit.

### 2.4 ⚠️ Not every account-type receipt is a bank deposit

24 account-type receipts use `CashSum` rather than `TrsfrSum`. These are **not** deposits:

```
ORCT  DocEntry 4357  TransId 32140  CardCode 1105001  CashAcct 1105001  CashSum 480,600
JDT1  DR 1105001 CASH SALE  480,600
      CR 1105001 CASH SALE  480,600      ← nets to zero
```
Comments: *"being cash received from param veerji"*. These are manual cash movements between G/Ls, made by finance in the GUI. **OMS must not attempt to reproduce them.** The deposit OMS generates is always the transfer-type form in §2.3.

### 2.5 The clearing-account bridge

```
   CUSTOMER (cash)                                    BANK
        │                                              ▲
        │ receipt DocType=C                            │ deposit DocType=A
        │ DR 1105001 / CR 1101001                      │ DR 1104107 / CR 1105001
        ▼                                              │
   ┌──────────────────────────────────────────────────┴──┐
   │  1105001 CASH SALE — cash collected, not yet banked  │
   └──────────────────────────────────────────────────────┘

   CUSTOMER (UPI/transfer) ──── DR 1104106 bank ────▶ BANK   (no clearing, no deposit)
```

---

## 3. Volumes

| Company | Cheque receipts | RCT1 lines | ODPS rows |
|---|---|---|---|
| BEVERAGES | **0** | **0** | **0** |
| OIL | **0** | **0** | **0** |
| MART | **0** | **0** | **0** |

BEVERAGES, last 120 days: 722 transfer receipts · 433 cash receipts · **0 cheque**.
Account-type: 410 transfer (deposits) · 24 cash (G/L movements).

Cash G/L actually used, last 180 days:

| Company | Primary | Also seen |
|---|---|---|
| BEVERAGES | `1105001` (502) | `1105003` (31) |
| OIL | `1105003` (95) | `1105001` (14) |
| MART | `1105003` (477) | `1105001` (55) |
| TEST_OIL | `1105003` (84) | `1105001` (11) |

`SapCompanyMap.cash_gl_account` for OIL is already `1105003` — correct. BEVERAGES and MART have no row yet.

---

## 4. Current OMS implementation

### 4.1 Receipt flow (working)
`POST /api/payments/receipts/<pk>/submit/` → `views.PaymentReceiptSubmitView` → `services.submit_receipt` (`:317`) → approval ladder → on final approval `hooks._on_receipt_approved` (`:15`) → `transaction.on_commit` → `services.post_receipt_to_sap` (`:415`) → `sap_payloads.build_incoming_payment` (`:27`) → `sap_poster.post_document` (`:185`) → `sap_client.post_incoming_payment` (`:212`) → `POST /IncomingPayments`.

Method → SAP field mapping (`sap_payloads.py:68-114`):

| OMS method | SAP fields | G/L source | Matches SAP? |
|---|---|---|---|
| CASH | `CashAccount`, `CashSum` | `SapCompanyMap.cash_gl_account` | ✅ §2.1 |
| UPI | `TransferAccount`, `TransferSum`, `TransferDate`, `TransferReference` | `PaymentMethodMapping.bank_key` → DSC1 | ✅ §2.2 |
| CHEQUE | `PaymentChecks[]` + `CheckAccount`; `BankCode` = payer's bank | mapped cheque account | ⚠️ **unverifiable — no SAP precedent** |

`PaymentMethodEntry.Method` has exactly three values: CASH, UPI, CHEQUE. `BANK_TRANSFER`, `NEFT` and `RTGS` **do not exist** in the code — a stale docstring at `sap_payloads.py:73` still names them.

### 4.2 Deposit flow (never succeeded)
Same shape, but `sap_payloads.build_deposit` (`:134-190`) emits an ODPS body — `DepositType`, `DepositAccount`, `AllocationAccount`, `Commission`, `JournalRemarks`, `CheckLines` — and `sap_client.post_deposit` (`:219`) POSTs to `/Deposits`. Every one of those fields is ODPS-only and must go.

### 4.3 Storage
`sap_doc_entry`, `sap_doc_num`, `sap_posted_at`, `sap_response`, `sap_raw_error`, `sap_raw_error_code` exist on both models. **`sap_trans_id` does not exist anywhere** — verified by grep across the whole backend. TransId is the only key that reaches JDT1.

---

## 5. Target design

### 5.1 Deposit payload

```
POST /IncomingPayments                    (was POST /Deposits)
{
  "DocType":           "A",                        ← Account
  "CardCode":          "<cash_gl_account>",        ← source, from SapCompanyMap
  "DocDate":           deposit.deposit_date,
  "TaxDate":           deposit.deposit_date,
  "DocCurrency":       "INR",
  "TransferAccount":   deposit.bank_gl_account,    ← destination bank
  "TransferSum":       deposit.deposit_amount,
  "TransferDate":      deposit.deposit_date,
  "TransferReference": deposit.slip_number,        ← optional
  "Remarks":           "OMS DEP-…",
  "BPLID":             <branch>
}
```

`TransferAccount`/`TransferSum` are the Service Layer property names; `TrsfrAcct`/`TrsfrSum` are the ORCT columns. `build_incoming_payment` already uses the SL names — reuse that vocabulary.

Fields to delete: `DepositType`, `DepositAccount`, `AllocationAccount`, `Commission`, `JournalRemarks`, `CheckLines`, `TotalLC`, `Reference`.

**`bank_charge` has no home in this payload.** ODPS had `Commission`; an Incoming Payment does not. Either drop it from the deposit form or post it as a separate journal entry — **decision required** (§8).

### 5.2 Source G/L: reuse `SapCompanyMap.cash_gl_account`

Do not add configuration. `bank_master.PaymentAccountResolver.cash_gl()` (`bank_master.py:155-160`) already reads this field for **receipts** and never touches DSC1. Using the same field for deposits guarantees the deposit credits exactly what the receipts debited, so the clearing account nets to zero.

Rejected alternatives: letting the user pick invites emptying an account the receipts never filled — a silent imbalance found months later at reconciliation. Deriving it from linked receipts breaks when a deposit mixes receipts posted to different accounts.

### 5.3 Destination bank: unchanged

`BankMasterService` / DSC1 + ODSC already resolve `bank_key`, `bank_code`, `bank_display_name`, `bank_gl_account` from the user's bank selection. No G/L is ever typed. Nothing changes here.

### 5.4 ⚠️ Only CASH receipts may be deposited

Per §2.2, a UPI receipt already debited the bank. Including one in a deposit would debit the bank a second time and credit a clearing account that was never debited — inventing money.

Current code does **not** enforce this. `validate_deposit` (`services.py:444`) checks amounts and G/L presence, not tender type. **This validation must be added**, and it is a correctness fix independent of the endpoint change.

### 5.5 Add `sap_trans_id`

One nullable integer on `PaymentReceipt` and `BankDeposit`, captured in `_sap_keys()` (`sap_poster.py:78`) beside DocEntry/DocNum. It is already present in the response body OMS receives and discards. Without it, "show me the journal entry" is a manual SAP hunt. Additive; no backfill.

---

## 6. Files to change

| File | Change | Risk |
|---|---|---|
| `payments/sap_payloads.py` | Rewrite `build_deposit()` (`:134-190`) to emit the DocType='A' body. Delete all ODPS-only keys. Fix the stale NEFT/RTGS docstring at `:73`. | Medium |
| `payments/services.py` | `post_deposit_to_sap` (`:525`): pass the resolved cash G/L. `validate_deposit` (`:444`): reject non-CASH receipts. Revisit the CheckKey guard (`:537-548`). | Medium |
| `payments/sap_poster.py` | `_entity_for()` (`:73`) → `IncomingPayments` for deposits; collapse the isinstance branch (`:213-216`); capture `TransId` in `_sap_keys()` (`:78`); drop `DeposId`/`DeposNum`. | Low |
| `payments/sap_client.py` | `post_deposit()` (`:219`) → `/IncomingPayments`. Keep the function name so callers are untouched. | Low |
| `payments/models.py` | Add `sap_trans_id` to both models. One additive migration. | Low |
| `payments/serializers.py`, `views.py` | Expose `sap_trans_id` on detail responses. | Low |
| `payments/tests_*.py` | New deposit payload tests; receipt tests must pass **unchanged**. | Low |

**No API endpoint is renamed or removed.** `/api/payments/deposits/*` keeps its contract — only what it sends to SAP changes.

### Frontend / mobile
Additive only: show `sap_trans_id` beside DocEntry/DocNum on deposit detail. If §5.4 lands, the deposit receipt-picker should list only CASH receipts and explain why UPI is excluded. No breaking change.

### Database
`ALTER TABLE … ADD COLUMN sap_trans_id integer NULL` on `payment_receipt` and `bank_deposit`. **Nothing dropped.** `payment_sap_call_log` and `payment_status_history` continue unchanged.

---

## 7. Duplicate protection

Keep as-is — it is already correct. `already_posted()` (`sap_poster.py:168`) is checked under `select_for_update()` inside `transaction.atomic`, and status flips to `POSTING_TO_SAP` before the HTTP call (`:196-205`), so a concurrent request cannot start a second post.

**One gap to close:** `reconcile_unknown()` (`sap_poster.py:344`) has **no caller anywhere** — no scheduler, no command, no view. Worse, a timeout sets `SAP_UNKNOWN` without a DocEntry, so even if called it hits its `return False` branch, while `sap_response` tells the user "It will be verified automatically; do not resubmit." Recommend a management command listing `SAP_UNKNOWN` documents for finance — an hour's work, and honest about needing a human.

---

## 8. Unknowns and blockers

| # | Blocker | Evidence | Needed |
|---|---|---|---|
| **B1** | **Cheque flow entirely unproven** | 0 cheque receipts, 0 RCT1 lines, 0 ODPS across all 3 companies | Does finance accept cheques at all? If yes, one real posted example. **Until then: change nothing in the cheque path** — neither receipt nor deposit. |
| **B2** | **BEVERAGES and MART have no `SapCompanyMap` row** | Only OIL is configured | `company_db`, `cash_gl_account` (BEVERAGES `1105001`, MART `1105003`), `default_bpl_id`. Deposits cannot post there without it — and the screenshots are BEVERAGES. |
| **B3** | **`bank_charge` has nowhere to go** | ODPS `Commission` has no Incoming Payment equivalent | Drop from the form, or post a separate JE? |
| **B4** | **Branch** | Deposits use `default_bpl_id` only (`services.py:550`); real rows show BPLId 1 and 2 | Should a deposit inherit its receipts' branch? |
| **B5** | **3 stuck deposits** | All `PENDING_ERROR`, never posted | Resubmit after the fix, or has finance already posted them by hand? |

B1 is the true blocker: it decides whether the CheckKey guard and `PaymentChecks` logic are deleted, kept, or rewritten. B2 blocks go-live for the company that raised this.

---

## 9. Migration plan

Deposits have never posted, so there is **no deposit regression risk** — only the receipt path must be proven untouched.

| Step | Work | Reversible |
|---|---|---|
| 1 | Add `sap_trans_id` (additive migration) + capture in `_sap_keys` | Yes — nullable column |
| 2 | Configure `SapCompanyMap` for BEVERAGES and MART (**B2**) | Yes — data only |
| 3 | Rewrite `build_deposit()`; repoint `post_deposit()` | Yes — revert commit |
| 4 | Add the CASH-only deposit validation (§5.4) | Yes |
| 5 | Sandbox post + JDT1 verification against `TEST_OIL_15122025` | Cancel the test document |
| 6 | Resubmit the 3 stuck deposits (**B5**) | Cancel in SAP if wrong |
| 7 | Expose `sap_trans_id` in API + UI | Yes |

Cheque work is **not** in this plan — it is blocked on B1.

---

## 10. Test plan

**Unit**
- Deposit payload emits `DocType='A'`, `CardCode` = company cash G/L, `TransferAccount` = `bank_gl_account`, `TransferSum` = `deposit_amount`.
- Payload contains **no** ODPS keys (`DepositType`, `DepositAccount`, `AllocationAccount`, `Commission`, `JournalRemarks`, `CheckLines`, `TotalLC`).
- `validate_deposit` rejects a deposit containing a UPI receipt.
- `_sap_keys` extracts TransId.

**Regression** — the receipt payload tests must pass **byte-identical**. The receipt path is not being changed, and that is the claim to defend.

**Sandbox** (`TEST_OIL_15122025`) — post one deposit, then verify in SAP:
```sql
SELECT "DocEntry","DocType","CardCode","TrsfrAcct","TrsfrSum","TransId"
FROM "TEST_OIL_15122025"."ORCT" WHERE "DocEntry" = ?;

SELECT "Account","Debit","Credit"
FROM "TEST_OIL_15122025"."JDT1" WHERE "TransId" = ? ORDER BY "Line_ID";
```
Expected: `DocType='A'`, `CardCode`=cash G/L, `TrsfrAcct`=bank, and JDT1 showing **DR bank / CR cash G/L** — the shape proved in §2.3. Cancel the document afterwards.

**Reconciliation** — after one real cash receipt and its deposit, confirm the clearing account nets to zero.

**Suites** — `manage.py check`; full `orders` and `payments` suites.

---

## 11. What this fixes

Deposits post for the first time. The clearing account reconciles, because receipts and deposits finally touch the same G/L. UPI receipts stop being eligible for deposit, closing a double-count that would have inflated the bank. And `sap_trans_id` makes every OMS document traceable into SAP's journal — which is what finance actually asks for when a number looks wrong.