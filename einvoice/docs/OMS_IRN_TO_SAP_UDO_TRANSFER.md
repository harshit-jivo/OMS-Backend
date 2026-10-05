 # Transferring OMS-generated IRNs into SAP's `@UTL_MDEXTH`

Runbook + record of the 2026-09-18 transfer. Companion to
`INVOICE_PRINTING_AND_QR_RUNBOOK.md`.

Read section 6 before repeating this for another company. One step in it is the
difference between a clean transfer and 22 invoices that cannot print.

---

## 1. Why this exists

OMS writes its IRN/QR results to `OMS_IRN_LOG`. SAP's own Crystal layouts read
**only** `@UTL_MDEXTH`, the add-on's UDO table:

| report proc | reads |
|---|---|
| `OMS_SP_GST_INVOICE` / `_SAP` | `@UTL_MDEXTH` **UNION** `OMS_IRN_LOG` |
| `CRYSTAL_AR_INVOICE_ITEMS` | `@UTL_MDEXTH` only |
| `CRYSTAL_AR_INVOICE_SERVICES` | `@UTL_MDEXTH` only |
| the other `CRYSTAL_*` procs | `@UTL_MDEXTH` only |

So an invoice whose IRN came from OMS printed fine through the OMS bill-print
route and printed **blank IRN/QR** from anything SAP-side. Copying the rows into
`@UTL_MDEXTH` fixes every report at once; the alternative was adding the
`OMS_IRN_LOG` UNION to each `CRYSTAL_*` proc.

---

## 2. Privileges — check first, they differ per company

`DSRN` holds **schema-level** privileges, so they cover `@UTL_MDEXTH`:

| schema | SELECT | INSERT | UPDATE | DELETE |
|---|---|---|---|---|
| `JIVO_OIL_HANADB` | yes | yes | **yes** | **no** |
| `JIVO_BEVERAGES_HANADB` | yes | yes | **no** | **no** |

Neither has `DELETE`. **Beverages has no `UPDATE` either**, which means the
repair in section 7 is not available there — a bad row cannot be corrected or
removed by `DSRN` at all. Get `UPDATE` granted before running Beverages again,
or have someone with rights on standby.

<<<<<<< HEAD
=======
> **2026-09-19 — the running application does not connect as `DSRN`.** It connects
> as **`B1i`**, whose grants are similar but not identical, and which also covers
> Mart (absent from the table above):
>
> | schema | SELECT | INSERT | UPDATE | DELETE |
> |---|:--:|:--:|:--:|:--:|
> | `JIVO_OIL_HANADB` | yes | yes | **yes** | no |
> | `JIVO_BEVERAGES_HANADB` | yes | yes | **no** | no |
> | `JIVO_MART_HANADB` | yes | yes | **yes** | no |
>
> So the section-7 repair is available in Oil **and Mart**, but still not Beverages,
> where an `UPDATE` fails with HANA **error 258 `insufficient privilege`**. Check
> whichever principal you are actually using — the query in this section hardcodes
> `DSRN`; use `USER_NAME = CURRENT_USER` to check the caller instead.

>>>>>>> 8d7af8811f9346fd059666d31a14d64ef9c5a9ca
Check with:

```sql
SELECT PRIVILEGE FROM SYS.EFFECTIVE_PRIVILEGES
 WHERE USER_NAME = 'DSRN' AND SCHEMA_NAME = '<schema>' AND IS_VALID = 'TRUE'
   AND PRIVILEGE IN ('SELECT','INSERT','UPDATE','DELETE');
```

---

## 3. The schemas are identical

`OMS_IRN_LOG` is a clone of `@UTL_MDEXTH` — **37 columns, same names, order,
types, lengths, scale and nullability, zero differences**, in both Oil and
Beverages. There is no column mapping to write. Verify before each run:

```sql
SELECT 'only in @UTL_MDEXTH' SIDE, COLUMN_NAME FROM SYS.TABLE_COLUMNS
 WHERE SCHEMA_NAME='<schema>' AND TABLE_NAME='@UTL_MDEXTH'
   AND COLUMN_NAME NOT IN (SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS
                            WHERE SCHEMA_NAME='<schema>' AND TABLE_NAME='OMS_IRN_LOG')
UNION ALL
SELECT 'only in OMS_IRN_LOG', COLUMN_NAME FROM SYS.TABLE_COLUMNS
 WHERE SCHEMA_NAME='<schema>' AND TABLE_NAME='OMS_IRN_LOG'
   AND COLUMN_NAME NOT IN (SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS
                            WHERE SCHEMA_NAME='<schema>' AND TABLE_NAME='@UTL_MDEXTH');
```

