# Document Tracker — System Reference

*Last verified: 2026-08-11*

The Document Tracker follows a purchase invoice from the moment it reaches the
head office until it is paid, recording **who held it, for how long, and what
they decided** at every desk. It replaces the office's Excel register, and the
Excel export still reproduces that sheet column-for-column.

It is a **self-contained Django app** (`tracker/`). It owns all of its tables and
holds **no foreign keys into any other OMS model** — the only shared dependency
is the project user table, used for login, attribution and per-stage permissions.
It reads SAP and JSAP, but never writes to either.

**Contents**
1. [Data model](#1-data-model)
2. [The stage flow](#2-the-stage-flow)
3. [Dispositions — what a desk can decide](#3-dispositions--what-a-desk-can-decide)
4. [Money: debit, hold and the payment desk](#4-money-debit-hold-and-the-payment-desk)
5. [SAP integration](#5-sap-integration)
6. [JSAP integration (budget approval)](#6-jsap-integration-budget-approval)
7. [Roles and permissions](#7-roles-and-permissions)
8. [API reference](#8-api-reference)
9. [Stuck alerts and scheduled jobs](#9-stuck-alerts-and-scheduled-jobs)
10. [Reports and the Excel export](#10-reports-and-the-excel-export)
11. [Frontend pages](#11-frontend-pages)
12. [Setup and configuration](#12-setup-and-configuration)
13. [Troubleshooting](#13-troubleshooting)
14. [Design decisions and gotchas](#14-design-decisions-and-gotchas)

---

## 1. Data model

`tracker/models.py`. Every table is prefixed `tracker_`.

### The heart of it

```
Invoice --current_stage--> Stage
   |
   +---< StageEvent   (one row per stage VISIT — immutable history)
   |
   +---- PaymentDetail (1:1, terminal stage only)
```

**Every timeline figure in the system is derived from `StageEvent` plus the live
`Invoice.current_stage_entered_at` pointer.** No date is ever typed by hand after
creation. That is the core design commitment — it is what makes "days at this
stage", ageing, bottleneck reports and the stuck alert all trustworthy.

### Invoice (`tracker_invoice`)

| Group | Fields |
|---|---|
| Identity | `invoice_number` (unique, trimmed on save), `invoice_date`, `effective_month`, `party_name`, `party_code`, `party_gstin` |
| Money | `taxable_value`, `gst_type`, `gst_rate`, `additional_charge_type`, `additional_charge_amount`, `invoice_value` (derived), `debit_amount`, `hold_amount` |
| Classification | `category`, `unit`, `branch`, `mode` |
| Flow state | `current_stage`, `current_stage_entered_at`, `status`, `is_locked`, `rejection_pending` |
| Audit | `created_by`, `created_at` (= "Head Office In"), `updated_at` |
| Soft delete | `is_deleted`, `deleted_at`, `deleted_by` |

Derived, never stored by the client:

- `gst_amount` = `taxable_value × rate%`
- `invoice_value` = `taxable_value + gst_amount + additional_charge_amount` — recomputed on **every** save, so it cannot drift
- `net_invoice_value` = `invoice_value − debit_amount` — the base every stage after Pre-Audit works from

Two managers: **`objects` excludes soft-deleted rows** (every read path — queue,
reports, alerts) and `all_objects` includes them (restore, and the uniqueness
check, since a deleted invoice still reserves its number).

- `effective_month` is the accounting period, stored as the **first day of the
  month**. Mandatory; captured as a month picker.
- `invoice_date` cannot be in the future (serializer-enforced).
- `party_code` is the SAP `CardCode`, filled when the vendor is picked from the
  SAP dropdown. It is optional — free-text `party_name` is allowed for parties
  not in SAP — but **without it the SAP and JSAP links cannot resolve** (§5).

### Stage (`tracker_stage`)

Admin-editable, so the flow can be reordered or retuned without a code change.

| Field | Meaning |
|---|---|
| `code` | Stable key (`entry`, `pre_audit`, …). **Code is what logic keys on — never the name.** |
| `order` | Position in the flow. **Unique** — see the reordering trap in §14. |
| `threshold_days` | Dwell time after which the stuck alert fires |
| `status_choices` | Stage-specific dispositions, e.g. `['OK','HOLD','DEBIT','RETURN']` |
| `requires_status` | Whether a disposition is mandatory here |
| `can_return` | May this desk bounce an invoice back? |
| `is_terminal` | Nothing advances past it (Payment) |

### StageEvent (`tracker_stage_event`)

Append-only. One row per stage **visit**: opened `RECEIVE` on arrival, closed
`ADVANCE`/`RETURN` on disposition, stamping `exited_at` and `days_spent`.

An invoice that bounces has **multiple visits to the same stage**, each timed
independently. Carries `stage_status`, `hold_type`, `amount`, `receiving_note`
(on-time vs after 6 PM), `remarks` and `acted_by`.

> `acted_by` is **NULL** when the system acted rather than a person — currently
> only the JSAP sync (§6).

### Supporting tables

| Model | Purpose |
|---|---|
| `PaymentDetail` | 1:1 with Invoice. Percentages, derived amounts, open balance, PAID/OPEN |
| `UserStageAccess` | Which stages a user may see and act on |
| `StuckAlert` | A raised flag, keyed to a specific stage **visit** via `stage_entered_at` |
| `AlertNotification` | One row per (invoice, stage, user) email actually sent |
| `Category` / `Unit` / `Branch` / `InvoiceMode` / `GstType` / `GstRate` | Admin-managed dropdowns, concrete tables so filters and reports stay clean |
| `TransporterPayment`, `CashVoucher` | Standalone side registers mirroring existing sheets — not part of the invoice flow |

---

## 2. The stage flow

Nine desks (as of the 2026-08-11 Transport Approval branch):

| # | Code | Name | Statuses | Can return | Threshold |
|---|---|---|---|---|---|
| 1 | `entry` | Head Office In | — | no | 2 d |
| 2 | `bilty_grpo` | Bilty / GRPO | — | yes | 3 d |
| 3 | `pre_audit` | Pre-Audit | OK · HOLD · DEBIT · RETURN | yes | 3 d |
| 4 | `transport_approval` | Transport Approval | APPROVED · REJECTED | yes | 2 d |
| 5 | `data_entry` | Data Entry | — | yes | 2 d |
| 6 | `sap_approval` | SAP Approval | APPROVED · REJECTED | yes | 3 d |
| 7 | `jsap_approval` | JSAP Approval | APPROVED · REJECTED | yes | 3 d |
| 8 | `save_in_sap` | Save in SAP | — | yes | 2 d |
| 9 | `payment` | Payment | — | no | 5 d (terminal) |

### Conditional routing

**Bilty/GRPO is Transport-only.** `TRANSPORT_ONLY_STAGE_CODES` in
`services.py`; a non-Transport invoice goes Entry → Pre-Audit directly.

Routing is computed per invoice by `stage_route(invoice)`, and advance/return
move along **that** route — so "previous stage" for a non-Transport invoice at
Pre-Audit is Entry, not Bilty. Add a conditional stage by extending this set.

### The Transport Approval branch

> **Transport Approval is a branch, not a step.** It is excluded from
> `stage_route()` entirely (`BRANCH_STAGE_CODES`) and routed explicitly by
> `_branch_neighbour()`.

```
                      +--> Transport Approval --+
                      |   APPROVED / REJECTED   |
                      |                         v
Bilty/GRPO --> Pre-Audit <-----------------------+ --> Data Entry
```

A **Transport** invoice leaving Pre-Audit detours to the approval desk. Both
verdicts hand it straight **back to Pre-Audit** — APPROVED as an ADVANCE (which
is what marks the visit approved), REJECTED as a RETURN carrying the reason,
landing it in Pre-Audit's Returned tab. Pre-Audit then advances it to Data Entry.

Because the branch is off the line, "previous stage" is unaffected: a return
from Data Entry still goes to Pre-Audit, and a return from Pre-Audit still goes
to Bilty/GRPO.

**Approval is per visit, not per invoice.** `transport_approved_this_visit()`
matches the approval's `exited_at` against `Invoice.current_stage_entered_at` —
the two are the same instant, because closing the approval visit is what opens
the Pre-Audit one. So **every fresh arrival at Pre-Audit needs a fresh
approval**: an invoice bounced back from Data Entry goes round again. A full
hold at Pre-Audit does *not* reset it (the invoice never left, so the visit
stands).

`services.next_stage(invoice)` answers "where would Advance send this?" and is
surfaced on every queue row as `next_stage_code` / `next_stage_name`, so the
Pre-Audit desk can see which of the two the button will do.

Non-Transport invoices never see the desk, and if it is deactivated in Admin
the branch simply disappears — Pre-Audit goes straight to Data Entry again.

### Locking

Advancing out of `entry` sets `is_locked = True` and the entry desk can no
longer edit. Returning an invoice **all the way back** to `entry` unlocks it
again for correction before re-submission.

---

## 3. Dispositions — what a desk can decide

All movement goes through one entry point: `services.apply_action()`. Bulk
actions (`apply_bulk`) call it per invoice, so no rule can be bypassed in bulk.

| Status | Effect |
|---|---|
| `OK` / none | Advance |
| `HOLD` + `FULL` | **Stays put.** Annotated with an immutable note; the dwell clock keeps running |
| `HOLD` + `PARTIAL` | **Advances**, withholding `amount` into `Invoice.hold_amount` |
| `DEBIT` | **Advances**, adding `amount` to `Invoice.debit_amount` permanently |
| `RETURN` | Back one stage along the route |
| `REJECTED` **with** remarks | Back one stage |
| `REJECTED` **without** remarks | **Parked** — `rejection_pending = True`, stays put, shows in the Rejected tab until remarks are supplied |

### The rules that are enforced server-side

- **Remarks are mandatory** for RETURN and for HOLD / DEBIT / RETURN / REJECTED
  — *except* a pending rejection, which exists precisely to be reason-less for now.
- **A debit needs an amount.** A partial hold needs one too, except for RM-PM
  style categories (`NO_HOLD_AMOUNT_CATEGORIES`).
- A stage with `can_return = False` cannot return; a terminal stage cannot advance.
- Only a user mapped to the invoice's current stage may act (superusers, and the
  creator at the entry stage, excepted).
- An explicit `action: "RETURN"` overrides an advance-looking status.

Amounts **accumulate**: an invoice debited twice carries the sum.

---

## 4. Money: debit, hold and the payment desk

The trickiest logic in the system. `services.compute_payment()`.

### Inputs the user gives

Only three: **discount %**, **TDS %**, **paid amount** (plus a *release hold*
checkbox). Every amount, the open balance and the status are derived
server-side; the frontend mirrors the same maths for live preview but the
server is authoritative.

### The bases — deliberately different

| Figure | Base |
|---|---|
| Discount amount | **Net invoice value** (`invoice_value − debit`) — *including* any held-back portion |
| TDS amount | **Taxable value** (pre-GST, pre-additional-charge) |

Discount is computed on the value **including** the hold, so releasing or
withholding a hold never changes the discount.

### Two different totals

This is the part that trips people up:

- **`net_payable`** — what may be paid *this round*. Subtracts the hold **unless
  the release checkbox is ticked**. This is the **cap** on the paid amount.
- **`total_owed`** — the full obligation, which **always includes the hold**.
  The **open balance is measured against this**.

So a withheld hold keeps the invoice **open** even after this round's payable is
fully paid — the balance never drops below the hold until it is released and
paid. That is the intended behaviour, per the requirement *"in the open invoice
take into account hold amount as well, whether or not the checkbox is ticked."*

### Outcome

- `open_balance == 0` → PAID → invoice `COMPLETED`, leaves the queue
- `open_balance > 0` → OPEN → stays at the payment desk in the **Partial** tab
- Lowering a paid amount on a completed invoice **re-opens** it

Paid amount is rejected if negative or above `net_payable`.

---

## 5. SAP integration

`tracker/sap.py`. Read-only, direct HANA (not the Service Layer — faster).

### Vendor dropdown

`fetch_vendors()` reads `OCRD` for **both** suppliers (`CardType='S'`) and
customers (`'C'`), cached 10 minutes. GSTIN comes from `OCRD.LicTradNum`,
falling back to any non-empty `CRD1.GSTRegnNo`.

### How a tracker invoice identifies its SAP document

> **The key is `TRIM(NumAtCard)` + `CardCode`.**

A tracker invoice is a *purchase* document, so it lives in the A/P tables, and
the vendor's own invoice number is stored by SAP in **`NumAtCard`** ("Vendor
Ref. No.").

| Table | Object type | What |
|---|---|---|
| `OPCH` | 18 | A/P Invoice (posted) |
| `ORPC` | 19 | A/P Credit Memo (posted) |
| `ODRF` | 18/19 | **Draft** — what JSAP approves, before posting |

**`NumAtCard` is not a key on its own.** `'21'` matches five unrelated vendors'
documents in the Oil company alone. Hence `find_sap_documents()` defaults to
`require_party=True` and returns **nothing** when `party_code` is blank, rather
than a confident wrong answer. Pass `require_party=False` only for a diagnostic
"what else carries this number?" search.

Even with the vendor scoped, a vendor may reuse a number across years, so the
finders return a **list**; the resolvers pick the newest live document.

### Which company DB

`schema_for_invoice(invoice)` — **branch wins over unit**:

| Tracker row | Company DB |
|---|---|
| branch = Mart | `JIVO_MART_HANADB` |
| unit = Beverage | `JIVO_BEVERAGES_HANADB` |
| otherwise | `JIVO_OIL_HANADB` |

`resolve_sap_document()` searches the invoice's own company first, then the
other two, because the unit/branch on the tracker row is not always where the
document was booked. `resolve_draft_document()` **deliberately does not** — the
draft DocEntry is fed to JSAP, whose DocEntry values collide across companies,
so a cross-company guess would attach the wrong approval (§6).

---

## 6. JSAP integration (budget approval)

`tracker/jsap.py`. Read-only against SQL Server `jsaplive3`. **JSAP owns the
decision; the tracker only mirrors it.** Nothing is ever written back.

### The chain

```
tracker Invoice
  --TRIM(NumAtCard) + CardCode-->  ODRF  (draft, in the invoice's company DB)
       |  ODRF.DocEntry
       v
  bud.jsDocEntry            status A / P / R   (Approved / Pending / Rejected)
       |  id
       v
  bud.jsBudgetStatusWorkflow    description = the approver's reason on rejection
```

> **JSAP tracks the SAP *draft*, not the posted invoice.**
> `bud.jsDocEntry.docEntry` is an **`ODRF.DocEntry`** — budget approval happens
> *before* posting. Verified: JSAP docEntry 53119 → ODRF 53119
> (`NumAtCard='JUNE 26/83265'`, `VENDA000987`), the same document that later
> posted as `OPCH` 48649. Joining `jsDocEntry` to `OPCH` finds nothing and is
> the single most likely mistake here.

### Two traps in this database

1. **Use the `bud` schema, not `dbo`.** `dbo.jsBudgetTable` is a frozen archive
   that stops at **2025-03-26**. `bud.jsBudgetTable` is live.
2. **Draft DocEntry collides across companies** (27 `jsDocEntry` rows map to more
   than one branch) and `jsDocEntry` has **no company column**. Every lookup is
   therefore cross-checked against `bud.jsBudgetTable.Branch`. Without that
   guard an OIL draft can pick up a BEVERAGE approval.

`jsBudgetTable.Branch` has only **OIL** and **BEVERAGE** (plus one stray
`BEVERAGES`). **Mart is not budget-approved in JSAP at all** and is deliberately
not mapped.

### What the desk does

`services.sync_jsap()` / `sync_jsap_all()`:

| JSAP says | Tracker does |
|---|---|
| Approved | Advance to Save in SAP |
| Rejected | **Return to SAP Approval, carrying JSAP's own `description`** as the remarks |
| Pending | Leave it; the desk shows why |
| Mart (`not_in_jsap`) | Pass straight through — it would otherwise sit forever |
| Manually rejected here | **Stand down** — see below |

### Manual override

The JSAP desk also has ordinary APPROVED / REJECTED controls, for an invoice
JSAP never received or a decision that must be made by hand.

**A human decision outranks the mirror.** If someone rejected an invoice here
and is still writing the reason (`rejection_pending`), `sync_jsap` skips it and
reports `reason: 'rejection_pending'`. Without that guard the next sweep would
see "JSAP says approved" and silently advance an invoice someone had
deliberately parked.

### When a status is unavailable

`status_for_invoice()` never throws. `available: false` is a normal answer, with
a typed `reason` the UI renders as a badge:

| `reason` | Meaning |
|---|---|
| `not_in_jsap` | Mart — JSAP does not budget-approve it |
| `no_party_code` | No SAP vendor picked, so the document cannot be identified |
| `no_draft` | No SAP draft matches this invoice number + vendor |
| `not_submitted` | The draft exists but has not reached JSAP yet |
| `not_configured` | `JSAP_DB_HOST` / `JSAP_DB_NAME` not set |
| `rejection_pending` | Rejected by hand here; the sync is standing down |

---

## 7. Roles and permissions

Two independent layers. Both must pass.

### Layer 1 — page access (role-driven)

`tracker/permissions.py`, decided in **one** place: `tracker_pages_for(user)`.
To change who sees what, change the user's **role**, never page code.

| Role | Pages |
|---|---|
| `tracker_admin` | Entry · Queue · Alerts · Reports · Admin · All-Invoices |
| `tracker_entry` | Entry · Queue |
| `tracker_user` | Queue |

Non-tracker OMS users see none of them. The page keys must match the frontend
config in `src/config/pageAccess.ts`.

### Layer 2 — stage access (per user)

`UserStageAccess` decides which desks a user works. It drives **both** the queue
they see and server-side authorisation of every action. Superusers get all stages.

### The shared entry desk

The head office / entry stage is **shared**: any entry-desk user sees and edits
every invoice sitting at `entry`, regardless of who created it.

### Visibility

`_scoped_queryset()` — a user sees invoices they created, **or** parked at a
stage they are mapped to, **or** (entry-desk users) anything at `entry`.

### Deletion

Soft delete only. Tracker admins may delete up to **stage order ≤ 7**
(`DELETE_ADMIN_MAX_ORDER`, i.e. through JSAP Approval) — nothing at Save-in-SAP
or Payment. Entry users may delete only an unlocked invoice still at the entry
desk. **The constant tracks the stage numbering**: inserting a stage shifts it.

---

## 8. API reference

All under `/api/tracker/`.

### Day-to-day

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `lookups/` | user | All dropdowns + stages in one call |
| GET | `vendors/` | entry | SAP vendor list (`?refresh=1` to bust cache) |
| GET/POST | `invoices/` | entry | List (filtered) / create |
| GET/PATCH/DELETE | `invoices/<id>/` | user | Detail / edit (entry only, unlocked) / soft-delete |
| PATCH | `invoices/<id>/payment/` | user | Record a payment |
| GET | `invoices/<id>/jsap/` | user | JSAP budget verdict |
| POST | `jsap/sync/` | user | Pull decisions from JSAP (`{invoice_id}` or whole desk) |
| GET | `my-queue/` | user | The actionable inbox + stage tabs with counts |
| GET | `stage-advanced/?stage=` | user | Read-only history of what left a desk |
| GET | `stage-decisions/?stage=&decision=` | user | A desk's decision log — `OK` · `HOLD` · `DEBIT` · `APPROVED` · `REJECTED` · `RETURN` · `TRANSPORT_APPROVAL` (comma-separated; omit for all). `&include_resolved=1` keeps send-backs the invoice has since come back from |
| GET | `stage-export/?stage=&tab=&ids=` | user | One queue tab as Excel, in the All-Invoices register layout |
| POST | `actions/bulk/` | user | Apply one action to many invoices |
| GET | `reports/` | reports | Turnaround analytics |
| GET | `alerts/` | alerts | Active stuck alerts |
| GET | `all-invoices/` | admin | Master list |
| GET | `all-invoices/export/` | admin | Excel register |

**Filters** (list + export): `party`, `invoice_number`, `effective_month`
(`YYYY-MM`), `category`, `branch`, `unit`, `status`, `stage`, `overdue`.

### Admin / config — all `IsTrackerAdmin`

| Method | Path |
|---|---|
| GET/POST | `admin/stages/` |
| PATCH/DELETE | `admin/stages/<id>/` |
| GET/POST | `admin/lookups/<kind>/` (`categories`, `units`, `branches`, `modes`, `gst_types`, `gst_rates`) |
| PATCH/DELETE | `admin/lookups/<kind>/<id>/` |
| GET | `admin/users/` · PUT `admin/users/<id>/stages/` |
| GET/POST | `admin/tracker-users/` · DELETE `admin/tracker-users/<id>/` |

---

## 9. Stuck alerts and scheduled jobs

An invoice sitting at a stage past `threshold_days` raises a `StuckAlert`, keyed
to that specific **visit** (`stage_entered_at`) — so an invoice that bounces back
later raises a *fresh* alert rather than reusing the stale one.

**Pre-Audit FULL holds are skipped**: a full hold parks the invoice on purpose,
so it must not generate a "stuck" email.

A re-notify cooldown (`TRACKER_ALERT_EMAIL_COOLDOWN_HOURS`, default 24) stops
the sweep from mailing the same invoice every run. `AlertNotification` is the
ledger of who was actually mailed.

### The three commands

| Command | Does | Batch file |
|---|---|---|
| `scan_stuck_alerts` | Raise / resolve stuck alerts | `run_stuck_alerts.bat` |
| `email_stuck_alerts` | Email each stage's users (`--dry-run`, `--force`, `--cooldown-hours`) | `run_email_stuck_alerts.bat` |
| `sync_jsap` | Mirror JSAP decisions (`--dry-run`, `--limit`) | `run_jsap_sync.bat` |

All three are **idempotent** — safe to run on a schedule. Register via Task
Scheduler:

```
schtasks /create /tn "Tracker Stuck Alerts" /tr "...\run_stuck_alerts.bat"  /sc minute /mo 30
schtasks /create /tn "Tracker JSAP Sync"    /tr "...\run_jsap_sync.bat"     /sc minute /mo 10
```

> **Without the JSAP task registered, nothing at the JSAP desk moves on its own** —
> the status column still refreshes on page load, but advancing/returning waits
> for someone to click **↻ Refresh from JSAP**.
>
> Cost per sweep: one HANA query (ODRF) + two SQL Server queries **per parked
> invoice**. Fine at a handful; stretch to 30 minutes if that desk ever holds
> hundreds.

---

## 10. Reports and the Excel export

### Reports (`reports.py`)

Everything derives from closed `StageEvent` rows plus live dwell time:

- **Summary** — in progress, completed, overdue, average cycle days
- **Pending by stage** with overdue counts
- **Average days per stage** (closed visits)
- **Bottlenecks** by person, vendor (top 20) and category
- **Ageing buckets** — 0-2 / 3-5 / 6-10 / 10+ days

Filterable by date range, branch, unit, category. Overdue and ageing are
computed in Python (they need a per-stage threshold comparison); the heavier
historical averages are aggregated in the database.

### Excel export (`exports.py`)

Flattens each invoice's history back to **one row**, in the original workbook's
column order — Head Office / Bilty-GRPO / Pre-Audit / **Transport Approval** /
Data Entry / SAP Approval / **JSAP Approval** / Save-in-SAP / Payment — plus
newer fields (current stage, GST number, additional charge). The Transport
Approval columns stay blank for anything that never went there, and read
`Pending` while the visit is still open.

For a bounced invoice, `_latest_by_stage()` keeps the **final** pass at each
stage. The JSAP columns show JSAP's own reason as the remarks.

---

## 11. Frontend pages

`OMS-Frontend/src/pages/`, API in `src/services/trackerService.ts`,
styles in `src/styles/Tracker.css`.

| Page | What |
|---|---|
| `Tracker_Entry.tsx` | Invoice entry form — SAP vendor dropdown, month picker, duplicate/future-date guards |
| `Tracker_Queue.tsx` | **The main desk.** Stage tabs, sub-tabs, bulk actions, payment modal, timeline, JSAP panel |
| `Tracker_Invoices.tsx` | Admin master list with filters + Excel export |
| `Tracker_Reports.tsx` | Analytics |
| `Tracker_Alerts.tsx` | Stuck alerts (admin) |
| `Tracker_Admin.tsx` | Stages, lookups, users, stage assignment |

### Queue sub-tabs

| Tab | Shows |
|---|---|
| Current | Normal arrivals |
| Returned | Arrived via RETURN — shows who sent it back and why |
| Awaiting Remarks | `rejection_pending` — rejected here, reason still owed; supply remarks to send it back (SAP/JSAP desks) |
| Partial | Part-paid invoices (terminal stage), kept out of Current |
| Advanced | Read-only history of what left this desk (entry desk) |
| Hold · OK · Debit | The desk's **decision log** — Pre-Audit |
| Approved · Rejected | Verdict log — SAP / JSAP / Transport Approval |
| Sent Back | What this desk returned — Pre-Audit |
| Transport Approval | Pre-Audit only — one row per trip to the approval desk |

Which of these appear is driven by the stage's own `status_choices`, so retuning
a stage in Admin picks them up without a code change.

> **"Awaiting Remarks" vs "Rejected".** They are different questions. The first
> is the live queue: rejected here, still sitting here, reason not written yet.
> The second is the log: what this desk actually sent back, and to whom.

### The decision-log tabs

Every history tab reads `stage-decisions/`, not the live queue. They **have
to**: only a FULL hold keeps an invoice at Pre-Audit — OK, DEBIT and a PARTIAL
hold all advance it — so filtering the queue would leave the OK and Debit tabs
permanently empty.

They are logs of **events**, not invoices: an invoice debited twice appears
twice, each row with its own amount, reason, handler and timestamp, plus where
the invoice sits *now* (`still here` marks a full hold that never left). The
Transport Approval tab groups the approval desk's rows by **visit**, so a
rejection and the later re-send are two rows, each with its own verdict:
`AWAITING` · `APPROVED` · `REJECTED` · `REJECTION_PENDING`.

**Verdicts collapse per visit.** APPROVED / REJECTED / RETURN are one decision
per stage visit, so the note row a reason-less rejection writes and the row that
closes the visit when the reason arrives are the *same* decision — they merge
into one row (the closed one wins, since it carries the reason). Reject the same
invoice twice, with a return to the desk in between, and you get two rows.

> ### A send-back drops off once the invoice comes back
> The Rejected and Sent Back tabs answer "what is still out there", not "what
> did I ever reject". A row is dropped once the invoice has **arrived at this
> desk again** after the decision — `came_back`, computed by comparing each
> later `StageEvent.entered_at` at this stage against the decision time. The
> desk is looking at it again, so the rejection is answered.
>
> Tick **"Also show ones that came back"** (`?include_resolved=1`) to see the
> full history; those rows are flagged *came back since*. Note rows written
> during a visit share that visit's `entered_at`, so they can never trigger the
> flag by themselves.

### Per-tab Excel export

Every sub-tab has its own **Export Excel** button, which writes exactly what the
tab is showing — **including the omni-search filter**, so a search for one party
exports just that party's rows.

> The sheet is the **same register layout as the All-Invoices export** — same
> columns, same order, same styling. `stage-export/` hands the ids straight to
> `exports.build_workbook()`, the one used by `all-invoices/export/`, so the two
> sheets match column for column and a desk's sheet drops into the office
> workbook unchanged.

The client sends the visible row ids; the server does **not** trust them for
access — they are intersected with the invoices reachable from that stage (any
it has ever handled, which is exactly what its tabs can show), and a user not
assigned to the stage gets a 403. A decision log can list one invoice twice (it
was debited twice); the register is one row per invoice, so ids are
de-duplicated.

The JSAP desk additionally shows a **JSAP Status** column (verdict, the
approver's reason, the draft DocEntry — or why it could not be linked) and the
**↻ Refresh from JSAP** button. Both the mirror and the manual controls are
available there.

---

## 12. Setup and configuration

### Settings (`OMS/settings.py`)

```python
HANA_OIL_COMPANY_DB       = config('HANA_DB_OIL_NAME')
HANA_BEVERAGE_COMPANY_DB  = config('HANA_BEVERAGE_COMPANY_DB')
HANA_MART_COMPANY_DB      = config('HANA_MART_COMPANY_DB', default='JIVO_MART_HANADB')

# JSAP (budget approval) SQL Server — blank host disables every JSAP lookup
JSAP_DB_HOST     = config('JSAP_DB_HOST', default='')
JSAP_DB_PORT     = config('JSAP_DB_PORT', default=1433, cast=int)
JSAP_DB_NAME     = config('JSAP_DB_NAME', default='')
JSAP_DB_USER     = config('JSAP_DB_USER', default='')
JSAP_DB_PASSWORD = config('JSAP_DB_PASSWORD', default='')

TRACKER_ALERT_EMAIL_COOLDOWN_HOURS = config(..., default=24, cast=int)
```

Also required: HANA connection settings (shared with the rest of OMS) and SMTP
for alert emails. `pymssql` provides the JSAP driver.

> `jsaplive3` lives on **103.89.45.75**, not `.76`.

### First run

```bash
python manage.py migrate tracker
python manage.py seed_tracker      # stages + lookup dropdowns — see the warning in §14
```

Then, in the UI: create tracker users, assign each to their stage(s) under
**Admin → Users → Stages**. **A stage with no users mapped is invisible to
everyone but superusers** — the usual cause of "the new desk isn't showing".

---

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| A desk doesn't appear in anyone's queue | No `UserStageAccess` rows for it. Assign users under Admin → Users → Stages |
| JSAP desk shows **"No SAP vendor"** | `party_code` is blank — the vendor wasn't picked from the SAP dropdown. The link cannot resolve on the number alone (§5) |
| JSAP desk shows **"No SAP draft"** | No `ODRF` row matches `NumAtCard` + `CardCode` in that company. Check the invoice number matches SAP's Vendor Ref. No. exactly |
| JSAP shows **"Not submitted"** | Normal for a fresh draft — it exists in SAP but hasn't reached JSAP |
| JSAP status never changes on its own | `run_jsap_sync.bat` is not registered in Task Scheduler (§9) |
| JSAP lookups return nothing at all | Querying `dbo.*` instead of `bud.*`, or joining `jsDocEntry` to `OPCH` instead of `ODRF` (§6) |
| Wrong company's approval attached | The `bud.jsBudgetTable.Branch` cross-check was bypassed — draft DocEntry repeats across companies |
| A Transport invoice went straight to Data Entry | It was already approved **into this Pre-Audit visit**, or the Transport Approval stage is inactive / its category isn't exactly "Transport" |
| The same invoice keeps returning to Transport Approval | By design — approval is per Pre-Audit visit, so every bounce back to Pre-Audit needs a fresh approval (§2) |
| Transport Approval desk is empty for everyone | No `UserStageAccess` rows for it — a new stage starts with nobody mapped (§12) |
| Pre-Audit's OK / Debit tabs look "wrong" | They are logs of past decisions, not the queue — those invoices have moved on (§11) |
| A rejection vanished from the Rejected tab | The invoice came back to this desk, so it is no longer outstanding. Tick "Also show ones that came back" (§11) |
| Two tabs both look like "rejected" | **Awaiting Remarks** = rejected here, reason still owed, still sitting here. **Rejected** = the log of what was actually sent back (§11) |
| Invoice fully paid but still OPEN | An un-released hold. `open_balance` is against `total_owed`, which always includes the hold (§4) |
| Discount looks "wrong" after a hold | Intended — discount is on the net invoice value **including** the hold |
| Invoice number rejected as duplicate but not visible | A **soft-deleted** invoice still reserves its number (`all_objects`) |
| Stuck-alert email for a deliberately held invoice | Only Pre-Audit **FULL** holds are exempt |
| `.bat` prints "'X' is not recognized" | Non-ASCII (em dash) in a `REM` line — `cmd` mis-parses it. Keep batch comments ASCII |
| Stage reorder fails with a unique violation | `Stage.order` is unique — park the moving rows out of range first (§14) |

---

## 14. Design decisions and gotchas

**Self-contained app.** No FK into any OMS business model, so the tracker can be
reasoned about, migrated and (if ever needed) lifted out on its own.

**Derived time, never typed.** Every duration comes from `StageEvent`. The
moment a date becomes hand-editable, every report becomes a negotiation.

**One movement entry point.** All flow rules live in `apply_action()`; views are
thin and bulk cannot bypass validation.

**Stage config is data, not code.** Names, order, thresholds and status choices
are admin-editable. Logic keys on `code` — **renaming a stage is safe, changing
its code is not**.

> ### ⚠️ `Stage.order` is unique
> Inserting or reordering a stage collides mid-loop unless the moving rows are
> parked out of range first. Both `seed_tracker` and migration `0014` do this
> (offsets 900/1000). Follow the pattern for any future reorder.

> ### ⚠️ `seed_tracker` overwrites stage names
> It is `update_or_create` keyed on `code` with the full defaults, so re-running
> it **renames stages back to the seed values** (e.g. a stage renamed to
> "Head Office In" in the UI reverts to "Invoice Entry"). Safe for a fresh
> install; on a live database, know what it will overwrite.

**Soft delete everywhere.** Rows are hidden, never destroyed — audit history
survives, and the invoice number stays reserved.

**Pending rejection.** Rejecting without a reason parks the invoice rather than
moving it. The reason is mandatory to actually send it back, but demanding it up
front made approvers write "-" — so the two-step exists deliberately.

**The tracker never writes to SAP or JSAP.** Both are systems of record; this is
a mirror. The only thing that flows back is a human's decision, recorded here.

**Branches vs. steps.** A desk that hands the invoice *back* to its sender is a
branch, not a step. Modelling Transport Approval as a branch (out of
`stage_route`, into `_branch_neighbour`) keeps every other neighbour honest —
had it been a step at order 4, a return from Data Entry would have landed on the
approval desk instead of Pre-Audit. Its `order` only positions it in the display
and the delete cut-off.

**The 2026-08-11 Transport Approval branch.** Migration `0015` inserts it at
order 4 and pushes Data Entry → Payment down one (parking them out of range
first, as `Stage.order` is unique), and moves `DELETE_ADMIN_MAX_ORDER` 6 → 7 to
preserve the original intent. Nothing in flight is moved: an invoice already at
Pre-Audit picks the detour up on its next advance. Assign users to the new desk
before anyone sends anything there.

**The 2026-07-29 SAP/JSAP split.** These were one desk ("SAP / JSAP Approval").
Migration `0014` renamed it **in place** so its id, history and user mappings
survived, then inserted `jsap_approval` after it. Anything already parked there
stayed on `sap_approval` and passes through the new desk on its next advance.
`DELETE_ADMIN_MAX_ORDER` moved 5 → 6 to preserve the original intent.

---

## Related

- `tracker/jsap.py` — the JSAP contract, documented in the module docstring
- `tracker/sap.py` — SAP A/P and draft lookups
- `einvoice/docs/INVOICE_PRINTING_AND_QR_RUNBOOK.md` — the *sales* side (IRN/QR); unrelated flow, same company-DB routing idea
- `run_stuck_alerts.bat`, `run_email_stuck_alerts.bat`, `run_jsap_sync.bat`
