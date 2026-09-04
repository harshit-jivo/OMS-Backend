# Payment Verification / Handover — Design and Implementation Plan

**Status:** APPROVED design. Implementation proceeds in phases (§25).
**Goal:** add a verification (handover) gate between payment creation and the existing approval workflow.
**Method:** every claim below is quoted from the current source.

---

## Context

Today a creator can send a payment straight into the approval chain — **nothing stands between
creation and approval**. The business wants a handover step: a second person physically checks the
cash or cheque against what was entered, before any approver sees it.

This adds that gate **without touching** the approval engine, SAP posting, deposits, cancellation
reconciliation or the receipt PDF.

---

## 1. Current flow

```
CREATE (Payments_Create)
   |  POST /api/payments/receipts/
DRAFT
   |  POST /receipts/<pk>/submit/  -> services.submit_receipt
PENDING_APPROVAL
   |  approvals engine, level by level
APPROVED (final rung)
   |  hooks._on_receipt_approved -> transaction.on_commit
POSTING_TO_SAP -> POSTED | PENDING_ERROR | SAP_UNKNOWN
```

## 2. Current architecture

| Concern | Where | Note |
|---|---|---|
| Receipt model | `payments/models.py` `PaymentReceipt` | `TimeStampedModel`, carries `created_by` |
| Status enum | `PaymentReceipt.Status` | 10 values, see §3 |
| Submit gate | `payments/services.py` `submit_receipt` | **the integration point** |
| Approval engine | `approvals/` | generic GFK engine, `document_type='PAYMENT'` |
| SAP posting | `payments/hooks.py` -> `services.post_receipt_to_sap` | fires on final approval via `on_commit` |
| Audit trail | `PaymentStatusHistory` + `services.log_status` | one generic append-only table |
| Permissions | `payments/permissions.py` | 5 string keys in `User.extra_pages`; role confers nothing |
| Object rules | `views._document_permissions` | `can_edit` / `can_decide` |
| Visibility | `views._receipt_queryset` | all companies, no per-user narrowing |

Two properties make this change cheap:

* **`PaymentStatusHistory` is already generic and append-only** — `content_type`/`object_id`, an
  `Action` enum, `changed_by`, `reason`, `actor_kind`, IP. No new audit table is needed.
* **`submit_receipt` is the single choke point.** Every path into approval calls it
  (`services.py:343`), so one guard there cannot be bypassed by another route.

## 3. Current status lifecycle

```
DRAFT · PENDING_APPROVAL · APPROVED · REJECTED · POSTING_TO_SAP
POSTED · PENDING_ERROR · SAP_UNKNOWN · CANCELLED_IN_SAP · CANCELLED
```

`submit_receipt` accepts exactly three — **DRAFT, REJECTED, PENDING_ERROR** — and also refuses when
`sap_doc_entry` is set or an approval is still open.

## 4. New flow

```
CREATE                Payments_Create   many creators
   v
PENDING VERIFICATION  Payments_Verify   many verifiers (NEW)
   v
VERIFIED
   v                  existing submit_receipt
PENDING_APPROVAL -> ... -> POSTED
```

## 5. Step 1 — creation (unchanged)

No change to the create API, form, data model or `Payments_Create`. A receipt is still born `DRAFT`;
it simply becomes visible to verifiers instead of submittable by its creator.

## 6. Step 2 — verification

A queue page of receipts awaiting verification, default window **last 2 days**. The verifier opens
one, checks it against the physical money, optionally edits, then verifies — which submits it into
the existing chain.

## 7. Permission design — new key `Payments_Verify`

The system is a flat list of keys in `extra_pages`, resolved by `permissions.granted_keys()`.

1. append to `ACTION_PERMISSION_KEYS` + `ACTION_PERMISSION_LABELS` (`payments/permissions.py`)
2. add `CanVerifyPayment(_KeyPermission)` beside the existing four
3. add `Payments_Verify` to `ASSIGNABLE_PAGES` (mobile `src/constants/pages.ts`)
4. add its screens to `PAYMENT_ACTION_SCREENS` so menu + route guard follow