The *data*, however, needs five transformations.

---

## 4. The five transformations

### 4.1 `DocEntry` — every OMS key collides

`@UTL_MDEXTH` is a registered SAP UDO (`OUDO.Code = 'UTL_MDEXT_UOM'`, name
*Document Extension(IRN)*, log table `AUTL_MDEXTH`). Its primary key is
`DocEntry` alone, and the sequence is **dense — no gaps**. `OMS_IRN_LOG` starts
its own sequence at 1, so *every* row collides.

Assign new keys from the end of the table, and compute the base **inside the
statement**:

```sql
(SELECT MAX("DocEntry") FROM "<schema>"."@UTL_MDEXTH")
  + ROW_NUMBER() OVER (ORDER BY O."DocEntry")
```

> Do **not** hard-code the base. The table is written continuously — during this
> very analysis `MAX(DocEntry)` moved from 18944 to 18945 between two queries.

### 4.2 `DocNum` — set it to the new `DocEntry`

`DocNum = DocEntry` in **every** add-on row (18,944/18,944 in Oil). `OMS_IRN_LOG`
instead stores the **invoice** number there. Copying it straight across would
make these the only rows in the table that break the invariant.

Nothing is lost: the invoice is referenced by `U_UTL_BaseEntry` (the invoice
`DocEntry`) plus `U_UTL_DocType` (13 = A/R Invoice), which is what the report
procs join on.

### 4.3 QR path — rebuild it, don't copy it

The filename is always `<IRN>.png` (verified 97/97 Oil, 105/107 Beverages), so
the path is derivable:

```sql
'\\10.10.101.52\SAP Attachments\Jivo Oil\Bitmaps\'       || O."U_UTL_IRN" || '.png'   -- OIL
'\\10.10.101.52\SAP Attachments\Jivo Beverages\Bitmaps\' || O."U_UTL_IRN" || '.png'   -- BEVERAGES
```

**The folder differs per company.** OMS's stored paths were spread across up to
four variants (an old IP `20.20.45.25`, a `JIVO-APP` hostname form, and a
separate `OMS_Attachments\…\Bitmap` share), none of which the add-on uses.
Rebuilding normalises them all.

The PNGs must already be in the target folder — copying the rows does not copy
the images.

### 4.4 Metadata

| column | OMS has | set to |
|---|---|---|
| `Object` | `OMS_IRN` | `UTL_MDEXT_UOM` |
| `Period` | NULL | `OINV."FinncPriod"` |
| `Series` | NULL | `-1` |
| `Instance` | NULL | `0` |
| `Handwrtten` | NULL | `N` |
| `Transfered` | NULL | `N` |
| `DataSource` | NULL | `O` |
| `RequestStatus` | NULL | `W` |
| `NaturalPer` | NULL | `N` |
| `DPPStatus` | NULL | `N` |
| `Canceled` | `N` | `N` (unchanged) |
| `Status` | `O` | `O` (unchanged) |
| `LogInst` | NULL | NULL (unchanged) |
| `UserSign` | NULL | `1` |
| `Creator` | `OMS` | **`OMS`** |

`Object` matters — a row carrying `OMS_IRN` is not a valid instance of the UDO.

`Period` is verified derivable: the add-on's `Period` equals `OINV."FinncPriod"`
exactly on every row checked, in both companies.

`Creator = 'OMS'` deliberately breaks the add-on's convention (there `Creator` is
always a SAP `USER_CODE` such as `USER13`). **Keep it.** With no `DELETE`
privilege it is the only reliable way to find these rows again. `UserSign = 1`
is SAP user `manager` in both companies.

### 4.5 Scope — `'S'` only

Only `U_UTL_IST = 'S'` rows carry an IRN. The `'F'` rows are failed attempts with
no IRN and no QR (19 of 116 in Oil, 38 of 145 in Beverages) and must be excluded.

---

## 5. The statement

