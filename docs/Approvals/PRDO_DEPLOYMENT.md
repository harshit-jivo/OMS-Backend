# PRDO — production deployment runbook

Everything needed to take Production Order approval from the test environment to
live, in order, with the verification for each step.

> **NOTHING IN THIS DOCUMENT HAS BEEN APPLIED TO LIVE.**
>
> No table has been created and no procedure has been changed in
> `JIVO_OIL_HANADB`, `JIVO_BEVERAGES_HANADB` or `JIVO_MART_HANADB`, and no
> migration has been run against the live Postgres. Everything below is a
> written plan; the only environment actually wired is
> `TEST_JIVO_OIL_HANADB`. The live figures in §1 come from reading the
> catalogues, which is read-only.

**Read §2 before anything else.** Deploying to live is NOT the same operation as
the test wiring, because live OIL already has a production-order gate and test
did not. Running the test script against live would destroy 22,791 lines of
business rules.

Companion documents: `PRDO_DESIGN.md` (why it is built this way),
`PRODUCTION_ORDER_JSAP_SAP.md` (what JSAP did and where it broke),
`prdo_test_wiring.sql` (the TEST-only script — do not run it on live).

---

## 1. Where things stand, verified

Read from the live and test HANA catalogues on 16 Sep 2026.

| schema | `OMS_PRDO_APPROVAL` | `PRODUCTIONORDERSYNC` rows | TN proc lines | has a 202 block | reads `PRODUCTIONORDERSYNC` | `OWOR.U_BATCH_NO` |
|---|---|---|---|---|---|---|
| `JIVO_OIL_HANADB` | **no** | 2,681 | **22,791** | yes | **yes** | yes |
| `JIVO_BEVERAGES_HANADB` | **no** | 11 | 6,480 | yes | no | **no** |
| `JIVO_MART_HANADB` | **no** | 0 | 6,329 | yes | no | yes |
| `TEST_JIVO_OIL_HANADB` | yes | 2,681 | 37 | yes | **no** (now reads the new table) | yes |
| `TEST_JIVO_BEVERAGES_HANADB` | no | 11 | 6,466 | yes | no | **no** |
| `TEST_JIVO_MART_HANADB` | no | 0 | 6,318 | yes | no | yes |

Postgres, live (`order_management` on .118): **no `workflow` schema and no
`production` schema.** Neither module has ever been migrated there.

Three consequences that shape everything below:

1. **Live OIL's notification procedure is 22,791 lines and already gates
   production orders.** The change there is an EDIT to three lines, never a
   `CREATE OR REPLACE` of the whole procedure.
2. **Live Beverages and Mart have no production gate at all.** Their procedures
   mention object type 202 for other rules but never read the approval table.
   Adding a gate there is a new business rule, not a migration — see §6.
3. **Beverages has no `U_BATCH_NO` / `U_MFG` / `U_EXP_DATE` on `OWOR`.** The
   sync already handles this (`_planned_order_sql` builds the SELECT from
   `SYS.TABLE_COLUMNS` per schema), but it is why a query written for OIL
   cannot be reused verbatim.

---

## 2. The one dangerous step, stated first

`JIVO_OIL_HANADB.SBO_SP_TRANSACTIONNOTIFICATION` is 22,791 lines covering every
document type in the company. It is the procedure SAP runs on every add and
update, and it can block postings. **Do not replace it. Edit it.**

Its production-order block currently reads, at roughly line 316:

```sql
IF :object_type = N'202'
AND (:transaction_type = N'U'
     OR :transaction_type = N'A')
THEN
IF EXISTS(Select
     *
    from "OWOR" A
    Left Join "PRODUCTIONORDERSYNC" P On P."DOCENTRY"=A."DocEntry"     -- line ~323
    Inner Join OITM O ON O."ItemCode"=A."ItemCode"
    Where A."DocEntry"=:list_of_cols_val_tab_del
    and A."Status"='R'
    and A."Type"='S'
    and (P."STATUS"!='A'                                               -- line ~328
        or P."STATUS" IS NULL)                                         -- line ~329
    and O."Series"!=392
    and A."UserSign"!=33)
THEN Select
     202264,
     'The Production order is not approved by Sales Team, Ask to approve to release the production order'
FROM DUMMY
;
```