**No role map** — there is none; `granted_keys()` reads `extra_pages` alone. The page grants
eligibility; the record stores who actually acted.

Enforced in **both** places: `permission_classes` on every endpoint (authoritative) and
`canAccessScreen` for the menu (affordance only).

## 8. Verification user tracking

Columns `verified_by` / `verified_at` answer "who verified this?" in one read. The **event** also
goes to `PaymentStatusHistory` via `log_status`. Both are wanted: the column for querying and
display, the history row for the timeline.

## 9. Database changes

Audited — **none of these exist today**:

| Field | Type | Why |
|---|---|---|
| `verification_status` | `CharField(20)`, default `PENDING`, indexed | see decision below |
| `verified_by` | `FK(AUTH_USER_MODEL, SET_NULL, null=True)` | who verified |
| `verified_at` | `DateTimeField(null=True)` | when |
| `verification_remarks` | `TextField(blank=True, default='')` | verifier note |

Plus one `Action` value: `VERIFIED`. (`RETURNED` already exists at `models.py:707` for a future
send-back — not used in this phase.)

**No new table, no new audit system, no new Payment model.**

### Decision: separate `verification_status`, NOT new `PaymentStatus` values

Confirmed against the code:

* **`submit_receipt` branches on `status`** against a 3-value whitelist (`services.py:343`) — a new
  main status means editing that *and* every other status branch.
* **`analytics.PENDING_STATUSES`** lists 6 statuses explicitly and `COUNTED_RECEIPT_STATUSES =
  ('POSTED',)`. A new main status would silently drop unverified receipts out of "pending".
* **`views._document_permissions`** derives `can_edit`/`can_decide` from `Status`.
* Mobile `STATUS_LABEL`/`STATUS_COLOR` and the tracking filters enumerate statuses by hand.

A separate field leaves all of that untouched — a receipt stays `DRAFT` until it enters approval, and
verification is a new orthogonal axis. **Lowest regression risk.**

Values: `PENDING` -> `VERIFIED`.

## 10. API changes

| Need | Existing | Change |
|---|---|---|
| Queue list | `GET /receipts/` | add `?verification_status=` — the view already filters status, company, card_code, dates, mine |
| Detail | `GET /receipts/<pk>/` | expose the 4 new fields |
| Edit | `PATCH /receipts/<pk>/` | reuse; extend `can_edit` |
| Submit | `POST /receipts/<pk>/submit/` | **add the guard** (§11) |
| History | `GET /receipts/<pk>/history/` | no change, already generic |

**One new endpoint:** `POST /receipts/<pk>/verify/`.

A dedicated `/verification/` list endpoint is **not** needed — a query param on the existing list is
less surface area and inherits pagination, filters and scoping.

## 11. Integration point — one guard

In `services.submit_receipt`, beside the existing status check:

```
if receipt.verification_status != VERIFIED:
    raise ValidationError(
        'Payment must be verified before it can be submitted for approval.')
```

Server-side and unbypassable. The verify endpoint verifies and calls `submit_receipt` in the **same
transaction**, so verification and submission cannot come apart.
**The approvals engine is not modified at all.**

## 12. Send back — NOT in this phase

Deferred by decision. `Action.RETURNED` already exists for it when the time comes.

## 13. Self-verification — FORBIDDEN (confirmed)

The codebase already takes this position: `approvals` sets `forbid_self_approval=True` and
`_document_permissions` blocks self-approval. Verification exists to put a second pair of eyes on
physical money.

**Decision:** reject when `created_by_id == request.user.id`, with
*"You cannot verify a payment you created."*

**Staffing consequence — check before go-live:** at least two people need `Payments_Verify`, or any
payment raised by the sole verifier can never leave the queue.

## 14. Editing rules

Reuse `PATCH /receipts/<pk>/` and `PaymentReceiptCreateSerializer`, which already revalidates
amount vs methods, allocations vs total, the one-method rule and the cheque CHECK constraint.
**No new validation path.** Extend `can_edit` to include a verifier while `verification_status=PENDING`.

