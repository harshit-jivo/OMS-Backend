# Live SAP vs OMS — Payments & Deposits Accounting Audit

**Type:** read-only verification. No code, payloads, migrations or data were changed.
**Source of truth:** live SAP HANA (`JIVO_OIL_HANADB`, `JIVO_MART_HANADB`, `JIVO_BEVERAGES_HANADB`) and the live OMS Postgres database.
**Rule applied:** every accounting claim below is backed by actual SAP rows. Nothing is inferred from textbook accounting.

---

## 1. Executive summary

| # | Finding | Severity |
|---|---|---|
| **F1** | **Cash G/L is wrong for MART.** OMS maps all three companies to `1105001`. MART's live cash receipts use `1105003` (1,067 receipts in 12 months, current to 2026-08-01). | ❌ **Blocker** |
| **F2** | **MART and BEVERAGES have no payment-method mappings at all.** Only OIL has UPI/CHEQUE rows. A UPI or cheque receipt in those companies resolves to an empty G/L. | ❌ **Blocker** |
| **F3** | **`reconcile_unknown()` cannot resolve a real timeout.** It only acts when `sap_doc_entry` is already set — which never happens when SAP never answered. Every genuine `SAP_UNKNOWN` needs manual intervention. | ❌ **High** |
| **F4** | **OMS advance = SAP on-account, and this matches.** SAP holds the unapplied amount in `ORCT.OpenBal`; OMS omits `PaymentInvoices`. Correct. | ✅ |
| **F5** | **No OMS advance-settlement path exists.** SAP settles later advances via internal reconciliation (`OITR`/`ITR1`, 30,004 / 122,839 rows). OMS has no API for this; it is done by Finance in SAP. | ⚠️ Gap, by design |
| **F6** | One receipt = one method is **enforced** and matches SAP: **0 of 11,422** OIL payments mix methods. | ✅ |
| **F7** | Cheque posts as a **transfer**, not `PaymentChecks`. **`CheckSum` is 0 on every OIL payment** — the company never uses SAP's cheque object. | ✅ |
| **F8** | Deposits are account-type Incoming Payments, **not `ODPS`**. `ODPS` = **0 rows in all three companies**. | ✅ |
| **F9** | 496 cancelled ORCT rows exist in OIL. OMS never learns of a SAP-side cancellation. | ⚠️ Medium |

**Verdict: not production-ready.** F1 and F2 will mispost or fail outright for MART and BEVERAGES.

---

## 2. Configuration as it stands (live OMS DB)

```
company    company_db                     cash_gl   deposit_source_gl   bpl
BEVERAGES  TEST_JIVO_BEVERAGES_HANADB     1105001   1105001             1
MART       TEST_JIVO_MART_HANADB          1105001   1105001             1
OIL        TEST_JIVO_OIL_HANADB           1105001   1105001             1

payment_method_mapping:
  OIL  CHEQUE  ICICI:1104106   active
  OIL  UPI     ICICI:2201102   active
  (MART — none)   (BEVERAGES — none)
```

Note all three point at **TEST** databases while the evidence below is from **live**.

---

## 3. Payment-method accounting — real SAP evidence

### 3.1 CASH — `ORCT.CashAcct` / `CashSum`

Last 12 months, `DocType='C'`, non-cancelled, amount > 1:

| Company | G/L actually used | Receipts | Latest | OMS configured | Match |
|---|---|---|---|---|---|
| OIL | **`1105001`** CASH SALE | 31 | 2026-07-14 | `1105001` | ✅ |
| MART | **`1105003`** CASH SALE MAYAPURI | **1,067** | 2026-08-01 | `1105001` | ❌ **MISMATCH** |
| BEVERAGES | **`1105001`** CASH SALE | 851 | 2026-08-21 | `1105001` | ✅ |

All-time totals confirm the split: OIL `1105001` ₹4,443,570 / `1105003` ₹3,854,314 (historic); MART **only** `1105003` (₹14,241,307); BEVERAGES `1105001` ₹46,880,279.

> **`1105003` in OIL is largely ₹0.0001 rounding lines** (`TransType 24`, 861 rows) — not cash collections. OIL's real cash is `1105001`. MART is the opposite: `1105003` is its genuine cash account.

**Journal** — DocEntry 21283, TransId (OIL), ₹50,000, user TARAN:
```
DR 1105001 CASH SALE      50,000
CR <customer receivable>  50,000
```

