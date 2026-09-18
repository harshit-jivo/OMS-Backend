# Production orders: how JSAP and SAP B1 actually work together

Investigated 15 Sep 2026. Everything below was read directly from the live systems
(`jsaplive3` on 138.252.101.118, and the three HANA company schemas on
138.252.101.222). Read-only throughout — nothing was written.

---

## 1. The short version

SAP B1 owns production orders. JSAP bolts an approval step on top of them, and SAP
enforces that approval through a hand-written rule in
`SBO_SP_TRANSACTIONNOTIFICATION`.

Three things are wrong with it right now:

| | |
|---|---|
| **The feed is dead** | Nothing has entered the approval queue since 13 Aug 2026. 432 production orders have been raised since. |
| **The gate exempts the person who raises 92% of orders** | The SAP rule ends `AND A."UserSign" != 33`. User 33 is Gautam Chanana, who created 2,888 of the 3,152 eligible orders. |
| **18% of approval records carry the wrong item code** | A T-SQL variable-scope bug. 483 wrong rows have been pushed into SAP. |

Each is independent of the others.

---

## 2. SAP B1 — the real production data

Standard tables: `OWOR` (header) → `WOR1` (components), with `OITT`/`ITT1` for BOMs.

| | orders | BOMs | component lines |
|---|---|---|---|
| **OIL** (`JIVO_OIL_HANADB`) | 8,604 | 633 | 55,734 |
| **BEVERAGES** (`JIVO_BEVERAGES_HANADB`) | 1,516 | 544 | 7,985 |
| **MART** (`JIVO_MART_HANADB`) | 27 | — | — |

**OIL** — 8,385 Closed, 163 Released, 24 Planned, 32 Cancelled. Types: Standard 6,926,
Special 956, Disassembly 716. Warehouses: BH-PF 6,647, BH-LO 938, BH-GJ 334, BH-GR 271.
Top items are bulk blends (`RM0000002` Canola cold-press loose, 442 orders) then
Mustard Kachi Ghani 1L (433). Created overwhelmingly by Gautam Chanana (6,544).

**BEVERAGES** — water dominates (PET 250ml Natural Mineral, 199 orders / 16.3M pcs).
Almost all Atul Sharma (1,415).

**MART** — 27 orders, all combo/gift sets. Kitting, not manufacturing.

SAP's own approval machinery (`OWTM` templates, `OWDD` requests) covers production
orders **nowhere** — `OWDD` for ObjType 202 is empty in all three companies. Approval
is entirely JSAP's.

Also present in OIL: a reporting view `PRODUCTION_RELEASE_OIL` (Released, batch-managed,
Series 389/393 items) — unrelated to the approval path.

---

## 3. JSAP — the approval layer

Schema `PRDO` in `jsaplive3`. Seven tables:

| table | rows | role |
|---|---|---|
| `PlannedProductionOrders` | 2,868 | Copy of SAP's planned orders. **The authoritative snapshot.** |
| `jsDocEntry` | 2,680 | One approval flow per order. Status A/P/R. |
| `jsDocEntryDetail` | 2,680 | itemCode / warehouse / company per flow. **18% wrong — see §6.** |
| `jsProductionOrderStatusWorkflow` | 2,688 | Per-decision audit log. |
| `jsProcessLog`, `DebugLog`, `TempResults` | 0 | Never used. |

### Templates

| id | name | company | active | trigger query | approver |
|---|---|---|---|---|---|
| 229 | Production order oil | OIL | ✓ | `ItemCode NOT LIKE 'PM%'` | **Preshit** (user 101) |
| 435 | prdo pm oil | OIL | ✓ | `ItemCode LIKE 'PM%'` | **Shahrukh** (user 136) |
| 230 | Production Order bev | BEV | ✗ | `ItemCode NOT LIKE 'PM%'` | Preshit |
| 436 | prdo pm bev | BEV | ✗ | — | — |