**Material edits reset verification.** If amount, method, allocations, cheque number/bank/date or
UPI reference change after `VERIFIED`, reset to `PENDING` and clear `verified_by`/`verified_at` —
otherwise the record claims someone verified figures they never saw.

## 15. Lifecycle interaction

| Situation | `verification_status` | Rationale |
|---|---|---|
| Created | `PENDING` | enters the queue |
| Verified | `VERIFIED` | submits to approval |
| Rejected by approver | **stays `VERIFIED`** | the approver disputed the decision, not the money |
| `PENDING_ERROR` | **stays `VERIFIED`** | a GL/period problem; the *approver* retries from their queue |
| `SAP_UNKNOWN` | stays `VERIFIED` | reconciliation resolves it |
| `CANCELLED_IN_SAP` | stays `VERIFIED` | historical fact, never rewritten |

**Decision (confirmed):** rejection and SAP failure do NOT return to verification. `submit_receipt`
already accepts `REJECTED` and `PENDING_ERROR` directly, and for `PENDING_ERROR` the approver
retries from their own queue — routing it back would break that existing path.

## 16. Audit trail

Reuse `log_status` exclusively:

```
CREATED               Ramesh
VERIFIED              Priya
SUBMITTED             Priya
APPROVED (Level 1/1)  Manager
SAP_POSTED            SYSTEM   DocEntry 20802
```

No separate `SUBMITTED_FOR_VERIFICATION` row — creation *is* entry into the queue.

## 17. Notifications — none in this phase

The `notifications/` framework has a registry and models but **no delivery yet**; Orders still owns
live notifications. Verifiers work a queue, as approvers do today. Do not build a parallel notifier.

## 18. Concurrency

Reuse the pattern from `sap_poster.post_document`:

```
with transaction.atomic():
    fresh = PaymentReceipt.objects.select_for_update().get(pk=pk)
    if fresh.verification_status == VERIFIED:
        raise ValidationError('This payment has already been verified by ...')
```

Two verifiers at once: one wins, the other gets a clear message. **No new locking framework** — this
is how duplicate SAP posting is already prevented.

## 19. Security

| Control | Mechanism |
|---|---|
| Authentication | existing JWT |
| Page permission | `Payments_Verify` in `extra_pages` |
| API permission | `CanVerifyPayment` on every verification endpoint |
| Object-level | verify only when `PENDING` and not already in SAP |
| Company isolation | existing `_receipt_queryset` + `?company=` |
| Separation of duties | creator != verifier (§13) |
| Bypass into approval | the `submit_receipt` guard (§11) |
| Duplicate verification | row lock (§18) |
| Audit | `log_status` with `changed_by` + IP |

URL-guessing is covered: the endpoint checks the permission class *and* the object state.

## 20. Performance

Reuse `StandardPagination` and the existing `select_related`/`prefetch_related` in
`_receipt_queryset` (already prevents N+1 on methods, allocations, attachments).
Add one index: `(verification_status, -created_at)` matching the queue filter+sort.

## 21. Mobile plan — no web work

Payments are **mobile-only**; the web has no receipt UI (`src/pages/Payments/` holds only the
dashboard, analytics and approval config).

| Item | Reuse |
|---|---|
| Queue screen | `PaymentTrackingScreen` pattern — filters, pagination, status pills |
| Detail/edit | `receive-payment.tsx` in edit mode (`?receiptId=`), already serves approvers |
| Verify action | `ApprovalBottomBar` + `ApproveDialog` |
| Navigation | drawer entry gated by `Payments_Verify` |
| Back behaviour | pass `from` (the established convention) |

## 22. Test plan

**Permission** — without the key: 403; with it: 200.
**Queue** — new receipt appears; verified one leaves; 2-day default; company filter honoured.
**Verify** — sets the three fields, writes one `VERIFIED` history row, submits to approval.
**Self-verification** — creator verifying own receipt is rejected.
**Guard** — unverified submit raises; verified proceeds.
**Edit** — verifier edit revalidates; material edit after verification resets to `PENDING`.
**Duplicate** — second verify raises.
**Concurrency** — two simultaneous verifies: exactly one succeeds.
**Lifecycle** — REJECTED / PENDING_ERROR / SAP_UNKNOWN / CANCELLED_IN_SAP keep `VERIFIED`.
**Regression** — approval ladder, SAP payloads and deposit flow byte-identical; existing suites pass.