### 3.2 UPI / BANK TRANSFER / NEFT / RTGS — `ORCT.TrsfrAcct` / `TrsfrSum`

SAP does **not** distinguish these — all use the same transfer fields. The G/L identifies the receiving bank.

OIL, all-time, amount > 1:

| G/L | Account | Payments | Total |
|---|---|---|---|
| `2201101` | INDIAN BANK CC 7007270527 | 5,701 | ₹5,277,256,748 |
| `3200003` | — | 2,211 | ₹528,463,454 |
| `1104201` | PAYTM BANK | 1,218 | ₹8,229,651 |
| `2201105` | HSBC 166794941001 | 938 | ₹2,544,299,614 |
| `1104202` | RAZORPAY BANK | 111 | ₹1,059,085 |
| `2201102` | ICICI 629305042195 | 81 | ₹381,234,751 |
| `1104106` | ICICI 629305042322 | 12 | ₹28,324,933 |

MART: `1104201` (3,766), `1104108` (3,452), `1104112` (305), `1104202` (158).
BEVERAGES: `1104106` (2,188), `1104107` (75).

**OMS maps OIL UPI → `2201102`.** That account is used, but is only the 6th most common. Confirm with Finance that OMS collections belong there rather than `2201101`.

**Journal:** DR bank G/L / CR customer receivable.

### 3.3 CHEQUE — posts as a TRANSFER

```sql
SELECT SUM(CASE WHEN "CheckSum">1 THEN 1 ELSE 0 END) FROM "JIVO_OIL_HANADB"."ORCT"
WHERE "DocType"='C' AND "Canceled"='N'
-- result: 0
```

**Zero cheque-object payments in 11,422 documents.** The company records cheques as transfers, identified by `Comments`:

| DocEntry | CardCode | TrsfrAcct | TrsfrSum | Comments |
|---|---|---|---|---|
| 21993 | CUSTA000877 | `2201101` | 260,000 | `CHEQUE DEPO 00215873 CLEARING 20/08/2026 CANARA BANK…` |
| 20004 | CUSTA000877 | `2201101` | 168,000 | `CHEQUE DEPO 00620800 CLEARING 06/05/2026 CANARA BANK…` |
| 19912 | CUSTA000877 | `2201101` | 170,000 | `CHEQUE DEPO 00620799 CLEARING 02/05/2026…` |

✅ **OMS matches this design** — `TRANSFER_METHODS` includes CHEQUE, and cheque detail goes into `Remarks`. **But real cheques use `2201101`, while OMS maps CHEQUE → `1104106`.** Confirm with Finance.

**Summary table (all values from live data):**

| METHOD | ORCT FIELD | REAL ACCOUNT (OIL) | JDT1 DEBIT | JDT1 CREDIT |
|---|---|---|---|---|
| CASH | `CashAcct` / `CashSum` | `1105001` | `1105001` | customer |
| UPI / NEFT / RTGS / transfer | `TrsfrAcct` / `TrsfrSum` | `2201101`, `1104201`, … | bank G/L | customer |
| CHEQUE | `TrsfrAcct` / `TrsfrSum` | `2201101` | bank G/L | customer |
| *(cheque object)* | `CheckAcct` / `CheckSum` | **never used — 0 rows** | — | — |

---

## 4. Advance payments — how SAP really does it

### 4.1 Structure

There is **no special advance liability account.** An advance is an ordinary Incoming Payment with **no `RCT2` line**, and SAP carries the unapplied amount in **`ORCT.OpenBal`**.

Live on-account examples (OIL):

| DocEntry | DocNum | CardCode | Date | TrsfrAcct | TrsfrSum | **OpenBal** | TransId |
|---|---|---|---|---|---|---|---|
| 22014 | 826246706 | CUSTA001143 | 2026-08-22 | `2201101` | 100 | **100** | 228417 |
| 22013 | 826246705 | CUSTA000636 | 2026-08-21 | `2201101` | 637,902 | **637,902** | 228404 |
| 22006 | 826246700 | CUSTA000025 | 2026-08-19 | `1104201` | 1,000 | **1,000** | 228370 |

### 4.2 Journal — TransId 228417 (DocEntry 22014)

```
Line 0   DR 2201101  INDIAN BANK        100      "Incoming Payments - CUSTA001143"
Line 1   CR 1101001  CUSTA001143        100      "Incoming Payments - CUSTA001143"
```