**Exactly three lines change**, and the third and fourth are easy to miss:

| line | from | to |
|---|---|---|
| ~323 | `Left Join "PRODUCTIONORDERSYNC" P On P."DOCENTRY"=A."DocEntry"` | `Left Join "OMS_PRDO_APPROVAL" P On P."DocEntry"=A."DocEntry"` |
| ~328 | `and (P."STATUS"!='A'` | `and (P."Status"!='A'` |
| ~329 | `or P."STATUS" IS NULL)` | `or P."Status" IS NULL)` |

**HANA quoted identifiers are case-sensitive.** `PRODUCTIONORDERSYNC` has
`DOCENTRY` and `STATUS` in upper case; `OMS_PRDO_APPROVAL` has `DocEntry` and
`Status` in mixed case. Changing only the join line leaves
`P."STATUS"` pointing at a column that does not exist, and the procedure fails
to compile — which, in a transaction notification, means **nothing can be
posted in that company**. Change all three together or none.

The join also improves: `PRODUCTIONORDERSYNC."DOCENTRY"` is `NVARCHAR` compared
against an `INTEGER`, relying on implicit conversion. `OMS_PRDO_APPROVAL."DocEntry"`
is a real `INTEGER`.

### How to make the edit safely

1. Extract the current definition to a file and keep it. This is the rollback:
   ```sql
   SELECT "DEFINITION" FROM SYS.PROCEDURES
   WHERE SCHEMA_NAME = 'JIVO_OIL_HANADB'
     AND PROCEDURE_NAME = 'SBO_SP_TRANSACTIONNOTIFICATION';
   ```
2. Edit only those three lines in that file.
3. `diff` the old and new. **The diff must be exactly three lines.** If it is
   more, something reformatted the file — stop.
4. Apply with `CREATE OR REPLACE PROCEDURE` using the full edited text, having
   first set the session schema so unqualified table names bind correctly:
   ```sql
   SET SCHEMA "JIVO_OIL_HANADB";
   ```
   Unqualified names inside a HANA procedure resolve against the SESSION's
   default schema at creation time, not the procedure's. Getting this wrong
   produces `Could not find table/view OWOR in schema <your user>` — it cost a
   failed attempt on test.
5. Verify it compiled:
   ```sql
   SELECT IS_VALID FROM SYS.PROCEDURES
   WHERE SCHEMA_NAME='JIVO_OIL_HANADB' AND PROCEDURE_NAME='SBO_SP_TRANSACTIONNOTIFICATION';
   ```
6. Post one harmless document of another type (an A/R invoice in a sandbox
   company, or check with someone who can) to confirm the procedure still
   permits normal work. A procedure that fails to compile blocks everything,
   and the production-order rule is not the only thing in it.

Do this in a maintenance window. It is the only step in this runbook that can
stop the business.

---

## 3. Postgres — the OMS side

On .118, `C:\LiveProjects\OMS\Backend`.

```powershell
git pull                                    # whatever branch production runs
python manage.py makemigrations --check     # must say "No changes detected"
python manage.py migrate workflow
python manage.py migrate backdate           # if BKDT is not already live
python manage.py migrate production
```

`production/migrations/0001_initial.py` creates the `production` PostgreSQL
schema itself (`CREATE SCHEMA IF NOT EXISTS production`) and depends on
`workflow.0007_drop_runtime_and_testflow`, so workflow must go first.

`migrate` also registers the module with the engine, from
`production/apps.py` on `post_migrate`:

```
register_module(code='PRDO', name='Production Order')
```

**Verify:**

```sql
SELECT table_name FROM information_schema.tables WHERE table_schema = 'production';
-- production_order, production_order_flow, production_order_action_logs

SELECT code, name FROM workflow.workflow_modules WHERE code = 'PRDO';
-- one row
```

Until `migrate production` has run, the workflow conditions cannot be validated
— the validator `EXPLAIN`s against `production.production_order`. The seed
command refuses to run before then and says so.

