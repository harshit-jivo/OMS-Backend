# Payments release runbook — production

Everything this release changes, in the order it must be done, with the check
that proves each step worked.

**Follow this document, not `PAYMENTS_WORKFLOW_DEPLOYMENT.md`.** That one
describes the workflow cutover only and predates the removals below.

Read §1 and §2 before running anything. The rest is copy-paste.

Every SQL statement below was executed read-only against TEST before this was
written. Two of them — the `approval_action` counts in §3 and §4 — necessarily
fail there, because TEST has already dropped that table; on production they
run before step 6 drops it, which is the whole point of them. Everything else
returns rows.

---

## 1. What this release does

| | Before | After |
|---|---|---|
| Approval engine | `approvals` app, 5 tables | generic Workflow Engine |
| Payments workflows | one, or none | **two** — RECEIPT and DEPOSIT, separate |
| Final approval | completed immediately | completes **only when SAP accepts** |
| Receiving account | `payment_method_mapping`, one per method per company | **the collector picks it**, snapshotted per line |
| Retry after SAP failure | — | offered to the final approver, backend-decided |
| Deposit `collected_amount` | cash **+ cheque** | **cash only** |
| Amount posted to SAP for a deposit | the receipts' full cash share, ignoring what was banked | **`deposit_amount`** — the cash actually paid in |
| Deposit types | Cash / Cheque / **Mixed** | **Cash / Cheque**, derived from contents |

Eight migrations, two data commands, one configuration step.

## 2. The two things that will bite you

**2.1 — Order is not optional.** Two migrations DESTROY tables
(`0040`, `0041`). Two others copy what those tables hold into
`payment_status_history` first (`0038`, `0039`), and a command copies the
mapping's answers onto the payment lines (`backfill_receiving_accounts`).
Migration dependencies enforce the migration half; **the command is not
enforced and must be run at the point shown in §4.** Run it late and documents
lose the account they were going to post to.

**2.2 — Configure the workflows BEFORE the code goes live.** The engine
selects a workflow by running configured queries. With none configured, every
payment and deposit submission is refused with *"No workflow is configured for
this document"* until someone fixes it. Configuration is forward-compatible —
it can be loaded while the current code is still serving — which is why §4
does it first.

---

## 3. Before the window

Run these against production. All read-only.

```bash
# 3a. Can receipts/deposits be keyed on their document number, and is the
#     engine configured? Blocks on anything that would stop submission.
python manage.py audit_approval_config

# 3b. Is the cash parent account set? Without it the cash picker is empty
#     and no cash receipt can be raised.
python manage.py validate_cash_parent
```

Then take the three numbers you will check again afterwards:

```sql
-- Approval decisions that exist ONLY in the old engine. 0038 copies these.
SELECT count(*) FROM payments.approval_action;

-- Payment lines with no receiving account. The backfill stamps the ones that
-- still get read.
SELECT count(*) FROM payments.payment_method_entry
WHERE account_key = '' AND gl_account = '';

-- Deposits with no frozen source drawer.
SELECT count(*) FROM payments.payment_bank_deposit WHERE source_gl_account = '';
```

**Back up the database.** Two tables are dropped and cannot be recovered from
the application.

---

## 4. The release, in order

### Step 1 — Configure both workflows (before deploying)

Payments needs **two** workflows under **one** module. Never register a
`PAYMENT` or `DEPOSIT` module; the module is `PAYMENTS`.

```
                    PAYMENTS  (one module)
                          |
          +---------------+---------------+
          |                               |
    RECEIPT workflow                DEPOSIT workflow
    query -> payment_receipt        query -> payment_bank_deposit
    RCP-...                         DEP-...
```

Each query MUST project `doc_no`, and the leading `SELECT *` is required —
configuration-time validation checks the default key `id`, so a projection
naming only `doc_no` is refused before it can be saved:

```sql
SELECT *, receipt_no AS doc_no FROM payments.payment_receipt
SELECT *, deposit_no AS doc_no FROM payments.payment_bank_deposit
```

The receipt workflow is configured on the Workflows page. The deposit one has
a command, because naming its approver is a decision nobody should make by
guessing:

```bash
python manage.py configure_deposit_workflow --approver <username> --dry-run
python manage.py configure_deposit_workflow --approver <username>
```

It refuses an approver without `Deposit_Approve`, a second deposit workflow
(two would make every submission ambiguous), and an unknown company.

Approvers need the matching action key as well as a stage: `Payments_Approve`
for receipts, `Deposit_Approve` for deposits. Being named on a stage is not
enough.

**Check:** `python manage.py audit_approval_config` reports
`WORKFLOW CONFIGURATION: PASS`, with one active query serving RECEIPT and one
serving DEPOSIT.

### Step 2 — Drain anything mid-approval in the old engine

A document sitting `PENDING_APPROVAL` in the old engine has no flow row in the
new one. After the release it can be **neither approved nor resubmitted** —
it sticks until someone changes it by hand.

```sql
-- MUST be 0 before you continue.
SELECT r.id, r.receipt_no, r.status
FROM payments.payment_receipt r
LEFT JOIN payments.payment_receipt_flow f ON f.receipt_id = r.id
WHERE r.status = 'PENDING_APPROVAL' AND f.id IS NULL;

SELECT d.id, d.deposit_no, d.status
FROM payments.payment_bank_deposit d
LEFT JOIN payments.payment_bank_deposit_flow f ON f.deposit_id = d.id
WHERE d.status = 'PENDING_APPROVAL' AND f.id IS NULL;
```

Decide each one **through the application**. Clearing them with SQL skips the
SAP posting, and an approved payment that never reached SAP is a book that
does not balance.

Freeze new submissions for this window, or the count will not converge.

### Step 3 — Deploy the backend code

Do not run `migrate` yet.

### Step 4 — Migrations, part one (preserve, then build)

```bash
python manage.py migrate payments 0039
```

| Migration | What it does |
|---|---|
| `0035_receiving_account_snapshot` | 7 `ADD COLUMN … DEFAULT '' NOT NULL` — metadata-only on PG 11+, no table rewrite |
| `0036_workflow_engine_runtime` | two flow tables, `stage_id` on the history |
| `0037_repair_payments_doc_no_alias` | adds a missing `doc_no` alias to payments queries |
| `0038_preserve_approval_decisions` | **copies every approval decision into `payment_status_history`** |
| `0039_repair_preserved_timestamps` | restores the real decision times on those rows |

**Check — this is the gate for everything that follows:**

```sql
-- Decisions that still exist ONLY in the old tables. MUST be 0.
SELECT count(*) FROM payments.approval_action a
JOIN payments.approval_request r ON r.id = a.request_id
WHERE NOT EXISTS (
  SELECT 1 FROM payments.payment_status_history h
  WHERE h.content_type_id = r.content_type_id
    AND h.object_id = r.object_id
    AND h.created_at BETWEEN a.acted_at - interval '5 min'
                         AND a.acted_at + interval '5 min'
    AND h.action = CASE a.action
        WHEN 'SUBMIT' THEN 'SUBMITTED'   WHEN 'RESUBMIT' THEN 'RESUBMITTED'
        WHEN 'APPROVE' THEN 'APPROVED'   WHEN 'REJECT'   THEN 'REJECTED'
        WHEN 'CANCEL' THEN 'CANCELLED'   WHEN 'RETURN'   THEN 'RETURNED'
        ELSE a.action END);
```

**If this is not 0, STOP.** The next migrations destroy the only copy.

Spot-check one posted payment's timeline and confirm it now names who
approved it — an `APPROVED … by <username>` row at the original time, not at
the time the migration ran.

### Step 5 — Backfill the receiving accounts (BEFORE step 6)

```bash
python manage.py backfill_receiving_accounts --dry-run   # review the list
python manage.py backfill_receiving_accounts
```

It stamps the account the mapping *would* have chosen onto every line that is
still going to be read — receipts awaiting SAP, and cash not yet banked — and
freezes each unsettled deposit's source drawer **from its own receipts**,
which is stricter than the mapping ever was.