**Answers to the audit questions:**

1. **How is the advance accounted?** Exactly like any receipt — DR bank, CR customer control.
2. **G/L debited:** the bank/cash account for the tender.
3. **Credited:** `1101001`, the customer receivable control account.
4. **Special advance account?** **No.**
5. **Customer balance:** decreases (or goes credit) — the customer is now in credit.
6. **Field marking advance?** `OpenBal > 0` marks it unapplied. UDFs `U_Type_of_Advance` and `U_Adv_Settl_Dt` exist but are **sparsely used**: OIL 25 "One Time Settlement" + 1 "Adjustable in EMI" out of 13,608; MART has free-text noise (`3107`, `TRF`, `advance`); BEVERAGES 18.
7. **Linked to an invoice?** No — no `RCT2` row.
8. **Where is the money held?** In the customer's receivable balance as a credit.
9. **Later settlement:** see below.

### 4.3 Settlement

Two distinct mechanisms:

**(a) At posting time** — `RCT2` links payment → invoice. DocEntry 21993:

| DocNum | DocEntry (invoice) | InvType | SumApplied |
|---|---|---|---|
| 21993 | 76037 | 13 | 259,998.999 |
| 21993 | 76208 | 13 | 1.001 |

**(b) After the fact** — SAP **internal reconciliation**: `OITR` (30,004 rows) / `ITR1` (122,839 rows). This is how an existing advance is applied to a later invoice. It creates **no new ORCT** and is performed by Finance inside SAP.

> **Do not build a settlement API in OMS.** SAP settles advances by internal reconciliation, a function OMS does not and should not replicate.

### 4.4 OMS advance behaviour vs SAP

| Check | OMS | SAP reality | Match |
|---|---|---|---|
| Allocation prohibited when advance | `sap_payloads.py:181` — `if allocations and not receipt.is_advance` | on-account = no `RCT2` | ✅ |
| `PaymentInvoices` omitted | Yes — entirely, never an empty array | correct; empty array is a common rejection cause | ✅ |
| `allocated_amount` stays 0 | Yes | mirrors `OpenBal = full amount` | ✅ |
| Can still post to SAP | Yes | yes | ✅ |
| Special SAP advance fields sent | **No** | UDFs exist but are barely used | ⚠️ acceptable |
| OMS settlement API | **None** | done via `OITR`/`ITR1` in SAP | ⚠️ by design |

**OMS advance handling is correct.** 5 advance receipts currently exist.

---

## 5. Deposit accounting

### 5.1 `ODPS` is unused — confirmed

```sql
SELECT COUNT(*) FROM "JIVO_OIL_HANADB"."ODPS";        -- 0
SELECT COUNT(*) FROM "JIVO_MART_HANADB"."ODPS";       -- 0
SELECT COUNT(*) FROM "JIVO_BEVERAGES_HANADB"."ODPS";  -- 0
```

✅ The account-type Incoming Payment design is correct.

### 5.2 The model transaction — DocEntry 21789 / TransId 224988

```
ORCT  DocEntry 21789  DocType A  CardCode 1105001 CASH SALE
      TrsfrAcct 1104107   TrsfrSum 186,250
JDT1  Line 0  DR 1104107   186,250
      Line 1  CR 1105001   186,250
```

✅ Matches the OMS payload shape exactly (`DocType 'A'`, `CardCode` = source G/L, `TransferAccount` = destination bank).

### 5.3 `DocType 'A'` CardCode usage, last 12 months

| Company | Top CardCodes |
|---|---|
| OIL | `2201101` (390), `1104201` (186), `2201105` (79), **`1105001` (26)**, `1113023` (25) |
| MART | `1104201` (313), **`1105003` (230)**, `1104202` (219), **`1105001` (83)**, `2191001` (14) |
| BEVERAGES | **`1105001` (120)**, `1110109` (42), `1104106` (37), `2191001` (4) |

`DocType 'A'` covers all account-to-account transfers, only some of which are cash deposits. Cash-deposit rows use the same G/L as that company's cash receipts — **`1105001` for OIL/BEVERAGES, `1105003` for MART** — reinforcing F1.

### 5.4 Cheque physical deposit — no second SAP document

A cheque debits the bank when its **receipt** posts (§3.3). No further SAP document exists for the physical hand-in.