All single-stage. Finished goods → Preshit, packaging material → Shahrukh. Beverages was
configured and switched off; `PlannedProductionOrders` holds zero Beverages rows.

Lifetime totals: 2,677 approved, 3 rejected. Volume ran 250–350 orders/month.

### Coverage

It never covered all of production. In the eight months it was demonstrably healthy
(Dec 2025 – Jul 2026), SAP OIL posted 3,184 orders and JSAP saw 2,372. The 812 it missed
were Special (439), Disassembly (263) and raw-material blending at BH-LO — by design,
since intake filters on `Status = 'P'` and the templates only match FG/PM item codes.

---

## 4. The mechanism, end to end

```
SAP OWOR (Status = 'P', Planned)
   |
   |  [1] EXTERNAL FEED — see §5. Not a stored procedure.
   v
PRDO.PlannedProductionOrders
   |
   |  [2] SQL Agent job "PRODUCTION PRDER", every 60 seconds:
   |        EXEC PRDO.jsProcessAllUsersProductionOrderApprovals 1
   |        EXEC PRDO.jsProcessAllUsersProductionOrderApprovals 2
   |      -> loops every user in the company
   |      -> dbo.jsGetQueries @userId, @company, 13   (13 = production order)
   |      -> PRDO.jsExecuteProductionOrderQueries
   v
PRDO.jsDocEntry (status 'P') + PRDO.jsDocEntryDetail
   |
   |  [3] Approver acts in the JSAP UI:
   |        PRDO.jsApproveProductionOrder  (Approve / Revoke)
   |        PRDO.jsRejectProductionOrder
   |      -> writes PRDO.jsProductionOrderStatusWorkflow
   |      -> advances currentStage, or sets status 'A' at the last stage
   v
   |  [4] Push back to HANA -> <schema>.PRODUCTIONORDERSYNC
   v
SAP: SBO_SP_TRANSACTIONNOTIFICATION blocks release without an 'A' row   <-- §7
```

### Stage logic (`jsApproveProductionOrder`)

Reasonably careful, for what it is:

- The user must be mapped to the current stage, or to the previous one when the current
  stage has had no action yet (`jsUserStage` → `jsStageTemplate`).
- A previous-stage user approving pulls the document **back** to their stage, and flips a
  rejected document back to Pending.
- Duplicate approval by the same user in the same stage throws 50020.
- Required approvals per stage come from `jsStage.approvalId` → `jsApprovalCount.approval`.
- Only when `approvedCount >= approvalRequired` does it advance; at the last stage it sets
  `status = 'A'`.
- `Revoke` is supported and un-approves.

`jsPrdoNotify` resolves who still has to act — remaining users in the current stage, or
the next stage's users once the current one is satisfied.

---

## 5. What feeds `PlannedProductionOrders` — and why it stopped

**Nothing in SQL Server writes this table.** Verified across all 75 online databases on
the instance: no stored procedure, no trigger, no SQL Agent job step. The six procedures
that reference it only read it.

So the feed is external — the JSAP application itself, presumably via the `HANADB112`
linked server (HANA, MSDASQL provider).

It wrote 3–29 rows every day from 23 Oct 2025 to **13 Aug 2026 19:10**, then stopped.
Nothing in 33 days. `PRODUCTIONORDERSYNC` in HANA stopped at the same moment
(last modified 13 Aug 19:16).

**The approval job looks perfectly healthy.** `PRODUCTION PRDER` has run every 60 seconds
since, most recently 14:56 on 15 Sep, and every history row says "The job succeeded." It
succeeds because the intake query is:

```sql
SELECT DISTINCT po.DocEntry
FROM PRDO.PlannedProductionOrders po
WHERE po.Status = 'P'
  AND NOT EXISTS (SELECT 1 FROM PRDO.jsdocentry jd WHERE jd.docEntry = po.DocEntry)
```

An empty source is not an error. Same silent-no-op shape as the tracker `.bat` files.

**Where to look:** the JSAP application's production-order sync, and the `HANADB112`
linked server. Not SQL Agent — that half is fine.