---

## 4. HANA — create the approval table

One per company you intend to gate. **OIL is the only one required**; do
Beverages and Mart only if §6 says you want gates there.

The table is identical in every schema. Column names are mixed case and must
stay that way — §2 depends on it.

```sql
CREATE COLUMN TABLE "JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL" (
    "DocEntry"    INTEGER       NOT NULL,   -- OWOR.DocEntry
    "Status"      NVARCHAR(1)   NOT NULL,   -- 'A' approved / 'P' pending / 'R' rejected
    "Company"     NVARCHAR(20)  NOT NULL,   -- traceability only, never joined on
    "RequestId"   BIGINT,                   -- production.production_order.id
    "DecidedBy"   NVARCHAR(100),            -- OMS username, audit only
    "DecidedAt"   TIMESTAMP,
    "SyncedAt"    TIMESTAMP     NOT NULL,
    PRIMARY KEY ("DocEntry"),
    CONSTRAINT "OMS_PRDO_APPROVAL_STATUS_CHK" CHECK ("Status" IN ('A','P','R'))
);
```

Repeat with `JIVO_BEVERAGES_HANADB` / `JIVO_MART_HANADB` if gating those.

`DocEntry` alone is the primary key here because **the schema IS the company**.
That is not true on the OMS side, where one table holds all three and the key is
`(company, sap_doc_entry)` — DocEntry sequences run per company, and JSAP's
missing company column is how 11 OIL orders ended up recorded against Beverages.

### 4.1 Seed it — OIL only, and do not skip this

```sql
INSERT INTO "JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL"
       ("DocEntry","Status","Company","RequestId","DecidedBy","DecidedAt","SyncedAt")
SELECT TO_INT("DOCENTRY"), "STATUS", 'OIL', NULL,
       'migrated-from-jsap', "UPDATEDON", CURRENT_TIMESTAMP
FROM   "JIVO_OIL_HANADB"."PRODUCTIONORDERSYNC"
WHERE  "DOCENTRY" IS NOT NULL
  AND  "STATUS" IN ('A','P','R');
```

Expect **2,681 rows**. Without this, every historical order becomes
un-releasable the moment §2's edit lands, because the gate would see no row and
treat it as unapproved. The seed makes the switch a no-op: the only variable
changing is which table the rule reads.

`PRODUCTIONORDERSYNC."DOCENTRY"` is `NVARCHAR`, hence `TO_INT`.

Beverages has 11 rows in `PRODUCTIONORDERSYNC`, but **all 11 are OIL orders**
recorded there by JSAP's company-blind intake — none of those DocEntries exist
in the Beverages `OWOR`. Do **not** seed Beverages from it. Mart has 0 rows.

**Verify:**

```sql
SELECT "Status", COUNT(*) FROM "JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL" GROUP BY "Status";
```

### 4.2 Order of operations between §2 and §4

**Create and seed the table BEFORE editing the procedure.** The reverse leaves a
window in which the gate points at a table that does not exist — which, in a
transaction notification, blocks releases rather than allowing them.

---

## 5. Permissions, users and workflows

### 5.1 Keys

`core/permission_registry.py` already declares them; they exist as soon as the
code deploys:

```
Production_Order            — view requests
Production_Order_Approval   — approve/reject
```

Grant both to each approver. **`Production_Order_Approval` alone is not enough
to approve** — the backend also requires the caller to be the current effective
user of that workflow stage. Someone holding only the key sees an empty queue,
which is the honest outcome.

### 5.2 The approver accounts — UNRESOLVED

Live has two candidates and the right one has not been confirmed:

```
id  2   preshit        Preshit
id 51   preshit_singh  Preshit singh
```

JSAP's production approver is jsUser 101, `Preshit@jivo.in`, empId JWPL0030.
Neither OMS account's email matches, so this cannot be resolved from the data.
**Ask before configuring** — the wrong choice routes every finished-goods order
to someone who is not expecting it.