```sql
INSERT INTO "<schema>"."@UTL_MDEXTH"
  ("DocEntry","DocNum","Period","Instance","Series","Handwrtten","Canceled","Object",
   "LogInst","UserSign","Transfered","Status","CreateDate","CreateTime","UpdateDate",
   "UpdateTime","DataSource","RequestStatus","Creator","Remark","NaturalPer","DPPStatus",
   "EncryptIV","U_UTL_QRPT","U_UTL_QRST","U_UTL_IRN","U_UTL_IST","U_UTL_RMK","U_UTL_CANDT",
   "U_UTL_IRNGENDT","U_UTL_DocType","U_UTL_BaseEntry","U_UTL_StatutoryType","U_UTL_GenrTime",
   "U_UTL_CancelTime","U_UTL_AckNo","U_UTL_ST_GSTSt")
SELECT
  (SELECT MAX("DocEntry") FROM "<schema>"."@UTL_MDEXTH") + ROW_NUMBER() OVER (ORDER BY O."DocEntry"),
  (SELECT MAX("DocEntry") FROM "<schema>"."@UTL_MDEXTH") + ROW_NUMBER() OVER (ORDER BY O."DocEntry"),
  I."FinncPriod", 0, -1, 'N', 'N', 'UTL_MDEXT_UOM',
  NULL, 1, 'N', 'O', O."CreateDate", O."CreateTime", O."UpdateDate",
  O."UpdateTime", 'O', 'W', 'OMS', O."Remark", 'N', 'N',
  O."EncryptIV",
  '<company QR folder>' || O."U_UTL_IRN" || '.png',
  O."U_UTL_QRST", O."U_UTL_IRN", O."U_UTL_IST", O."U_UTL_RMK", O."U_UTL_CANDT",
  O."U_UTL_IRNGENDT", O."U_UTL_DocType", O."U_UTL_BaseEntry", O."U_UTL_StatutoryType",
  O."U_UTL_GenrTime", O."U_UTL_CancelTime", O."U_UTL_AckNo", O."U_UTL_ST_GSTSt"
FROM "<schema>"."OMS_IRN_LOG" O
JOIN "<schema>"."OINV" I ON I."DocEntry" = TO_INTEGER(O."U_UTL_BaseEntry")
WHERE O."U_UTL_IST" = 'S'
  AND NOT EXISTS (SELECT 1 FROM "<schema>"."@UTL_MDEXTH" U      -- SECTION 6. DO NOT OMIT.
                   WHERE U."U_UTL_BaseEntry" = O."U_UTL_BaseEntry"
                     AND U."U_UTL_DocType"  = O."U_UTL_DocType"
                     AND U."U_UTL_IST" = 'S');
```

Run it with autocommit **off**, compare the affected row count against the
pre-computed expected count, and roll back if they differ.

---

## 6. The step that went wrong — read this one

**Some invoices already have an add-on IRN row.** Transferring those produces two
`'S'` rows for one invoice, and the SAP report procs read the QR with *scalar*
subqueries keyed on the invoice:

```sql
(Select Distinct AA."U_UTL_IRNGENDT" from "@UTL_MDEXTH" AA
  where AA."U_UTL_BaseEntry" = OINV."DocEntry" and AA."U_UTL_IST" = 'S' ...)
```

Two rows → two values → **SQL error 305, "single-row query returns more than one
row"**, and the bill does not render at all. `DISTINCT` does not save it: even
where the IRN and QR path are identical, `U_UTL_IRNGENDT` is OMS's write time
versus the add-on's, so the values differ.

`OMS_SP_GST_INVOICE` survives this because it de-duplicates with
`ROW_NUMBER() … WHERE RN = 1`. The `CRYSTAL_*` procs do not.

### The check must be on `U_UTL_BaseEntry`

```sql
SELECT COUNT(*) FROM "<schema>"."OMS_IRN_LOG" O
 WHERE O."U_UTL_IST" = 'S'
   AND EXISTS (SELECT 1 FROM "<schema>"."@UTL_MDEXTH" U
                WHERE U."U_UTL_BaseEntry" = O."U_UTL_BaseEntry"
                  AND U."U_UTL_DocType"  = O."U_UTL_DocType"
                  AND U."U_UTL_IST" = 'S');
```

