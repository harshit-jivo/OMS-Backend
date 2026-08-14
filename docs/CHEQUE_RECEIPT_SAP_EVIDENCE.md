# Cheque Receipts → SAP: Evidence and Implementation Plan

**Status:** Pre-implementation deliverable (items 1–9 of Task 21). No code changed.
**Method:** Real `ORCT` + `JDT1` + `NNM1` records from live company databases. Nothing inferred.

---

## 0. Correction to a previous finding

My earlier audit reported **zero cheque receipts**. That was wrong. It searched `ORCT.CheckSum > 0`, which is empty because the company does not use SAP's cheque-processing path at all. Searching `Comments` instead — the way this data is actually shaped — finds them immediately:

| Company | Customer receipts with cheque wording |
|---|---|
| BEVERAGES | **66** |
| OIL | **52** |
| MART | 0 |

The conclusion that `PaymentChecks` / `RCT1` is unused stands, and is now positively confirmed rather than inferred from absence.

---

## 1. The real customer cheque receipt (evidence)

```
ORCT   DocEntry   6346
       DocNum     826248105
       DocType    C                    ← Customer
       CardCode   CUSTA001217
       DocDate    2026-08-12
       Series     2514  (IP0826)
       BPLId      2
       TransId    41589

       CashAcct   NULL      CashSum   0
       CheckAcct  NULL      CheckSum  0        ← PaymentChecks NOT used
       TrsfrAcct  1104106   TrsfrSum  24,000   ← the money field

       Comments   BEING CHEQUE RECEIVED FROM SUNNY SINGH
```

Five further examples, same structure:

| DocEntry | Card | TrsfrAcct | TrsfrSum | Series | Comments |
|---|---|---|---|---|---|
| 6267 | CUSTA001215 | 1104106 | 71,999 | 2514 | CHEQUE DEPO 00190912 CLEARING 05/08/2026 BANK OF MAHARASHTRA CHQ DEP 190912 |
| 6244 | CUSTA001174 | 1104106 | 10,800 | 2514 | CHEQUE DEPO 00000815 CLEARING 04/08/2026 240 CHQ DEP 000815 |
| 6243 | CUSTA001174 | 1104106 | 27,000 | 2514 | CHEQUE DEPO 00000635 CLEARING 04/08/2026 240 CHQ DEP 000635 |
| 6242 | CUSTA001029 | 1104106 | 30,500 | **2513** | CHEQUE DEPO 00001708 CLEARING 30/07/2026 BANK OF BARODA CHQ DEP 001708 |
| 6212 | CUSTA001174 | 1104106 | 37,800 | 2514 | BEING CHEQUE RECEIVED FROM SHUNTY |

`CheckAcct` and `CheckSum` are NULL/0 on **all 66**. Every one uses `TrsfrAcct` = `1104106`.

---

## 2. JDT1 — the actual debit/credit

```
DocEntry 6346  TransId 41589
  L0  DR  1104106  INDIAN BANK-7051847887   24,000   ShortName 1104106      BaseRef 826248105
  L1  CR  1101001  SUNDRY DEBTORS GT        24,000   ShortName CUSTA001217  BaseRef 826248105

DocEntry 6267  TransId 41262
  L0  DR  1104106  INDIAN BANK-7051847887   71,999
  L1  CR  1101001  SUNDRY DEBTORS GT        71,999

DocEntry 6242  TransId 41158
  L0  DR  1104106  INDIAN BANK-7051847887   30,500
  L1  CR  1101001  SUNDRY DEBTORS GT        30,500
```

**A cheque receipt debits the BANK directly and credits the customer receivable.**

### Consequence for Task 7 — the cash G/L is NOT used

| Tender | Debit | Deposit step later? |
|---|---|---|
| CASH | `1105001` cash-in-hand clearing | **Yes** |
| UPI / transfer | `1104106` bank | No |
| **CHEQUE** | **`1104106` bank** | **No** |