✅ OMS matches: `sap_postable_amount()` counts `SAP_POSTABLE_DEPOSIT_METHODS = ('CASH',)`. A cheque-only deposit makes **no SAP call** and keeps `sap_doc_entry` NULL. Live proof: `DEP-OIL-20260813-000006`, CHEQUE, ₹91,800, `POSTED`, `sap_doc_entry = NULL`.

**Cheque physical deposit = OMS-only operational record.** ✅ Correct.

### 5.5 Mixed and multi-receipt deposits

- **Mixed (cash + cheque):** SAP receives the **cash share only**; OMS retains the full physical amount. ✅ Correct — re-posting cheques would double-debit.
- **Multiple receipts:** one OMS deposit → **one** SAP document for the summed cash. Since `CardCode` is a G/L (not a customer), receipts from three different customers merge cleanly into one transfer. ✅ Consistent with SAP's model.

---

## 6. Invoice allocation

`ORCT` → `RCT2` → `OINV` verified (§4.3). `RCT2.SumApplied` increments `OINV.PaidToDate`; `DocStatus` flips `O`→`C` when fully paid.

OMS `payment_invoice_allocation` (`sap_doc_entry`, `amount_applied`, `invoice_type`) maps 1:1 to `RCT2` (`DocEntry`, `SumApplied`, `InvType 13`). ✅ Correct.

---

## 7. Multi-method receipts

```sql
SELECT SUM(CASE WHEN "CashSum">1 AND "TrsfrSum">1 THEN 1 ELSE 0 END) AS cash_and_transfer,
       SUM(CASE WHEN "CashSum">1 AND "CheckSum">1 THEN 1 ELSE 0 END) AS cash_and_check,
       SUM(CASE WHEN "TrsfrSum">1 AND "CheckSum">1 THEN 1 ELSE 0 END) AS transfer_and_check,
       COUNT(*) AS total
FROM "JIVO_OIL_HANADB"."ORCT" WHERE "DocType"='C' AND "Canceled"='N';
-- 0, 0, 0, 11422
```

**Zero mixed-method payments in 11,422 documents.**

✅ OMS enforces this at `serializers.py:425` — distinct methods > 1 raises a validation error. The comment cites the incident this prevents: DocEntry 20802, ₹1,200,000 of cheque money posted to the UPI bank G/L. **One receipt = one method = one SAP payment is enforced and correct.**

---

## 8. Cancelled documents

OIL `ORCT`: **13,634 non-cancelled, 496 cancelled.**

SAP cancellation writes a reversing journal; the original row remains with `Canceled='Y'`. **OMS never learns of this** — a receipt stays `POSTED` with its original `DocEntry`. OMS does not cancel SAP documents itself.

⚠️ **Risk:** silent divergence. Detection query:

```sql
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT "DocEntry","DocNum","Canceled","CancelDate"
  FROM "JIVO_OIL_HANADB"."ORCT" WHERE "Canceled" = ''Y''
');
```
Compare against `payment_receipt.sap_doc_entry WHERE status = 'POSTED'`.

---

## 9. `SAP_UNKNOWN` safety — defect found

`sap_poster.py:371 reconcile_unknown()`:

```python
if document.sap_doc_entry:
    found = fetch_document(document.sap_doc_entry, ...)
    ...
# No DocEntry to check against. SAP holds no OMS reference field, so this
# cannot be resolved automatically — a human must look.
return False
```

❌ **The reconciliation path cannot fire for a real timeout.** `sap_doc_entry` is only set *after* a successful response, so a document that reached `SAP_UNKNOWN` because SAP never answered has **no DocEntry** — and the function returns `False` immediately.

The audit brief expects "SAP document found → adopt identifiers → POSTED; not found → PENDING_ERROR". **Neither branch executes.** The code is honest about it (it logs and asks for a human), but the automatic safety net does not exist.

**Root cause:** no OMS reference is written to SAP. `sap_payloads.py:88` records that `U_OMS_REF`/`U_OMS_IDEM` were removed because they do not exist on `ORCT` and SAP rejects payloads carrying them.

**Recommended fix (needs SAP-side work):** have the SAP team add a UDF to `ORCT` for the OMS receipt number, then search by it during reconciliation. Alternatively, search by `CardCode + DocDate + amount` — a heuristic, not a guarantee.

Current OMS state: 11 receipts and 6 deposits in `PENDING_ERROR`; no rows currently stuck in `SAP_UNKNOWN`.