> **What actually happened.** On Oil this check was done on `DocNum` instead and
> reported "0 overlap". That comparison is meaningless — `OMS_IRN_LOG."DocNum"`
> holds the *invoice* number while `@UTL_MDEXTH."DocNum"` holds *its own key*
> (section 4.2), so it can only ever return zero. The real answer was **22**, and
> 22 invoices lost the ability to print until they were repaired.
>
> On Beverages the correct check was run up front: 6 duplicates, excluded before
> the insert, zero breakage.

Include `U_UTL_DocType` in the match. Beverages' add-on table holds DocType
**14** (A/R Credit Memo, 401 rows) as well as 13; without it a credit memo could
mask an invoice.

---

## 7. Repair, if duplicates were already inserted

Requires `UPDATE` — **available on Oil, not on Beverages.**

```sql
UPDATE "JIVO_OIL_HANADB"."@UTL_MDEXTH" AS M
   SET "U_UTL_IST" = 'F',
       "U_UTL_RMK" = 'Redundant OMS transfer - invoice already had an add-on IRN row'
 WHERE M."Creator" = 'OMS'
   AND EXISTS (SELECT 1 FROM "JIVO_OIL_HANADB"."@UTL_MDEXTH" O
                WHERE O."U_UTL_BaseEntry" = M."U_UTL_BaseEntry"
                  AND O."U_UTL_DocType"  = M."U_UTL_DocType"
                  AND O."Creator" <> 'OMS'
                  AND O."U_UTL_IST" = 'S');
```

Every report proc filters `U_UTL_IST = 'S'`, so this removes the rows from the
scalar subqueries without deleting anything, restoring the invoices to their
prior behaviour. The `EXISTS` clause makes it safe: it can only touch rows where a
non-OMS `'S'` row already exists.

Run from the JSAP SQL Server box over the linked server (`EXEC … AT`, not
`OPENQUERY` — `OPENQUERY` is only reliable for SELECTs):

```sql
EXEC (N'
UPDATE "JIVO_OIL_HANADB"."@UTL_MDEXTH" AS M
   SET "U_UTL_IST" = ''F'', "U_UTL_RMK" = ''Redundant OMS transfer''
 WHERE M."Creator" = ''OMS''
   AND EXISTS (SELECT 1 FROM "JIVO_OIL_HANADB"."@UTL_MDEXTH" O
                WHERE O."U_UTL_BaseEntry" = M."U_UTL_BaseEntry"
                  AND O."U_UTL_DocType"  = M."U_UTL_DocType"
                  AND O."Creator" <> ''OMS'' AND O."U_UTL_IST" = ''S'')
') AT HANADB112;
```

`EXEC … AT` needs **RPC OUT** enabled on the linked server. A *"Could not execute
statement on remote server 'HANADB112'"* is the known linked-server flakiness —
retry rather than assuming the SQL is wrong.

---

## 8. Verification

```sql
-- count and key range
SELECT COUNT(*), MIN("DocEntry"), MAX("DocEntry"), COUNT(DISTINCT "U_UTL_BaseEntry")
  FROM "<schema>"."@UTL_MDEXTH" WHERE "Creator" = 'OMS';

-- no duplicate 'S' introduced BY US
SELECT COUNT(*) FROM "<schema>"."@UTL_MDEXTH" M
 WHERE M."Creator" = 'OMS' AND M."U_UTL_IST" = 'S'
   AND EXISTS (SELECT 1 FROM "<schema>"."@UTL_MDEXTH" O
                WHERE O."U_UTL_BaseEntry" = M."U_UTL_BaseEntry"
                  AND O."U_UTL_DocType" = M."U_UTL_DocType"
                  AND O."Creator" <> 'OMS' AND O."U_UTL_IST" = 'S');   -- must be 0

-- invariants
SELECT COUNT(*) FROM "<schema>"."@UTL_MDEXTH" WHERE "DocNum" <> "DocEntry";   -- must be 0
SELECT MAX("DocEntry") - COUNT(*) FROM "<schema>"."@UTL_MDEXTH";              -- gaps, must be 0

-- printing actually works
CALL "<schema>"."CRYSTAL_AR_INVOICE_ITEMS"(<a transferred U_UTL_BaseEntry>);
CALL "<schema>"."CRYSTAL_AR_INVOICE_SERVICES"(<a transferred U_UTL_BaseEntry>);
CALL "<schema>"."OMS_SP_GST_INVOICE"(<a transferred U_UTL_BaseEntry>);
```

