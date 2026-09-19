# Payments → Workflow Engine: production deployment runbook

> **SUPERSEDED for deployment — follow `PAYMENTS_RELEASE_RUNBOOK.md`.**
>
> This document covers the workflow cutover alone and predates the removals
> that shipped with it: the old `approvals` engine and `payment_method_mapping`
> have since been deleted, and the release now includes two data commands whose
> ORDER matters. Running the steps below on their own would drop tables whose
> contents had not been copied.
>
> Kept for the background it explains — why selection keys on `doc_no`, what
> the two workflows are, and how the final approval is coupled to SAP.

Moving Payments off the old `approvals` engine and onto the generic Workflow
Engine, without interrupting live payment collection.

Read this whole document before starting. The schema change is the easy part;
the risk is almost entirely in **configuration and in-flight work**, and one of
the failure modes strands documents in a state no user can clear.

**Nothing in this release deletes the old `approvals` app, its tables or its
data.** That is a separate step, taken only after this one has run in
production for a full approval cycle. See [After the release](#8-after-the-release).

---

## 1. What changes

| | Before | After |
|---|---|---|
| Routing | `approvals.ApprovalWorkflow` / `ApprovalLevel` | `workflow.Workflow` / `WorkflowStage` |
| Runtime state | `approvals.ApprovalRequest` | `payment_receipt_flow`, `payment_bank_deposit_flow` |
| Who may approve | level membership | permission key **AND** the stage's effective user |
| Approve endpoint | `POST /api/approvals/requests/<id>/act/` | `POST /api/payments/receipts/<pk>/approve/` |
| Queue | `/api/approvals/…` | `GET /api/payments/approvals/queue/` |
| Selection key | document id | **document number** (`doc_no`) |

Two migrations ship: `payments/0035` (receiving-account snapshot columns) and
`payments/0036` (the two flow tables plus a `stage_id` column on the history).

### The final approval is coupled to SAP

Approving the last stage AUTHORISES the posting; it does not complete the
workflow. The flow stays PENDING at that final stage until SAP accepts the
document:

| SAP answered | document | flow | who holds it |
|---|---|---|---|
| accepted | `POSTED` | `APPROVED`, no stage | nobody — it is finished |
| refused | `PENDING_ERROR` | `PENDING` at the final stage | the same final approver, who retries |
| never answered | `SAP_UNKNOWN` | `PENDING` at the final stage | nobody — the retry is REFUSED until it is verified |

Intermediate stages never call SAP.

Two consequences worth knowing before the release:

* **`POSTING_TO_SAP` now means "a final approval was given and a post is
  owed"**, and it is written before the call is made so the intent survives a
  lost `on_commit` callback. The stranded-document sweep keys on it, with a
  30-minute age threshold so it never races a posting in flight.
* **A cheque-only deposit completes without any SAP call** — its cheques
  reached SAP when their receipts posted — so it finishes its approval on the
  "SAP was not needed" path rather than the "SAP accepted it" one. Both
  complete the flow.

---

## 2. The four ways this can break production

Each is preventable. None is detected by the migration itself.

### 2.1 No PAYMENTS workflow configured — submission stops (CRITICAL)

The engine selects a workflow by **running the configured queries**. If no
active `PAYMENTS` workflow matches a document, `submit_receipt` raises and the
user sees:

> No workflow is configured for this document. Nothing was submitted.

Every payment and deposit submission fails until configuration exists. Nothing
is corrupted — the submission is refused, not half-applied — but collection
stops. **Configure before the code is deployed** (§4).

### 2.2 A query without the `doc_no` alias — submission stops (CRITICAL)

Payments registers **one** module for two document types, and receipt ids and
deposit ids overlap, so selection keys on the document *number*. Every payments
query must project it under the alias `doc_no`:

```sql
SELECT *, receipt_no AS doc_no FROM payments.payment_receipt
SELECT *, deposit_no AS doc_no FROM payments.payment_bank_deposit
```

The leading `SELECT *` is **mandatory**, not decoration: configuration-time
validation cannot know the runtime key column, so it validates against the
default key `id`, and a projection naming only `doc_no` is refused before it
can be saved.

A query that saves cleanly but omits the alias fails only at submit time, with:

```
ProgrammingError: column wf_q.doc_no does not exist
```

> This is not hypothetical. The TEST database currently holds an active
> workflow `1ST_APPROVAL_FOR_OIL` whose query is
> `SELECT * FROM payments.payment_receipt WHERE company = 'OIL'` — no alias —
> so OIL receipts cannot be submitted in TEST today. Fix it there first; it is
> the same mistake that would take production down.

### 2.3 In-flight approvals strand (CRITICAL — irreversible without DBA help)

A document that is `PENDING_APPROVAL` in the **old** engine has no row in
`payment_receipt_flow`. After the release it:

- **cannot be approved or rejected** — there is no flow, so every decision
  endpoint answers *"This document is not awaiting approval."*
- **cannot be resubmitted** — `submit_receipt` refuses with *"A pending
  approval receipt cannot be submitted."*

It is stuck, and only a manual `UPDATE` can free it. **Drain the queue to zero
before the release** (§3.2). No migration back-fills these on purpose: a
fabricated flow would invent a stage and an approver who never decided
anything, and that history is financial.

### 2.4 Configuration the new engine cannot express

The new engine has **no quorum, one user per stage, and no company
precedence**. Three old shapes therefore break:

| Old shape | What happens now |
|---|---|
| `min_approvals > 1` | not expressible — must become sequential stages |
| several named approvers on one level | not expressible — one user per stage |
| an `ALL` workflow *and* a company-specific one | `AmbiguousWorkflowSelection` at submit |

Also: **self-approval is now always refused** (it was a per-workflow flag
before). If a stage names the person who raises the document, those documents
block. Run the audit in §3.1 — it reports every one of these.

---

## 3. Before the release

### 3.1 Audit the current configuration (read-only)

```bash
python manage.py audit_approval_config
```

Read-only, runs in a `READ ONLY` transaction, and refuses to run unless the
environment is recognised. It reports the incompatible shapes from §2.4, the
open-request backlog, and whether `doc_no` is usable. **Section C must read all
zeros** and the result must say `CONFIGURATION COMPATIBILITY: PASS`.

### 3.2 Drain the approval queue to zero

```sql
-- MUST return 0 before the release. Read-only.
-- NOTE: the old engine's tables live in the `payments` schema too, and
-- `approval_request` carries no document_type of its own - the type is on the
-- workflow the request points at.
SELECT w.document_type, r.status, COUNT(*)
FROM payments.approval_request r
JOIN payments.approval_workflow w ON w.id = r.workflow_id
WHERE r.status IN ('PENDING', 'DRAFT')
  AND w.document_type IN ('PAYMENT', 'DEPOSIT')
GROUP BY w.document_type, r.status;
```

Approve or reject each one **through the application** in the normal way. Do
not clear them with SQL — that skips SAP posting, and an approved payment that
never reached SAP is a book that does not balance.

Freeze new submissions for the drain window, or the count will not converge.

### 3.3 Check the SAP cash parent is set

```bash
python manage.py validate_cash_parent
```

`SAP_CASH_PARENT_ACCOUNT` must be set in the production environment (it is
`1105000`, "CASH IN HAND", in the systems audited). Unset, the cash-account
picker returns nothing and cash receipts cannot be raised. The system check
`payments.W001` / `W002` also flags this offline.

---

## 4. Configure the workflows (before deploying the code)

Configuration is **forward-compatible**: the new tables can be populated while
the old engine is still serving, because nothing reads them until the new code
is live. Do this first so there is no window with no workflow.

Payments runs **two separate business workflows** under **one** module. Never
register a `PAYMENT` or `DEPOSIT` module — the module is `PAYMENTS`, and the two
workflows are told apart by the table their query reads:

```
                         PAYMENTS  (one module)
                              |
              +---------------+---------------+
              |                               |
        RECEIPT workflow                DEPOSIT workflow
        query -> payment_receipt        query -> payment_bank_deposit
        RCP-...                         DEP-...
        receipt stages/approvers        deposit stages/approvers
```

1. The module row is created automatically by `post_migrate`
   (`PAYMENTS` / "Payments"). No manual step.
2. Create **one workflow for receipts and one for deposits**, per routing rule.
   **Do not create both an `ALL` workflow and a company-specific one for the
   same document type** (§2.4), and **never one workflow serving both** — see
   below.
3. Attach the queries, with the `doc_no` alias exactly as in §2.2. The receipt
   workflow's query reads `payments.payment_receipt`; the deposit workflow's
   reads `payments.payment_bank_deposit`.
4. Add one stage per approver, in sequence, one user each. **Receipt and
   deposit approvers are configured independently** — do not assume they are
   the same people.

Approvers also need the matching action key, which is a second, separate gate:
`Payments_Approve` for receipts, `Deposit_Approve` for deposits. A receipt
approver cannot approve a deposit even if a stage names them.

### This configuration is created by an administrator, not by a migration

`workflow_queries.query_text`, workflows, stages and approver assignments are
CONFIGURATION — written on the Workflows page — and the project treats them
that way (see `backdate/migrations/0007_repoint_condition_queries.py`, which
repairs such rows but never creates them). **No migration in this release
creates a workflow, a stage or an approver assignment**, because who may
approve what is a business decision a migration must not invent.

Creating them with raw SQL is also wrong: `validated_at` is stamped by the
engine's own validator, and selection REFUSES a query whose stamp is NULL. A
hand-inserted row would be configuration that silently never routes anything.

`python manage.py audit_approval_config` reports exactly what is missing and
blocks until both workflows exist.

### Routing is enforced, not merely conventional

`payments/workflow_flow.py:_guard_routing` checks the matched query against the
table it reads, so a receipt can only ever be routed by a query selecting from
the receipt table. A workflow whose query reads the wrong table — or both
tables — is refused at submit with a message naming the misconfiguration,
rather than quietly approving a receipt through the deposit chain. The prefixes
`RCP-` / `DEP-` remain a convention; they are no longer the thing keeping the
two workflows apart.

Verify before deploying:

```sql
-- Every active PAYMENTS query MUST project doc_no. Any row returned is a bug.
SELECT w.code, q.name, q.query_text
FROM workflow.workflow_queries q
JOIN workflow.workflows w  ON w.id = q.workflow_id
JOIN workflow.workflow_modules m ON m.id = w.module_id
WHERE m.code = 'PAYMENTS'
  AND w.is_active
  AND q.query_text !~* 'as\s+doc_no';
```

---

## 5. The migration itself

### Is it safe on a live database? Yes — with one caveat to measure.

**`payments/0035` — seven `ADD COLUMN … DEFAULT '' NOT NULL`.**
On PostgreSQL 11+ (production runs 16) a non-volatile `DEFAULT` on `ADD COLUMN`
is **metadata-only — no table rewrite**. The lock is held for microseconds.

The defaults are declared with Django's `db_default`, so there is **no
`DROP DEFAULT`**. That is deliberate: during a rolling deploy the old code is
still `INSERT`ing without these columns, and the database supplies the default
for it. Old and new code can run side by side.

**`payments/0036` — two new tables, one new column, indexes.**
The two flow tables are created empty, so their indexes and constraints are
instant. The one statement that touches an existing table is:

```sql
ALTER TABLE payments.payment_status_history ADD COLUMN stage_id bigint NULL … ;
CREATE INDEX payment_status_history_stage_id_302bcc5a ON payments.payment_status_history (stage_id);
```

Adding a nullable column is metadata-only. **`CREATE INDEX` is not
`CONCURRENTLY`**, so it takes an `ACCESS EXCLUSIVE` lock on
`payments.payment_status_history` for the duration of the build. For reference this
table is 603 rows / 368 kB in TEST — a few milliseconds. **Measure production
before you assume the same:**

```sql
SELECT c.reltuples::bigint AS approx_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'payments' AND c.relname = 'payment_status_history';
```

Under roughly a million rows, build it in the migration and move on. Much
larger, or a busy write window, and you should create it `CONCURRENTLY` outside
the transaction first, then run the migration (it will see the index exists).

Both migrations run **inside a transaction** and roll back cleanly on failure.

### What does NOT happen

- No data is written, moved, back-filled or deleted.
- No row in `approvals_*` is touched.
- No SAP call is made.
- No existing column is altered or dropped.

### Running it

```bash
python manage.py migrate payments 0036
```

Then confirm the module registered:

```sql
SELECT id, code, name FROM workflow.workflow_modules WHERE code = 'PAYMENTS';
```

---

## 6. Release order

Configuration first, then backend, then clients — the approve endpoints move,
so a client released ahead of the backend calls URLs that do not exist yet.

1. Audit and drain (§3) — queue at zero, freeze submissions.
2. Configure workflows, queries and stages (§4).
3. Deploy the backend and run `migrate`.
4. Smoke-test (§7) before lifting the freeze.
5. Lift the freeze.
6. Release web, then mobile.

Until a client is updated it can still *read* payments; only the approve,
reject and cancel actions move.

---

## 7. Smoke test (in production, on one real document)

1. Raise a receipt, verify it, submit it → it reaches the first stage, and the
   stage's user is notified.
2. Sign in as that user → it appears in `GET /api/payments/approvals/queue/`.
3. Approve it → it advances, or completes and posts to SAP.
4. Check the timeline shows `PENDING_APPROVAL` → `APPROVED` with no
   `STATUS_CHANGED` filler row.
5. Repeat once for a bank deposit — deposits and receipts select through the
   same module and are the case `doc_no` exists to separate.

If step 1 fails with *"No workflow is configured"* or `doc_no does not exist`,
stop and fix §2.1 / §2.2. Nothing is damaged; submission is simply refused.

---

## 8. After the release

Leave the old engine installed and untouched. Once payments have run through
the new engine for a full cycle and nothing reads the old tables, the removal
is a separate, reviewed change:

- drop the `('approvals', '0001_initial')` dependency in
  `users/migrations/0027_payment_roles.py` **in the same change** that removes
  the app (the migration already guards the import with `LookupError`);
- remove `approvals` from `INSTALLED_APPS` and `OMS/urls.py`;
- only then delete its tables, with a backup taken first.

---

## 9. Rollback

Before the smoke test passes, rollback is clean:

```bash
python manage.py migrate payments 0034
```

This drops the two empty flow tables and the `stage_id` column. **It also drops
the seven snapshot columns from `0035`** — harmless if no receipt has been
raised on the new code, lossy if one has, because the receiving account chosen
by the user lives in those columns. So:

- **Nothing submitted yet** → roll back the code and the migration together.
- **Documents already in flight on the new engine** → do **not** roll the
  migration back. Roll the code back only if the new columns are left in place,
  or fix forward. Rolling back the schema under live flows strands them exactly
  as §2.3 describes, in the other direction.

---

## 10. Verification queries (all read-only)

```sql
-- Old-engine backlog: must be 0 before release, and stay 0 after.
SELECT w.document_type, r.status, COUNT(*)
FROM payments.approval_request r
JOIN payments.approval_workflow w ON w.id = r.workflow_id
WHERE w.document_type IN ('PAYMENT','DEPOSIT')
GROUP BY w.document_type, r.status;

-- New-engine flows opening after release.
SELECT status, COUNT(*) FROM payments.payment_receipt_flow GROUP BY status;
SELECT status, COUNT(*) FROM payments.payment_bank_deposit_flow GROUP BY status;

-- Documents that say they are in approval but have no flow — MUST be empty.
-- Any row here is a stranded document (§2.3).
SELECT r.id, r.receipt_no, r.status
FROM payments.payment_receipt r
LEFT JOIN payments.payment_receipt_flow f ON f.receipt_id = r.id
WHERE r.status = 'PENDING_APPROVAL' AND f.id IS NULL;

SELECT d.id, d.deposit_no, d.status
FROM payments.payment_bank_deposit d
LEFT JOIN payments.payment_bank_deposit_flow f ON f.deposit_id = d.id
WHERE d.status = 'PENDING_APPROVAL' AND f.id IS NULL;
```

The last two are the single most useful check after release. Run them at the
end of the first day. They should return nothing, ever.

### What these return in TEST today (2026-09-18)

Every query above was run read-only against TEST while writing this document.
They are not illustrative — they already find real problems:

```
3.2  drain check            PAYMENT PENDING 14 | DEPOSIT PENDING 6   <- 20 to drain
4    queries missing doc_no 1ST_APPROVAL_FOR_OIL / "OIL entries"     <- §2.2, broken
5    history table size     603 rows, 368 kB                          <- index build trivial
10   receipt flows          (none yet - migration applied, not in use)
10   stranded receipts      3 rows, all PENDING_APPROVAL
10   stranded deposits      none
     module row             id=54 PAYMENTS "Payments"                 <- auto-registered
```

Two things to take from that. The `doc_no` check works — it caught the one
genuinely broken query without being told where to look. And **TEST already has
three stranded receipts**, so §2.3 is not a theoretical failure mode: it is
what happens when the new code meets documents left in the old engine. In
production that number is whatever §3.2 reports, and every one of them will
stick unless it is drained first.