## 23. Migration plan

One additive migration:

* all four columns nullable/defaulted — no table rewrite
* **backfill** existing receipts to `verification_status='VERIFIED'`, `verified_by=NULL` — nothing in
  flight is blocked by a gate that did not exist when it was raised; NULL honestly records
  "verified before the feature existed"
* POSTED / PENDING_ERROR / CANCELLED_IN_SAP keep every SAP identifier and amount untouched

Verify with `sqlmigrate` (expect only `ADD COLUMN` / `CREATE INDEX`) before applying.

## 24. Rollback / failure safety

| Failure | Behaviour |
|---|---|
| Verification save fails | one transaction — nothing written |
| Approval submit fails after verify | same transaction — both roll back |
| Concurrent update | row lock; loser gets a message |
| Verifier loses network | no partial state; retry |
| Edit after verification | resets to `PENDING` (§14) |
| SAP posting fails later | unchanged `PENDING_ERROR` path |

Feature rollback: revert the code; the columns stay (harmless, defaulted `VERIFIED`).

## 25. Implementation phases

| # | Phase | Deliverable |
|---|---|---|
| 1 | Model + migration | 4 fields, `VERIFIED` action, index, backfill |
| 2 | Permission | `Payments_Verify` + `CanVerifyPayment` |
| 3 | Serializer | expose fields; extend `can_edit`; material-edit reset |
| 4 | Guard | the `submit_receipt` check |
| 5 | Verify API | `POST /verify/` with lock + `log_status` |
| 6 | Queue filter | `?verification_status=` on the list view |
| 7 | Mobile queue | list screen + drawer entry |
| 8 | Mobile detail | verify action reusing the edit form |
| 9 | Tests | §22 |

Phases 1–6 are backend-only and shippable before any UI exists. Tests run after each phase.

## 26. Files expected to change

`payments/models.py` · `permissions.py` · `serializers.py` · `services.py` · `views.py` · `urls.py` ·
one migration · new tests.
Mobile: `src/constants/pages.ts` · a queue screen · a route file · drawer entry.

## 27. Files explicitly NOT changing

`sap_poster.py` · `sap_payloads.py` · `sap_client.py` · `hana_queries.py` · `hooks.py` · the whole
`approvals/` app · deposit logic · the receipt PDF · `notifications/` · the web frontend.

## 28. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | In-flight receipts blocked by the new gate | backfill to `VERIFIED` |
| R2 | Verification becomes a bottleneck | multiple key holders; permission-based |
| R3 | Self-verification forbidden with only one verifier | confirm staffing (§13) |
| R4 | Material edit after verification | reset rule (§14) |
| R5 | Analytics drift | orthogonal field; `PENDING_STATUSES` untouched |
| R6 | Two audit sources | only `log_status` writes history; columns are a projection |

## 29. Decisions taken and questions remaining

**Confirmed:**

1. **Self-verification — FORBIDDEN** (§13). Requires 2+ holders of `Payments_Verify`.
2. **Rework path — stays `VERIFIED`** (§15).
3. **Send-back — deferred** to a later phase (§12).

**Still open (do not block phases 1–6):**

4. **Queue scope** — should a verifier see every company they can access, or only their own?
   Today `_receipt_queryset` does no per-user narrowing.
5. **Dashboard** — split "pending" into awaiting-verification vs awaiting-approval, or keep one
   bucket? `PENDING_STATUSES` lumps them together, which stays correct either way.

## 30. Final architecture