---

## 10. Reconciliation matrix

| Flow | OMS behaviour | SAP behaviour | Match | Risk | Change needed |
|---|---|---|---|---|---|
| CASH receipt | `CashAccount` = configured cash G/L | OIL/BEV `1105001`, **MART `1105003`** | ❌ MART | **High** | Per-company cash G/L |
| UPI receipt | `TransferAccount` from method mapping | `TrsfrAcct` = bank G/L | ⚠️ | Med | Confirm `2201102` vs `2201101`; add MART/BEV mappings |
| CHEQUE receipt | `TransferAccount`, detail in Remarks | `TrsfrAcct`; `CheckSum` never used | ⚠️ | Med | Confirm `1104106` vs `2201101`; add MART/BEV |
| BANK TRANSFER / NEFT / RTGS | not distinct methods | not distinct in SAP either | ✅ | — | none |
| ADVANCE receipt | omits `PaymentInvoices` | no `RCT2`, `OpenBal` = amount | ✅ | — | none |
| ADVANCE settlement | no OMS API | `OITR`/`ITR1` internal reconciliation | ⚠️ | Low | none — Finance does it in SAP |
| CASH deposit | `DocType A`, CardCode = source G/L | verified DocEntry 21789 | ✅* | High | *same MART G/L issue |
| CHEQUE deposit | no SAP call, `sap_doc_entry` NULL | no second SAP document exists | ✅ | — | none |
| CASH + CHEQUE deposit | cash share only | cheque already posted at receipt | ✅ | — | none |
| Multiple receipts / deposit | 1 deposit → 1 SAP doc | CardCode is a G/L, so merging is valid | ✅ | — | none |
| Rejected SAP payment | `PENDING_ERROR`, approval reopened | nothing committed | ✅ | — | none |
| `SAP_UNKNOWN` | blocks resubmit; auto-reconcile **cannot run** | doc may or may not exist | ❌ | **High** | OMS ref UDF on ORCT |
| Cancelled SAP document | not detected | reversing journal, `Canceled='Y'` | ❌ | Med | periodic detection query |

---

## 11. Answers to the 16 questions

1. **Is OMS advance handling correct?** ✅ Yes — matches SAP on-account exactly.
2. **How is a SAP advance represented?** Ordinary Incoming Payment, no `RCT2`, unapplied amount in `ORCT.OpenBal`. No special account.
3. **How is it settled later?** SAP internal reconciliation (`OITR`/`ITR1`) — or `RCT2` if allocated at posting. No new ORCT.
4. **CASH G/L?** OIL `1105001`, BEVERAGES `1105001`, **MART `1105003`**.
5. **UPI G/L?** Bank G/L via `TrsfrAcct` — OIL commonly `2201101`/`1104201`; OMS maps `2201102`.
6. **CHEQUE G/L?** Also `TrsfrAcct` — real cheques use `2201101`; OMS maps `1104106`.
7. **NEFT/RTGS/Transfer?** Same transfer fields; SAP does not distinguish them.
8. **One receipt = one method enforced?** ✅ Yes (`serializers.py:425`); SAP shows 0 mixed in 11,422.
9. **3 receipts in one deposit → how many SAP docs?** **One**, for the summed cash.
10. **3 different customers?** Still one — `CardCode` is a G/L, not a customer.
11. **CASH + CHEQUE deposit?** SAP gets **cash only**; OMS keeps the full physical amount.
12. **Second SAP posting for a cheque at deposit?** **No.** Accounted at receipt time.
13. **SAP tables on receipt posting?** `ORCT`, `RCT2` (if allocated), `OINV.PaidToDate`/`DocStatus`, `JDT1`, `OCRD.Balance`.
14. **SAP tables on deposit posting?** `ORCT` (`DocType A`), `JDT1`. **Not `ODPS`.**
15. **Are OMS payloads identical to the real process?** Structurally ✅; **account values ❌ for MART and unmapped for MART/BEVERAGES.**
16. **What must be corrected before production?** F1, F2, F3 — see below.

---

## 12. Required changes

### ❌ Blockers

**C1 — per-company cash G/L.** `payment_sap_company_map.cash_gl_account` is already per-company; only the *values* are wrong. Set MART to `1105003` (both `cash_gl_account` and `deposit_source_gl_account`). **Config change only — no code.**