### Consequence

```
SAP OIL production orders posted 14 Aug – 15 Sep : 432
  Standard, non-raw-material (what JSAP approves) : 285
  present in JSAP                                 :   0
```

Plus **17 orders stuck in Planned** in JSAP, oldest 24 Oct 2025 (`FG0000317` Soyabean
15kg) — never approved, never rejected, now unreachable.

---

## 6. The wrong-item bug (18% of all flows)

`jsDocEntryDetail` disagrees with `PlannedProductionOrders`:

```
flows compared    : 2680
itemCode correct  : 2201     WRONG: 479  (17.9%)
warehouse correct : 2655     WRONG:  25
```

`PlannedProductionOrders` agrees with SAP `OWOR` every time — it is `jsDocEntryDetail`
that is wrong. Present in every single month, worst in May 2026 (100 of 337).

### Cause — proven

In `jsExecuteProductionOrderQueries`, inside the per-DocEntry cursor loop:

```sql
DECLARE @itemCode VARCHAR(100);
DECLARE @warehouse VARCHAR(50);
SELECT @itemCode = po.ItemCode, @warehouse = po.Warehouse
FROM PRDO.PlannedProductionOrders po
WHERE po.DocEntry = @docentry;
...
IF @newDocId IS NOT NULL AND @itemCode IS NOT NULL AND @warehouse IS NOT NULL
    INSERT INTO PRDO.jsdocentrydetail (...) VALUES (@itemCode, @warehouse, @company, @newDocId);
```

A `DECLARE` inside a T-SQL `WHILE` loop does **not** reset the variable, and
`SELECT @x = ...` that matches no row **leaves the previous value in place**. Confirmed
empirically on this server:

```sql
WHILE @i <= 3
BEGIN
    DECLARE @val VARCHAR(20);
    SELECT @val = val FROM @src WHERE k = @i;   -- no row for k = 2
    ...
END
-- k=1 -> AAA   k=2 -> AAA   k=3 -> CCC
```

So when the lookup misses, the previous order's item and warehouse are written, and the
`IS NOT NULL` guard passes because they are not null — they are simply someone else's.

The lookup misses because `#DocEntries` is built once at the top of the loop while the
external feed keeps upserting the same table underneath it. Evidence for that reading:

- The wrong value is almost always a *nearby* flow's correct value (offsets −1 to +9);
  only 42 of 479 have no match within ±40.
- Warehouse is wrong only 25 times, because nearly every order uses BH-PF — the same
  stale pair, visible only when the neighbour differed.

### Someone already hit this and papered over it

`jsGetPendingProductionOrders`:

```sql
INNER JOIN PRDO.PlannedProductionOrders ppo
    ON ppo.DocEntry = pd.docEntry
    --AND ppo.ItemCode = po.itemCode
    --AND ppo.Warehouse = po.warehouse
    --AND ppo.Company = po.company
```

The join predicates are commented out. So the **UI shows the right item** (read from
`PlannedProductionOrders`) and the approver is not misled — but `jsDocEntryDetail` is what
gets pushed to HANA, so **483 of 2,681 rows in SAP's `PRODUCTIONORDERSYNC` carry the wrong
item code** (18.0%).

### Fix

```sql
SET @itemCode = NULL; SET @warehouse = NULL;
```
immediately before the SELECT — or better, drop the variables and insert from a
`SELECT ... FROM PlannedProductionOrders WHERE DocEntry = @docentry` directly, so a
missing row writes nothing rather than something wrong.

The historical rows can be repaired from `PlannedProductionOrders`, which is correct.

### Related: a cross-company leak

The intake's `#DocEntries` query has **no company filter**, while `@company` is written
into `jsDocEntryDetail.company` and decides which HANA schema the row is pushed to. Result:
all 11 rows in the **BEVERAGES** `PRODUCTIONORDERSYNC` are OIL production orders — none of
those DocEntries exist in the Beverages `OWOR` at all. One approval request (flow id 3551,
DocEntry 10799) has sat Pending since 30 Apr 2026 on template 230, which is deactivated,
so it can never be actioned.