A **service** invoice legitimately returns 0 rows from `CRYSTAL_AR_INVOICE_ITEMS`
and rows from `CRYSTAL_AR_INVOICE_SERVICES`. That is not a failure.

---

## 9. What was done, 2026-09-18

### Oil — `JIVO_OIL_HANADB`

```
OMS_IRN_LOG            116 rows  (97 'S', 19 'F')
inserted                97 rows  DocEntry 18946 .. 19042
  of which duplicates   22       -> later set U_UTL_IST='F' (section 7)
effective 'S' rows      75
```

22 invoices could not print between the insert and the repair, including
726090103 and 726090104. After the repair all three report procs return rows for
every affected invoice, and `REMAINING_OMS_DUPLICATES = 0`.

### Beverages — `JIVO_BEVERAGES_HANADB`

```
OMS_IRN_LOG            145 rows  (107 'S', 38 'F')
duplicates excluded      6       (invoices 626097979, 626098041-44, 626098126)
inserted               101 rows  DocEntry 5223 .. 5323
duplicates created       0
```

Verified: metadata uniform across all 101, `DocNum = DocEntry` holds for all
5,323 rows, 101/101 QR paths in the Beverages folder, `Period` matches
`OINV."FinncPriod"`, printing confirmed on three sampled invoices.

### Mart

**Not done.** `JIVO_MART_HANADB` has not been examined.

---

## 10. Open items

1. **`ONNM.AutoKey` drift.** SAP allocates UDO keys from `ONNM`, and 17 of 19
   populated UDOs on this system sit at exactly `MAX(DocEntry) + 1`. This one
   does not:

   | company | `AutoKey` | `MAX(DocEntry)` | behind by |
   |---|---|---|---|
   | Oil | 18836 | 19042 | 206 |
   | Beverages | 4567 | 5323 | 756 |

   This **predates the transfer** (Oil was already 108 behind) and is why the
   add-on evidently derives its key from the table rather than from `ONNM` — if it
   used `AutoKey` it would already be colliding on every QR it writes. Nothing is
   broken today, but anything creating this UDO through the **SAP UI or DI API**
   would fail repeatedly. Setting `AutoKey = MAX("DocEntry") + 1` fixes a
   pre-existing bug as well as the drift this added.

2. **Pre-existing duplicate `'S'` rows in the add-on's own data** — 185 invoice
   groups in Oil, 60 in Beverages (dating from Oct 2024). The add-on IRN-ing the
   same invoice twice. They do not currently break the reports, presumably
   because the duplicate rows agree in the fields the scalar subqueries read, but
   it is the same latent fault as section 6.

3. **Beverages DocEntry 5259** (invoice 626098050, BaseEntry 17697) had **no QR
   path in `OMS_IRN_LOG`**. The path was rebuilt from its IRN; confirm the PNG
   exists in the Beverages Bitmaps folder, otherwise that one bill prints the IRN
   and Ack number with a broken QR image.

4. **The real fix is upstream.** This transfer is a backfill. Either OMS should
   write to `@UTL_MDEXTH` directly going forward, or the `CRYSTAL_*` procs should
   gain the `OMS_IRN_LOG` UNION that `OMS_SP_GST_INVOICE` already has. Otherwise
   this has to be repeated every time OMS generates IRNs.
<<<<<<< HEAD
=======

   > **2026-09-19 — built, and enabled in `.env`, but not yet live.** OMS now writes
   > `@UTL_MDEXTH` directly, gated on `EINV_MIRROR_UDO`; see
   > `IRN_DUAL_WRITE.md`. The flag was set on 2026-09-19 and the accumulated gaps
   > closed (Oil 6 rows, Beverages 5), **but the service had not been restarted**, so
   > the flag was still unread and new IRNs were continuing to arrive without a UDO
   > row — Oil BaseEntry 80518 appeared minutes after `reconcile_udo_mirror` reported
   > "up to date". Until that restart happens this backfill still has to be repeated.
   > Confirm with `python manage.py reconcile_udo_mirror` (exit 0 = nothing missing).
>>>>>>> 8d7af8811f9346fd059666d31a14d64ef9c5a9ca