It deliberately leaves alone lines on receipts already posted AND already
banked: nothing re-resolves them, and stamping today's mapping onto a receipt
that posted months ago would record an account SAP may never have used.

Lines it cannot resolve are listed and it exits non-zero. Lines with **no
mapping to inherit** are reported as a warning, not a failure — they could not
post before this release either, and their owner picks an account in the app.

**Check:**

```sql
-- Lines still needing an account they do not have. Should be 0, or only
-- documents the command listed as "no mapping to inherit".
SELECT r.receipt_no, e.method, r.status
FROM payments.payment_method_entry e
JOIN payments.payment_receipt r ON r.id = e.receipt_id
LEFT JOIN payments.payment_bank_deposit_line l ON l.receipt_id = r.id
WHERE e.account_key = '' AND e.gl_account = ''
  AND (r.status NOT IN ('POSTED','CANCELLED','CANCELLED_IN_SAP')
       OR (e.method = 'CASH' AND l.id IS NULL));

-- Unsettled deposits with no source drawer. MUST be 0.
SELECT count(*) FROM payments.payment_bank_deposit
WHERE source_gl_account = ''
  AND status NOT IN ('POSTED','CANCELLED','CANCELLED_IN_SAP');
```

### Step 6 — Migrations, part two (the deletions)

```bash
python manage.py migrate
```

| Migration | What it drops |
|---|---|
| `0040_drop_payment_method_mapping` | `payment_method_mapping` |
| `0041_drop_legacy_approval_tables` | `approval_action`, `approval_level_approver`, `approval_request`, `approval_level`, `approval_workflow` |

`0041` drops leaf-first with `DROP TABLE IF EXISTS` and **no CASCADE** — the
order makes it unnecessary, and nothing outside the group references them.
It lives in `payments` on purpose: the `approvals` app is gone from the code,
so nothing else could reach those tables.

**Check:**

```sql
-- All 6 gone.
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'payments'
  AND table_name IN ('payment_method_mapping','approval_action',
                     'approval_level_approver','approval_request',
                     'approval_level','approval_workflow');

-- Still there.
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'workflow';   -- workflows, workflow_queries,
                                   -- workflow_stages, workflow_modules,
                                   -- workflow_user_replacements
```

### Step 7 — Smoke test, then lift the freeze

On one real document of each type:

1. Raise a receipt → the method card offers **Receiving Cash Account** (cash)
   or **Receiving Bank Account** (UPI/cheque). Submit is blocked until one is
   chosen.
2. Verify and submit → it reaches stage 1 and that stage's user is notified.
3. Approve through to the final stage → SAP posts.
   - **accepted** → document `POSTED`, approval completes
   - **refused** → document `PENDING_ERROR`, still at the final stage, and the
     approver sees **Retry SAP Posting**
4. Open the timeline → the ladder shows each stage, who decided it and when.
5. Repeat for a bank deposit.
6. On a cheque, confirm **Customer Cheque Bank** and **Receiving Bank Account**
   are two separate fields with two different values.

Then lift the submission freeze.

### Step 8 — Release the clients

Web first, then mobile. Until a client is updated it can still READ payments;
only the approve/reject actions and the account picker are new.

---

## 4b. Deposits become cash-only (migration `0042`)

### Why

A deposit used to add cash and cheques into one `collected_amount`, and the SAP
posting ignored `deposit_amount` entirely — it summed the CASH entries of the
linked receipts and sent that. Both are wrong for the same reason: **a cheque
reached the bank when its own RECEIPT posted**, so it is already in SAP, and the
AP team routinely banks less cash than was collected (which is what
`shortfall_reason` records).

Seen on TEST `DEP-OIL-20260919-000002`: SAP credited **618,000** out of the cash
drawer against **2,091** actually banked — the books said the drawer was 615,909
emptier than it was.

After this: `collected_amount` = cash in the linked receipts, `deposit_amount` =
cash banked, and **the SAP document is `deposit_amount` exactly**.

### Before you run it — the one check that matters

Any deposit already posted while SHORT has a SAP document that overstates what
left the drawer. The migration fixes OMS's own figures; it cannot and must not
touch SAP. Find them **before** the migration, while the old numbers are still
readable:

```sql
-- Deposits posted to SAP for MORE than was actually banked.
-- Each row is a SAP document Finance must review. Expect zero.
SELECT d.deposit_no, d.company, d.deposit_date,
       d.sap_doc_num, d.sap_doc_entry,
       d.deposit_amount                        AS actually_banked,
       COALESCE(SUM(m.amount) FILTER (WHERE m.method = 'CASH'), 0)
                                               AS posted_to_sap,
       COALESCE(SUM(m.amount) FILTER (WHERE m.method = 'CASH'), 0)
         - d.deposit_amount                    AS overstated_by,
       d.shortfall_reason
FROM payments.payment_bank_deposit d
JOIN payments.payment_bank_deposit_line l ON l.deposit_id = d.id
JOIN payments.payment_method_entry m ON m.receipt_id = l.receipt_id
WHERE d.sap_doc_entry IS NOT NULL
GROUP BY d.id
HAVING COALESCE(SUM(m.amount) FILTER (WHERE m.method = 'CASH'), 0)
       > d.deposit_amount
ORDER BY 8 DESC;
```

**If this returns rows, stop and take the list to Finance.** Each one needs a
correcting entry in SAP; no migration can do it. The release can still proceed —
the migration only makes OMS agree with reality going forward — but the existing
SAP documents stay wrong until someone corrects them.

Then measure the backfill's reach (read-only, no locks):

```sql
SELECT count(*) FILTER (WHERE d.collected_amount <> cash)      AS collected_rewritten,
       count(*) FILTER (WHERE d.deposit_amount   > cash)       AS deposit_lowered,
       count(*) FILTER (WHERE cash = 0)                        AS become_zero_cash,
       count(*) FILTER (WHERE d.deposit_type = 'MIXED')        AS relabelled,
       count(*)                                                AS total
FROM payments.payment_bank_deposit d
JOIN LATERAL (
  SELECT COALESCE(SUM(m.amount) FILTER (WHERE m.method = 'CASH'), 0) AS cash
  FROM payments.payment_bank_deposit_line l
  JOIN payments.payment_method_entry m ON m.receipt_id = l.receipt_id
  WHERE l.deposit_id = d.id
) c ON true;
```

### Run it

```bash
python manage.py migrate payments 0042
```

One transaction, three statements against `payment_bank_deposit` only. On TEST
(20 deposits) it completed instantly. It takes an `ACCESS EXCLUSIVE` lock on
that one table twice — dropping and re-adding the CHECK — so run it in the
window, not under load.

### Verify

All five must return `0`:

```sql
SELECT 'collected <> cash total' AS check, count(*) FROM (
  SELECT d.id, d.collected_amount,
         COALESCE(SUM(m.amount) FILTER (WHERE m.method='CASH'),0) AS cash
  FROM payments.payment_bank_deposit d
  JOIN payments.payment_bank_deposit_line l ON l.deposit_id=d.id
  JOIN payments.payment_method_entry m ON m.receipt_id=l.receipt_id
  GROUP BY d.id, d.collected_amount) x
WHERE collected_amount <> cash
UNION ALL SELECT 'deposit > collected', count(*) FROM payments.payment_bank_deposit
  WHERE deposit_amount > collected_amount
UNION ALL SELECT 'shortfall with no reason', count(*) FROM payments.payment_bank_deposit
  WHERE deposit_amount < collected_amount AND btrim(shortfall_reason) = ''
UNION ALL SELECT 'negative amount', count(*) FROM payments.payment_bank_deposit
  WHERE deposit_amount < 0
UNION ALL SELECT 'still MIXED', count(*) FROM payments.payment_bank_deposit
  WHERE deposit_type = 'MIXED';
```

### The shortfall reason now travels to SAP

`ORCT.Comments` used to carry the user's remarks alone, so a short deposit
showed SAP a credit smaller than the day's collections with nothing explaining
the gap. The reason existed only in OMS.

`build_deposit_remarks` now appends it:

```
Testing 2 remarks | SHORT 10.00 of 100.00 collected: Testing 2
```