**There is no Shahrukh account in OMS.** JSAP routes packaging material
(`item_code LIKE 'PM%'`) to him. Either create the account, or point the PM
stage at the same person as FG. Do not simply omit the PM workflow: the FG
condition explicitly excludes `PM%`, so those orders would fail to route with
`WorkflowNotConfigured` rather than falling through.

### 5.3 Workflows

Either configure at `/Workflows` (needs `workflow.config.manage`) or run:

```powershell
python manage.py seed_prdo_workflows --fg-approver <user> --pm-approver <user> --dry-run
python manage.py seed_prdo_workflows --fg-approver <user> --pm-approver <user>
```

The configuration that is running on test, for reference:

| workflow | company | query | stage 1 |
|---|---|---|---|
| `PRDO_FG` | `ALL` | `SELECT id FROM production.production_order WHERE item_code NOT LIKE 'PM%'` | → FG approver |
| `PRDO_PM` | `ALL` | `SELECT id FROM production.production_order WHERE item_code LIKE 'PM%'` | → PM approver |

Rules that are easy to get wrong:

* **The query's own `company` must match the workflow's.** Company is filtered
  at both levels (`selection.candidate_queries`), so a query left on `OIL`
  under an `ALL` workflow silently never applies to Beverages or Mart.
* **`SELECT id`, not `SELECT 1`** — the engine joins on the key column.
* **No `AND id = …` inside** — the engine binds the id on an outer wrapper.
* **Write `%` once, never `%%`** — the engine escapes it for psycopg2.
* **The pair must be exhaustive and non-overlapping.** Zero matches raises
  `WorkflowNotConfigured`, two raises `AmbiguousWorkflowSelection`, and the
  engine does no tie-breaking. `LIKE 'PM%'` / `NOT LIKE 'PM%'` partitions every
  order exactly once.
* **`ALL` does not outrank a specific company.** Two `ALL` workflows split by
  item code is fine; adding an `OIL`-scoped one alongside them makes every OIL
  order ambiguous.

**Verify** — this runs selection for real against live data and rolls it back,
so it proves routing rather than predicting it:

```powershell
python manage.py sync_production_orders --company OIL --dry-run
```

Every line should read `would route DocEntry … -> PRDO_FG / … -> <user>`, with
no `NOT ROUTED`.

---

## 6. Beverages and Mart — a decision, not a step

Neither company's notification procedure gates production today. JSAP's
Beverages templates (230, 436) are inactive and its planned-order table holds
zero Beverages rows.

**Recommended: ship OIL only.** Then the §2 edit is the only procedure change,
and Beverages/Mart keep behaving exactly as they do now.

If you do want gates there later, each needs: the table from §4 (no seed — there
is nothing valid to seed from), a new 202 block added to that company's
procedure, and a workflow whose company is `BEVERAGES` or `MART`. Adding a block
to a procedure that has none is lower-risk than editing OIL's, but it is still a
new rule that can block releases in a company that has never had one.

Note the sync only reads companies that have an **active PRDO workflow**
(`_configured_companies`), so leaving them unconfigured means they are never
queried. If you configure an `ALL` workflow, the command will try all three —
use `--company OIL` on the scheduled task until the others are genuinely ready.

---

## 7. The scheduled job

On .118, as SYSTEM:

```
schtasks /create /tn "OMS Production Order Sync" ^
  /tr "\"C:\LiveProjects\OMS\Backend\run_production_sync.bat\"" ^
  /sc minute /mo 15 /ru SYSTEM /rl HIGHEST /f
```

Every 15 minutes. JSAP ran every 60 seconds for a feed averaging 3–29 rows a
**day**.

Exit codes, and why they matter:

| code | meaning |
|---|---|
| 0 | ran, and saw orders (or has not been silent long enough to worry) |
| 1 | SAP unreachable, a row failed, or an order could not be routed |
| 2 | SAP returned nothing, and has returned nothing for over `PRODUCTION_SYNC_SILENCE_HOURS` (default 24) |

**Code 2 is not noise.** JSAP's feed died on 13 Aug 2026 and its job reported
success every 60 seconds for 33 days, because "no rows" and "no connection"
looked identical to it; 432 production orders went through no approval at all.