```
              PAYMENT CREATION
                     |   Payments_Create - many creators
                     v
              PaymentReceipt          status=DRAFT
                                      verification_status=PENDING
                     |
                     v
           PENDING VERIFICATION  <-- queue, 2-day default
                     |   Payments_Verify - many verifiers
                     |   view / edit / verify
                     v
                 VERIFIED              verified_by / verified_at
                     |
                     |   services.submit_receipt   <-- THE GUARD
                     v
        EXISTING APPROVAL WORKFLOW     status=PENDING_APPROVAL
              Level 1 / Level 2 / ...  (engine unchanged)
                     |
                     v
               FINAL APPROVAL          status=APPROVED
                     |   hooks -> on_commit
                     v
                 SAP POSTING           (unchanged)
                     |
                     v
                   POSTED
```

---

## 31. Attachment path persistence — store the exact path in OMS

**Status:** planned, not implemented. Independent of the verification work above;
either can ship first.

### 31.1 Requirement

Store the **exact** path of every attachment in the OMS database. SAP does not
hold OMS attachment paths — OMS is the sole authority for where its files live.

### 31.2 Current behaviour

`payment_attachment` (`attachments/models.py`) stores metadata only. There is
deliberately no `FileField` and **no path column**; the location is *derived* at
every read:

```
storage.directory_for(attachment_type)  +  attachment.stored_name
```

`directory_for()` returns one of two settings, chosen by type:

| Attachment types | Setting |
|---|---|
| `CHEQUE_IMAGE`, `UPI_SCREENSHOT` | `PAYMENTS_IMAGES` |
| `DEPOSIT_SLIP`, `DEPOSIT_RECEIPT` | `DEPOSIT_PAYMENTS_IMAGES` |

Files are written flat under a generated UUID name (`save_upload`), via
`smbclient` when the target is a UNC path and `PAYMENTS_SMB_USERNAME` is set,
otherwise plain filesystem I/O. Both branches write to a temp name and rename,
so a reader never sees a half-written file.

**Live state:** 13 rows — 5 `CHEQUE_IMAGE`, 3 `UPI_SCREENSHOT`, 3 `DEPOSIT_SLIP`,
2 `DEPOSIT_RECEIPT`.

### 31.3 Why derivation is a defect

If `PAYMENTS_IMAGES` or `DEPOSIT_PAYMENTS_IMAGES` ever changes, **every existing
row silently breaks.** `open_stored()` will look for old files in the new
directory and raise `FileNotFoundError`; the download endpoint turns that into a
404. Nothing in the database records where the file was actually written, so the
break is unrecoverable without manually correlating filenames on disk.

This is not hypothetical. `.env` currently points both settings at local disk
inside the repo:

```
PAYMENTS_IMAGES=C:\Users\Jivo\Desktop\OMS\OMS Backend\images\Receive_Payments
DEPOSIT_PAYMENTS_IMAGES=C:\Users\Jivo\Desktop\OMS\OMS Backend\images\Deposit_Payments
```

The intended production target is the share (`\JIVO-APP\Payments\...`). That
migration **is** the directory change that breaks derivation. Storing the path
must land before it, not after.

### 31.4 Design

Add two columns to `payment_attachment`:

| Column | Type | Meaning |
|---|---|---|
| `stored_path` | `CharField(max_length=500)` | Exact absolute path written, e.g. `\JIVO-APP\Payments\Receive_Payments\9f0c2dbe.jpg` |
| `stored_dir` | `CharField(max_length=400)` | The directory alone, as resolved at write time |

`stored_dir` is redundant with `stored_path` but makes "which files are on the
old share?" a plain indexed query during any future move, instead of a `LIKE`
scan. Both are populated once at write time and never rewritten.

`stored_name` stays. It remains the unique key and the basename.

### 31.5 Security — the path is authoritative for reads, never trusted for I/O

A path read from the database and passed to `open()` is a path-traversal sink.
A row altered through the admin, a bad migration, or a future bulk update could
point at any file the process can read.

The rule: **use the stored path to locate, re-validate before opening.**

```
open_stored(attachment):
    path = attachment.stored_path
    basename = os.path.basename(path)

    # 1. basename must match the UUID name we generated
    if basename != attachment.stored_name: reject

    # 2. the directory must be one this deployment is configured to serve
    if dirname(path) not in {PAYMENTS_IMAGES, DEPOSIT_PAYMENTS_IMAGES,
                             *PAYMENTS_LEGACY_DIRS}: reject
```