The shortfall note is the BASE and the user's remarks are appended, so when the
254-character limit bites (`ORCT.Comments` is `NVARCHAR(254)` — verified) the
free text is clipped and the reason survives whole. Losing the reason would
leave an unexplained credit, which is the failure this prevents.

**A deposit banked in full is unchanged** — remarks alone, or `OMS <deposit_no>`
when there are none. Code only; no migration, and nothing already in SAP moves.

Spot-check after the first short deposit posts:

```sql
-- The reason should appear in Comments alongside the remark.
SELECT deposit_no, deposit_amount, collected_amount,
       shortfall_reason, sap_doc_num
FROM payments.payment_bank_deposit
WHERE deposit_amount < collected_amount
  AND sap_doc_num IS NOT NULL
ORDER BY id DESC LIMIT 5;
```

Then read `Comments` for those `sap_doc_num` values in SAP.

### Client note

The mobile app must ship with this. An old build still offers **Mixed** and
still sends `collected_amount` as cash + cheque, which the new server rejects
("Selected payments hold X in cash but collected_amount is Y") — so deposits
cannot be created from a stale client until it updates. Receipts are unaffected,
and reading deposits is unaffected.

---

## 4c. Run the scheduler worker — approvals are lost without it

### The failure

The final approval defers its SAP call to `transaction.on_commit`, so a 5-7
second call does not hold the approval's row locks. **That callback is not
durable.** A restart between the commit and the callback — a deploy, a recycle,
an OOM kill, a dev-server autoreload — loses the call entirely. The document is
left in `POSTING_TO_SAP` owing a post that nothing will ever make, and it is
invisible to every other sweep.

Seen on `RCP-OIL-20260919-000003`: approved 09:38:48, **zero** SAP call log
rows, no DocEntry. A log row is written *before* the request leaves, so its
absence proves SAP was never called.

### The fix: run the worker

```bash
python manage.py run_scheduler
```

This is a long-lived process, separate from the web server, and it must be
supervised (systemd / NSSM / Task Scheduler) so it restarts with the machine.
It holds a Postgres advisory lock, so a second copy exits rather than
double-running — overlapping restarts are safe.

It now sweeps for stranded posts **once immediately on startup** and every
`PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS` thereafter. The immediate sweep matters:
a restart is what strands a document, so the moment the worker comes back is
the first moment worth looking.

**Without this worker running, a payment stranded by a restart stays unposted
until a human notices and runs the command by hand.**

### Settings

| setting | default | notes |
|---|---|---|
| `PAYMENTS_SAP_STRANDED_MIN_AGE_MINUTES` | `30` | How long a document must owe a post before it is treated as lost. |
| `PAYMENTS_SAP_SWEEP_INTERVAL_SECONDS` | `300` | Floored at 30. |

The age threshold guards one window: a call genuinely in flight that has not
yet written its `SapCallLog` row. `sap_poster.post_document` calls
`_log_start()` **outside** any atomic block, so that row autocommits *before*
the HTTP request leaves — the window is two statements wide, i.e.
milliseconds. 30 minutes is a deliberately large margin because the cost of
waiting is a delayed payment while the cost of being early is a **duplicate**
one.

**On a test or staging box set it to 2-5.** A dev server autoreloads on every
file save, so stranding is routine there, and a half-hour wait makes recovery
look broken.

### Manual recovery

Always safe, and safe to repeat — a document that posts leaves
`POSTING_TO_SAP`, and `post_document` has its own duplicate guard:

```bash
python manage.py recover_stranded_sap_posts --dry-run     # list
python manage.py recover_stranded_sap_posts               # post
python manage.py recover_stranded_sap_posts --company OIL # one company
```

### Find stranded documents by hand

```sql
SELECT r.receipt_no, r.company, r.status, r.updated_at,
       now() - r.updated_at AS owing_for
FROM payments.payment_receipt r
WHERE r.status = 'POSTING_TO_SAP'
  AND r.sap_doc_entry IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM payments.payment_sap_call_log l
        WHERE l.object_id = r.id
          AND l.content_type_id = (SELECT id FROM django_content_type
                                    WHERE app_label='payments'
                                      AND model='paymentreceipt'))
ORDER BY r.updated_at;
```