The `.bat` ends on `exit /b %RC%`, not `echo`. A `.bat` returns its LAST
command's exit code — ending on `echo` is what made three tracker tasks report
`Last Result: 0x0` while doing nothing for weeks.

**While you are on that box:** the `%~dp0` path fix to the six existing
`run_*.bat` files has still not been deployed there. Three tracker tasks are
silent no-ops on .118 today. Fix them in the same visit.

**Verify:** run the task once by hand, then check
`GET /api/production/health/` — it reports last-sync per company, so "is the
feed alive?" is answerable without opening Task Scheduler.

---

## 8. Frontend

Deploy the built assets as usual. Two new routes, both gated by
`routeAccess.ts`:

```
/Production_Orders     Production_Order
/Production_Approval   Production_Order_Approval
```

They appear under a **Production** group in the sidebar and in the admin
page-permission list.

**Typecheck with `npm run build` or `npx tsc -b`, never `tsc --noEmit`.**
`tsconfig.json` is solution-style (`"files": []` plus references), so
`tsc --noEmit` compiles zero files and reports success on anything. That
mistake hid five real type errors in this module.

---

## 9. Post-deployment verification, in order

1. `SELECT table_name FROM information_schema.tables WHERE table_schema='production'` → 3 tables
2. `SELECT code FROM workflow.workflow_modules WHERE code='PRDO'` → 1 row
3. Both workflows present, `is_active`, **both queries `validated_at IS NOT NULL`**
4. `SELECT COUNT(*) FROM "JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL"` → 2,681
5. `SELECT IS_VALID FROM SYS.PROCEDURES WHERE …` → `TRUE`
6. `sync_production_orders --company OIL --dry-run` → every order routes, none `NOT ROUTED`
7. `sync_production_orders --company OIL` → orders created; check `production_order` count
8. `/api/production/health/` → `last_synced_at` is recent for OIL
9. The FG approver opens `/Production_Approval` and sees a non-empty queue
10. Approve one order, then confirm the SAP row:
    ```sql
    SELECT "DocEntry","Status","DecidedBy","DecidedAt"
    FROM "JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL" WHERE "RequestId" IS NOT NULL;
    ```
    Expect `Status = 'A'` and the approver's username.

---

## 10. Rollback

In reverse order of risk.

**The procedure** — restore the definition saved in §2 step 1. Do this first if
anything is blocking postings.

**The scheduled task** — `schtasks /delete /tn "OMS Production Order Sync" /f`.
Nothing else depends on it; unsynced orders are simply not in OMS.

**The workflows** — `python manage.py seed_prdo_workflows --deactivate`, or
untick them at `/Workflows`. Flows already running are unaffected:
`stages_for` filters on stage activity, not workflow activity, so nothing in
flight deadlocks.

**The HANA table** — only after the procedure no longer references it:
`DROP TABLE "JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL";`

**The Postgres schema** — `python manage.py migrate production zero` reverses
the initial migration, which drops the `production` schema CASCADE. Destroys
every recorded decision, so treat it as a last resort.

---

## 11. Still open

1. **Which Preshit** (§5.2) — blocks configuration.
2. **Shahrukh's account** (§5.2) — blocks the PM workflow.
3. **`AND A."UserSign" <> 33`.** As it stands the gate exempts the user who
   raised **2,888 of 3,152** eligible OIL orders, so approval is a record rather
   than a control for ~92% of production. OMS reports this (`gate_exempt` on the
   API, a badge in the UI, a count on the insights endpoint) but cannot change
   it. Removing the line is a business decision, and §2 is the natural moment
   for it — you are already in that procedure.
4. **Beverages and Mart** (§6) — gate them or not.
5. **Raw-material orders** route to `PRDO_FG` under `NOT LIKE 'PM%'`. That is
   938 OIL orders a year JSAP never approved. SAP exempts `Series = 392` anyway,
   so they will show as *not gated by SAP* — but the approver will see them.
6. **List views are not company-scoped.** Anyone with `Production_Order` sees
   every company's orders; only *acting* is scoped, by stage. If that is not
   acceptable, it is shared `core` work, not a PRDO change.