---

## 7. How SAP enforces it — and why the gate is effectively open

In `JIVO_OIL_HANADB.SBO_SP_TRANSACTIONNOTIFICATION` (line ~316). This procedure runs on
every document add/update and can block the transaction:

```sql
IF :object_type = N'202' AND (:transaction_type = N'U' OR :transaction_type = N'A')
THEN
IF EXISTS(
    SELECT * FROM "OWOR" A
    LEFT JOIN "PRODUCTIONORDERSYNC" P ON P."DOCENTRY" = A."DocEntry"
    INNER JOIN OITM O ON O."ItemCode" = A."ItemCode"
    WHERE A."DocEntry" = :list_of_cols_val_tab_del
      AND A."Status" = 'R'                              -- being Released
      AND A."Type"   = 'S'                              -- Standard only
      AND (P."STATUS" != 'A' OR P."STATUS" IS NULL)     -- not approved in JSAP
      AND O."Series" != 392                             -- not a raw material
      AND A."UserSign" != 33)                           -- <-- the exemption
THEN SELECT 202264,
  'The Production order is not approved by Sales Team, Ask to approve to release the production order'
```

Note `P."STATUS" IS NULL` counts as not approved — so with the feed dead, this should now
block every new release.

It blocks nothing, because of the last line:

**UserSign 33 is `USER24` — Gautam Chanana.**

```
OIL Standard, non-RM production orders since JSAP went live (3,152 total):
  Gautam CHanana    user 33   2,888  (92%)   <-- EXEMPT
  SHAHRUKH          user 36     126  (4%)
  RAVINDER SINGH    user 32      97  (3%)
  PANKAJ            user 44      30  (1%)
  manager           user  1       7
  KULBIR SINGH      user 30       3
  admin             user 56       1
```

All 285 Standard non-RM orders posted since the feed died were created by user 33. The
number the rule would have blocked is **0**.

BEVERAGES and MART have an object_type 202 rule in their notification procedures too, but
neither references `PRODUCTIONORDERSYNC` — there is no approval gate on production in
those companies at all.

---

## 8. Dead code worth knowing about

`PRDO.jsSyncDocEntriesToHana_Simple` writes `PRODUCTIONORDERSYNC` via
`OPENQUERY(HANA112, ...)`. **There is no linked server called `HANA112`** — the real one is
`HANADB112`. The budget equivalents (`bud.jsSyncDocEntriesToHana`,
`bud.jsSyncDocEntriesToHana_Simple`) use the correct name. Nothing calls the PRDO copy —
no job, no other procedure. It is a broken copy-paste of the budget procedure, and the
actual push must therefore come from the JSAP application.

`SAPSTATUS` is hardcoded to the literal `'PENDING'` in that procedure, and all 2,681 rows
in HANA carry `'PENDING'` — the column tracks nothing.

---

## 9. What to do, in order

1. **Restart the `PlannedProductionOrders` feed.** 432 orders and a month of approvals are
   missing. Look at the JSAP application's sync and the `HANADB112` linked server.
2. **Decide about `UserSign != 33`.** As written, the approval gate does not apply to 92%
   of production. Either the exemption is deliberate — in which case the rest of the
   mechanism is mostly ceremony — or it is a debugging leftover that was never removed.
   This is a business question, not a technical one.
3. **Fix the stale-variable bug** and backfill the 479 flows / 483 HANA rows from
   `PlannedProductionOrders`.
4. **Add the company filter** to the intake query, and clear the 11 stray OIL rows out of
   the Beverages `PRODUCTIONORDERSYNC`.
5. **Clear the 17 stranded Planned orders** and the one request stuck on the deactivated
   Beverages template.
6. **Make the job report emptiness.** A sweep that finds nothing for 33 days should not
   log "The job succeeded" 47,000 times in a row.