A path outside the allow-list is refused even though the row says otherwise.
`PAYMENTS_LEGACY_DIRS` (new setting, comma-separated, default empty) is what
makes an old share readable after a move without rewriting rows.

The serializer is unchanged: **`stored_path` is never exposed to a client.**
`AttachmentSerializer` already documents that the network location is internal;
the new fields stay out of `fields`. Downloads keep going through
`/api/payments/attachments/<id>/download/` with `can_view_attachment`.

### 31.6 Backfill

The 13 existing rows have no path. A data migration derives it exactly as
`open_stored()` does today, so the value written is provably the file's current
location:

```
for row in Attachment.objects.all():
    directory = directory_for(row.attachment_type)   # current settings
    row.stored_path = join(directory, row.stored_name)
    row.stored_dir  = directory
```

This is correct **only while the settings still point where the files were
written** — i.e. it must run before the share migration, per §31.3. The
migration must not fail if a setting is blank; it skips those rows and logs
them, leaving `stored_path` empty for a later manual pass.

### 31.7 Fallback for empty `stored_path`

Rows with a blank path (backfill skipped, or written by an older deploy) fall
back to today's derivation. This keeps the change strictly additive — no read
path regresses. Once the backfill is confirmed complete the fallback can be
removed, but not in this phase.

### 31.8 Changes required

| File | Change |
|---|---|
| `attachments/models.py` | add `stored_path`, `stored_dir`; index on `stored_dir` |
| `attachments/migrations/0002_…` | `AddField` x2 + index |
| `attachments/migrations/0003_…` | data migration, backfill the 13 rows |
| `attachments/storage.py` | `save_upload` returns `(name, path, dir)`; `open_stored`/`delete_stored` take the attachment and re-validate; add `PAYMENTS_LEGACY_DIRS` handling |
| `attachments/services.py` | `attach()` persists the new columns; `detach()` passes the attachment |
| `attachments/views.py` | pass the attachment to `open_stored` |
| `OMS/settings.py` | add `PAYMENTS_LEGACY_DIRS` |

**Not changing:** `attachments/serializers.py` (path stays unexposed),
`attachments/admin.py`, the download URL, `can_view_attachment`, the
validation rules (jpg/jpeg/png/pdf, 5 MB, magic-byte check), and every
payments/deposit/SAP module.

### 31.9 Phases

1. Model + schema migration. `makemigrations --check`, inspect SQL.
2. Write path — `save_upload` returns the triple, `attach()` stores it. New
   uploads carry a path; old rows still work via §31.7.
3. Read path — `open_stored`/`delete_stored` prefer `stored_path`, re-validate,
   fall back when blank.
4. Backfill migration for the 13 rows.
5. `PAYMENTS_LEGACY_DIRS` + the settings documentation.

Tests after each phase.

### 31.10 Test plan

- A new upload persists `stored_path` and `stored_dir`, and the file is at that path.
- Download succeeds for a row with a stored path.
- Download succeeds for a row with a blank path (fallback).
- A row whose `stored_path` basename ≠ `stored_name` is **refused**.
- A row whose `stored_path` directory is outside the allow-list is **refused**.
- A traversal payload (`..\..\..\windows\win.ini`) is **refused**.
- After the backfill, all 13 rows resolve and download.
- With the directory setting changed and the old value in `PAYMENTS_LEGACY_DIRS`,
  old rows still download and new uploads land in the new directory.
- `stored_path` appears in no API response.

### 31.11 Rollback

Phases 1–3 are additive; the fallback means reverting the code leaves every row
readable. The backfill only fills previously-empty columns and is safe to re-run.
No file is moved, renamed, or deleted at any point.

### 31.12 Open question

**Should the share move to `\JIVO-APP\Payments\...` happen in this phase or
separately?** The plan above assumes *separately, afterwards* — path storage
lands first precisely so the move is survivable. Doing both at once would mean
backfilling paths that are already wrong.