Swap `payment_receipt` / `paymentreceipt` for `payment_bank_deposit` /
`bankdeposit` to check deposits.

---

## 5. Migration safety notes

* `0035` — `ADD COLUMN … DEFAULT` is **metadata-only** on PostgreSQL 11+
  (production runs 16). Declared with `db_default`, so there is no
  `DROP DEFAULT` and old code inserting during a rolling deploy still works.
* `0036` — the two flow tables are created empty. The one statement touching an
  existing table is `CREATE INDEX` on `payment_status_history`, which is **not**
  `CONCURRENTLY` and holds an `ACCESS EXCLUSIVE` lock while it builds. Measure
  first:
  ```sql
  SELECT c.reltuples::bigint AS rows,
         pg_size_pretty(pg_total_relation_size(c.oid)) AS size
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'payments' AND c.relname = 'payment_status_history';
  ```
  Under ~1M rows, build it in the migration. Much larger, or a busy write
  window: create it `CONCURRENTLY` first, then migrate.
* `0038`/`0039` — write-only, no schema change.
* `0040`/`0041` — destructive, and the reason §3 says take a backup.
* `0042` — rewrites deposit amounts (see §4b). Three things about it worth
  knowing before it runs:
  * It drops `bank_deposit_amount_positive`, backfills, then re-adds it as
    `>= 0`. The drop must come first: a cheque-only deposit legitimately
    banks zero cash, and the old `> 0` would reject exactly those rows.
  * `collected_amount` and `deposit_amount` move in **one** `UPDATE`.
    PostgreSQL checks constraints per statement, so lowering `deposit_amount`
    alone leaves a row failing `bank_deposit_shortfall_requires_reason` for the
    length of that statement. This was hit on TEST
    (`DEP-OIL-20260907-000001`) before the statements were merged.
  * It ends with `SET CONSTRAINTS ALL IMMEDIATE`. Without it the re-add fails
    with *"cannot ALTER TABLE … because it has pending trigger events"* — the
    whole migration is one transaction and PostgreSQL will not alter a table
    with trigger events still outstanding against it.

---

## 6. Rollback

| Situation | Action |
|---|---|
| Before step 4 | Redeploy the previous build. Nothing has changed. |
| After step 4, before step 5 | `migrate payments 0034`, redeploy. This also drops the snapshot columns — harmless while nothing has been raised on the new code. |
| After step 6 | **Do not roll the schema back.** The dropped tables cannot be restored by a migration. Roll the CODE back only if the new columns stay in place, or fix forward. |
| After `0042` | `migrate payments 0041` restores the old constraint and recomputes `collected_amount` as cash + cheque. It **cannot** restore a `deposit_amount` the migration lowered — the old value was the cash-plus-cheque total and carried no record of the cash share. Those rows keep the truthful smaller figure. Deliberate: the alternative is re-introducing a number known to be wrong. |

---

## 7. Afterwards

Run at the end of the first day:

```sql
-- Documents that say they are in approval but have no flow. Should be empty,
-- always. Anything here is stranded and needs a human.
SELECT r.receipt_no, r.status FROM payments.payment_receipt r
LEFT JOIN payments.payment_receipt_flow f ON f.receipt_id = r.id
WHERE r.status = 'PENDING_APPROVAL' AND f.id IS NULL
UNION ALL
SELECT d.deposit_no, d.status FROM payments.payment_bank_deposit d
LEFT JOIN payments.payment_bank_deposit_flow f ON f.deposit_id = d.id
WHERE d.status = 'PENDING_APPROVAL' AND f.id IS NULL;

-- New receipts should now carry the account the collector chose.
SELECT count(*) FILTER (WHERE e.account_key <> '') AS with_account,
       count(*)                                    AS total
FROM payments.payment_method_entry e
JOIN payments.payment_receipt r ON r.id = e.receipt_id
WHERE r.created_at > now() - interval '1 day';
```

`python manage.py audit_approval_config` is read-only and safe to re-run any
time; it reports the workflow configuration, document states, and anything
mid-approval without a flow.