Cheque behaves like UPI, not like cash. `SapCompanyMap.cash_gl_account` must **not** be used for cheque receipts — doing so would put money into the clearing account that no deposit ever removes, leaving a permanently inflated cash-in-hand balance.

Cheque and UPI both target `1104106` (all 66 cheques; 637 of 688 transfers). They are the same accounting path, distinguished only by Remarks.

---

## 3. Target SAP payload

```json
{
  "DocType":           "rCustomer",
  "CardCode":          "CUSTA000844",
  "DocDate":           "<OMS posting date>",
  "TaxDate":           "<OMS posting date>",
  "DocCurrency":       "INR",
  "Series":            2514,
  "BPLID":             2,
  "TransferAccount":   "1104106",
  "TransferSum":       20000.0,
  "TransferDate":      "<OMS posting date>",
  "Remarks":           "CHEQUE | RCP-OIL-2026-27-000123 | CHQ 252525 | HDFC | 2026-08-06",
  "PaymentInvoices": [
    { "LineNum": 0, "DocEntry": 63408, "InvoiceType": "it_Invoice", "SumApplied": 20000.0 }
  ]
}
```

**Not sent:** `PaymentChecks`, `CheckAccount`, `CheckSum`, `BankCode`, `CashAccount`, `CashSum`.

`DocDate` / `TaxDate` / `TransferDate` are the **OMS posting date**, never `cheque_date`. The cheque date is informational and appears only in Remarks.

---

## 4. OMS → SAP field mapping