**C2 — payment-method mappings for MART and BEVERAGES.** Neither has UPI or CHEQUE rows. Add them, or block those tenders in those companies. Suggested from live usage: MART UPI → `1104201` (3,766 payments); BEVERAGES UPI/CHEQUE → `1104106` (2,188). **Confirm with Finance.**

**C3 — confirm OIL UPI and CHEQUE accounts.** OMS maps UPI `2201102` and CHEQUE `1104106`; real cheques predominantly use `2201101`. Confirm with Finance which account OMS collections belong to.

### ⚠️ High

**C4 — make `SAP_UNKNOWN` recoverable.** Ask the SAP team to add an OMS-reference UDF to `ORCT`, then search by it in `reconcile_unknown()`. Until then, every timeout needs a human.

**C5 — cancellation detection.** Add a periodic job comparing `POSTED` OMS documents against `ORCT.Canceled='Y'`.

### ✅ Already correct — do not change

Advance handling · one-method-per-receipt · cheque-as-transfer · cheque-deposit-without-SAP · cash-only deposit posting · `DocType 'A'` deposit shape · invoice allocation · `PENDING_ERROR` + approval reopen · duplicate-post guard.

---

## 13. Evidence index

| Claim | Evidence |
|---|---|
| OIL cash = `1105001` | DocEntry 21283 (₹50,000), 20743, 20205 — user TARAN |
| MART cash = `1105003` | 1,067 receipts in 12 months, latest 2026-08-01 |
| BEVERAGES cash = `1105001` | 851 receipts, latest 2026-08-21 |
| Advance = `OpenBal` | DocEntry 22014 / TransId 228417; 22013, 22006 |
| Advance journal | TransId 228417: DR `2201101` 100 / CR `1101001` 100 |
| Settlement via RCT2 | DocEntry 21993 → invoices 76037, 76208 |
| Internal reconciliation | `OITR` 30,004 rows, `ITR1` 122,839 rows |
| Deposit model | DocEntry 21789 / TransId 224988: DR `1104107` / CR `1105001` 186,250 |
| `ODPS` unused | 0 rows × 3 companies |
| No mixed methods | 0 / 11,422 (OIL) |
| Cheque never uses CheckSum | 0 rows with `CheckSum > 1` |
| Cheque = transfer | DocEntry 21993, 20004, 19912 — `Comments` "CHEQUE DEPO…" |
| Cheque deposit is OMS-only | `DEP-OIL-20260813-000006`, POSTED, `sap_doc_entry` NULL |
| Cancellations exist | 496 cancelled ORCT rows (OIL) |

### Reusable queries

```sql
-- cash G/L per company
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT "CashAcct", COUNT(*) AS "n", MAX("DocDate") AS "latest"
  FROM "JIVO_MART_HANADB"."ORCT"
  WHERE "DocType"=''C'' AND "Canceled"=''N'' AND "CashSum" > 1
    AND "DocDate" >= ADD_MONTHS(CURRENT_DATE, -12)
  GROUP BY "CashAcct" ORDER BY COUNT(*) DESC ');

-- unapplied advances
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT "DocEntry","DocNum","CardCode","DocDate","TrsfrSum","OpenBal","TransId"
  FROM "JIVO_OIL_HANADB"."ORCT"
  WHERE "DocType"=''C'' AND "Canceled"=''N'' AND "OpenBal" > 0
  ORDER BY "DocEntry" DESC ');

-- journal for one document
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT "TransId","Line_ID","Account","ShortName","Debit","Credit","LineMemo"
  FROM "JIVO_OIL_HANADB"."JDT1" WHERE "TransId" = 224988 ORDER BY "Line_ID" ');

-- mixed-method check
SELECT * FROM OPENQUERY(HANADB112, '
  SELECT SUM(CASE WHEN "CashSum">1 AND "TrsfrSum">1 THEN 1 ELSE 0 END) AS "mixed",
         COUNT(*) AS "total"
  FROM "JIVO_OIL_HANADB"."ORCT" WHERE "DocType"=''C'' AND "Canceled"=''N'' ');
```

---

## 14. Note on scope

This audit reads live SAP and OMS only. **No writes were performed** — no test transactions, no migrations, no code or payload edits.

One limitation: OMS is configured against **TEST** databases while this evidence is from **live**. G/L codes were verified to exist in both, but usage patterns come from live. Re-verify C1–C3 against whichever databases production will actually target.