| OMS field | SAP destination | Note |
|---|---|---|
| `card_code` | `CardCode` | unchanged |
| `payment_date` | `DocDate`, `TaxDate`, `TransferDate` | **posting date, not cheque_date** |
| `PaymentMethodEntry.amount` | `TransferSum` | summed across cheque lines |
| resolved collection bank | `TransferAccount` | see §7 |
| `cheque_number` | **Remarks only** | never a SAP field |
| `bank_name` (payer's bank) | **Remarks only** | never `BankCode`, never a G/L |
| `cheque_date` | **Remarks only** | informational |
| `receipt_no` | **Remarks** (mandatory) | finance's search key |
| `remarks` (user) | appended after the OMS block | never replaces it |
| allocations | `PaymentInvoices[]` | unchanged |

All cheque columns stay in `PaymentMethodEntry`. **No migration, no new table, nothing removed.**

### Remarks format

```
CHEQUE | <receipt_no> | CHQ <cheque_number> | <bank_name> | <cheque_date>
```
with user remarks appended after a final ` | `. Truncation must clip the **user** portion, never the OMS identifier — capped at SAP's 254 characters.

---

## 5. Confirmation that PaymentChecks is not required

Positively evidenced, not assumed:
- `CheckAcct` NULL and `CheckSum` 0 on **all 66** cheque receipts.
- `RCT1` (the cheque-lines table) has **0 rows** in all three companies.
- The previous `PaymentChecks` attempt failed with `-2028 No matching records found` — consistent with a company that has never configured that path.

---

## 6. Series resolution (Task 8)

Verified in `NNM1`, `ObjectCode = '24'`:

| Series | SeriesName | Indicator | BPLId |
|---|---|---|---|
| 2513 | IP0726 | JUL-26-27 | **NULL** |
| **2514** | **IP0826** | **AUG-26-27** | **NULL** |
| 2516 | IP1026 | OCT-26-27 | NULL |

Three facts that matter:

1. **One series per month**, named `IP<MM><YY>` — selected by the posting date's month.
2. **`BPLId` is NULL** — the series is *not* per-branch. Do not filter by branch.
3. **Numbers are company-specific**: BEVERAGES Aug = 2514, TEST_OIL Aug = **2564**. Must be resolved per company DB.

⚠️ **Correction to the brief:** it gave "July → 2563, August → 2564" as the example. In BEVERAGES, **2563/2564 are ObjectCode 202 (Production Orders)**. The correct Incoming Payment series are 2513/2514. In TEST_OIL, 2564 *is* the August payment series — the numbers collide across databases, which is exactly why they must never be hardcoded.

**Resolution rule:** query `NNM1` where `ObjectCode='24'`, `Indicator` = the posting date's month-year label, `Locked='N'`; use its `Series`. A generic resolver already exists at `hana/services/connection.py:402` (parameterised `object_code`); `payments` currently sends **no** `Series` at all.

---

## 7. Collection bank G/L resolution

Not the cash G/L (§2). The destination is a company bank account, resolved through the existing machinery:

`PaymentMethodMapping` (company + `payment_method='CHEQUE'`) → `bank_key` → `bank_master.find_bank()` → DSC1/ODSC → `gl_account`.

This is the same path UPI already uses, and it keeps SAP as the source of truth for bank accounts. `bank_master.PaymentAccountResolver.resolve('CHEQUE')` already returns exactly this — the resolver needs no change; only the payload builder does.

If no CHEQUE mapping exists for a company, posting must fail loudly with a configuration error rather than silently falling back to cash.

---

## 8. Files to change

| File | Change |
|---|---|
| `payments/sap_payloads.py` | `build_incoming_payment` (`:27`): route CHEQUE into the transfer branch; delete `PaymentChecks` construction (`:84-97`, `:113-114`); add the Remarks composer; accept and emit `Series`. |
| `payments/services.py` | `post_receipt_to_sap` (`:415`): resolve and pass `Series`. Cheque G/L already resolved by `_bank_accounts_for` (`:381`). |
| `payments/sap_client.py` | No change. |
| `payments/hana_queries.py` | Add the ObjectCode-24 series lookup (or reuse `connection.py:402`). |
| `payments/models.py` | `sap_trans_id` on `PaymentReceipt` + `BankDeposit` (additive). **No cheque field changes.** |
| `payments/sap_poster.py` | Capture `TransId` in `_sap_keys` (`:78`). |
| Frontend / mobile | **No change.** Cheque UI stays as-is; no SAP internals exposed. |

`CASH` and `UPI` payload construction is untouched.

---

## 9. Tests to add

**Payload (no SAP contact)**
- CHEQUE payload contains **no** `PaymentChecks`, `CheckAccount`, `CheckSum`, `BankCode`.
- Emits `TransferAccount` = the mapped cheque bank G/L, `TransferSum` = total, `TransferDate` = posting date.
- **`cheque_date` never appears in any date field** — only in Remarks.
- Remarks contains receipt no, cheque no, payer bank and cheque date, in that order.
- User remarks are appended, never substituted; over-length input clips the user text, not the OMS identifier.
- `Series` matches the posting month; a July date and an August date resolve differently.
- `PaymentInvoices` unchanged.
- **Regression: CASH and UPI payloads byte-identical to today.**

**Series resolver** — month→series mapping, company-specific values, and a hard failure when no unlocked series exists.

**Post-implementation SAP verification** (sandbox `TEST_OIL_15122025`, then cancel): confirm `ORCT` DocType/CardCode/Series/TrsfrAcct/TrsfrSum/Remarks/TransId; `JDT1` shows DR bank / CR receivable; `OINV.PaidToDate` increased and `BalanceDue` decreased, `DocStatus='C'` when fully paid.

---

## 10. Open items before coding

1. **Cheque bank mapping missing.** Only `UPI` and `CHEQUE` rows exist for OIL (`ICICI:1104106`, `ICICI:2201102`); BEVERAGES and MART have **no `SapCompanyMap` row at all**, so neither series nor bank can resolve there.
2. **Which bank should OMS cheques target?** All 66 real cheques went to `1104106` (INDIAN BANK in BEVERAGES). OIL's configured cheque mapping points at `ICICI:1104106` — same G/L number, different bank name per database. Finance should confirm the intended collection bank per company.
3. **Cheque deposits.** A cheque receipt already debits the bank, so — like UPI — it should **not** be depositable. The existing `BankDeposit` CheckKey guard becomes dead code. Confirm before removing.
