# BUDGET — Budget approval of SAP drafts in OMS

Design for replacing JSAP's budget approval (`bud` schema) with an OMS module.
Read `BUDGET_JSAP_SAP.md` first: it is the measured description of how JSAP
does this today, and §8a records the SAP-side table OMS now owns.

Decided with the business, 2026-10-06:

* A separate workflow module, **`BUDGET`**: approval stages only. Unlike
  Payments there is **no Payment / Audit / Final** — JSAP's 96 active budget
  templates are all one or two approval stages.
* The approvers follow the budget hierarchy (`Budget_Approval_Hierarchy.xlsx`),
  routed by **budget head**.
* OMS writes its decisions to **its own table**, `OMS_BUDGET_APPROVALS`, never to
  JSAP's. SAP's gate reads it through the view `tbl_Draft_Approvals` (option B).
* Payments handled by the OMS Payments module are **out of scope**: they are
  posted directly, never as drafts, and pay vendor / employee / customer
  accounts, which SAP's gate does not check.
* The monthly budget is read from **SAP's own budget** (OBGS/OBGT), only to
  decide auto-approval. It is **not shown** to approvers and **never blocks**
  them.
* **48-hour auto-approval** stays, as a setting, with a list of users it never
  acts for.
* **Mart waits**: it has no gate table, so an OMS decision would change nothing
  in SAP until the SAP team adds one.

---

## 1. Scope — what enters approval

A **line** of a SAP **draft** enters budget approval when SAP's gate would check
it: an **indirect expense** account (grandparent `5600000`: delivery, general,
repair & maintenance, finance, selling, travel, rents & taxes) or account
`5100008`. Measured over the last 180 days that is every line JSAP received —
no fixed-asset, raw-material or stock lines ever reach it. So the Payments
hierarchy rows for FA, RM & PM, oil and imports do not apply here.

| object type | document | Oil / Bev drafts, last 90 days |
|---|---|---|
| 18 | A/P invoice | 769 / 204 |
| 46 | outgoing payment (expense-account payments made in SAP) | 183 / 26 |
| 28 | journal voucher | 86 / 20 |
| 19 | A/P credit memo | 62 / 30 |
| 14 | A/R credit memo | 33 / 1 |

Companies: **Oil and Beverages**. Mart is out until it has a gate.

Ported from `JIVO_OIL_HANADB."DRAFT_APPROVAL"`, which takes in **both** Oil and
Beverages (it reads the Beverages schema directly — that is why Beverages has no
procedure of its own):

| types | SAP tables | line key written to the gate | waiting means |
|---|---|---|---|
| 14, 15, 18, 19, 59, 60 | `ODRF` / `DRF1` | `DRF1.LineNum` | SAP's approval request open: `OWDD.ProcesStat = 'Y'`, `WDD1.Status = 'Y'` |
| 46 | `OPDF` / `PDF4` | `PDF4.LineId` | the same (Oil also takes `WDD1.Status = 'P'`) |
| 28 journal voucher | `OBTF` / `BTF1` (`BatchNum` is the DocEntry) | `BTF1.Line_ID` | batch open (`BtfStatus = 'O'`); debit lines only; salary accounts `5630001`–`5630016` excluded |

Every branch: document date on or after 2025-04-01; account parent in
`5610000`–`5690000` or account `5100008`; and **`OcrCode3 != 'Sal CF'`** — so
**salary (`Sal CF`) never enters budget approval**, and neither does a line with
no budget code (the same condition drops NULL).

Which schemas: **`BUDGET_SAP_SCHEMAS`** in `.env`, e.g.
`OIL=TEST_JIVO_OIL_HANADB,BEVERAGES=TEST_JIVO_BEVERAGES_HANADB` — read and
written by this module only. Unset, the module does nothing; it never falls
back to the live company schemas the rest of OMS reads.

---

## 2. Tables

App `budget`, schema `budget`, the BKDT / PRDO shape.

### 2.1 `budget_draft` — one SAP draft

`company`, `obj_type`, `draft_entry` (unique together), `doc_date`,
`card_code`/`card_name`, `created_by_sap` (U_NAME), `doc_total`, `comments`,
`status` (PENDING / APPROVED / REJECTED / GONE — posted or deleted in SAP before
a decision), `synced_at`, `first_seen_at`.

### 2.2 `budget_line` — one gated line

`draft` FK, `line_num` (as the gate reads it), `vis_order`, `acct_code`,
`acct_name`, `budget_code` (OcrCode3), `sub_budget_code` (OcrCode4), `amount`,
`effect_month`, `route` (see §3), `item` FK.

### 2.3 `budget_item` — what one owner approves

The **approval unit**. A draft whose lines fall under two heads (a Factory bill
with an electricity line) becomes two items, each with its own route and
approver; the draft is APPROVED only when every item is. `draft` FK, `route`,
`amount` (sum of its lines), `status`, `workflow` / `matched_query` /
`current_stage` / `current_user` / `version` (the flow, as Payments and PRDO
keep it), `sap_write_status` / `sap_write_error` / `sap_written_at`.

### 2.4 `budget_action_log` — append-only history

Per item: CREATED, APPROVED, REJECTED, AUTO_APPROVED, SAP_WRITTEN,
SAP_WRITE_FAILED, GONE — with actor, stage, remarks. **Transitions only**: no
row for a poll that changed nothing (JSAP wrote 127,000 of those a day).

### 2.5 `budget_settings` — one row

`auto_approve_enabled`, `auto_approve_hours` (48), and an M2M `exempt_users`:
people the auto-approval never acts for (JSAP hard-coded three ids in the
procedure body).

---

## 3. Routing — by budget head

The route of a line is computed in code from what SAP says about it:

| line | route |
|---|---|
| account `5680011` (electricity) | `<HEAD>_ELECTRICITY` — owner, then **Director Approval** |
| any other account | `<HEAD>` — the head's owner(s) per the hierarchy |
| no budget code (`OcrCode3` empty / NA) | **not taken in** — as JSAP: SAP's gate keeps the draft unpostable until its creator adds the budget in SAP, and the next sync then takes it in. Rare: 8 Oil and 1 Beverages draft line in 180 days, none ever posted. The sync counts what it skipped in its own output. |

`<HEAD>` is the budget code: `BackOff`, `Sales`, `Sales RE`, `Del Bkhp`, `Del Mayp`, `Factory`, `FACT_COM`, `Interest`, `Med MKT`, `R & D`,
`OTE`, `Transprt`, `NPD1–3`.

One workflow per route per company, its query one line on the item's route —
e.g. `SELECT id FROM budget.budget_item WHERE company = 'OIL' AND route =
'FACTORY_ELECTRICITY'` — so exactly one workflow matches every item. A
`seed_budget_workflows` command (dry run by default, like
`seed_payment_workflows`) writes them from the hierarchy:

| head | Oil | Beverages | then |
|---|---|---|---|
| BackOff | Nirmal Didi Ji | Nirmal Didi Ji | electricity → Director |
| Sales, Sales RE | Jasvir Singh | Karanpreet Singh | |
| Del Bkhp, Del Mayp | Bhupinder Singh | Bhupinder Singh | |
| Factory, FACT_COM | Gagandeep Singh | Arvinder Singh | electricity → Director |
| Interest | Avtar Singh | Avtar Singh | |
| Med MKT | Karanpreet Singh | Karanpreet Singh | |
| R & D, OTE | Director only | Director only | |
| Transprt | Paramdeep Singh | Paramdeep Singh | |
| NPD1–3 | Avtar Singh | Avtar Singh | Director |

Stage users are then the business's to change on the Workflows page; where
Payments sends a row to a picked Department Head, budget uses a fixed person
set there.

---

## 4. Intake — `sync_budget_drafts`, every 5 minutes

Reads SAP directly, never JSAP (JSAP's production-order copy proved 17.9%
wrong). For each company and object type: drafts awaiting SAP approval (SAP's
own approval request, OWDD/WDD1, open), their gated lines (§1), each line's
budget and route. New drafts get rows, items and flows; known drafts are
refreshed.

* **Fails loudly.** SAP unreachable raises; the run stamps `synced_at`; a run
  that finds nothing is reported, not silent — JSAP's production feed died for
  7 weeks with its job reporting success every minute.
* **A draft changed in SAP after an approval** (lines or amounts differ): its
  decided items go back to PENDING and the change is logged — an approval was
  for what the approver saw.
* **A draft posted or deleted** in SAP before a decision: GONE, logged, taken off
  the queues.

---

## 5. Deciding

Same engine and rules as Payments' approval stages: one user per stage today
(replacements applied), approve / reject with remarks, `version` refuses a
stale screen. The queue lists **everything waiting for the user**; budget month
is a filter they choose, never a default (JSAP's month filter hid 695
documents).

**Rejection is final for that version of the draft** (decided 2026-10-06). No
one can approve it afterwards. If the creator **changes the draft in SAP**, the
next sync sees new lines or amounts and puts it back to PENDING for approval
(§4) — the way out JSAP never had: it took a draft in only the first time it
saw it, so a corrected rejected draft was stuck for good. (JSAP let an approver
reverse a rejection; 5 of its 328 rejected documents were.)

### Auto-approval

A scheduled job, when enabled: an item waiting at a stage for
`auto_approve_hours` is approved on the stage user's behalf **only if**

1. the stage user is not in `exempt_users`, and
2. SAP's budget for the head and month exists (OBGS/OBGT), and the head's
   posted spend for the month plus this item stays within it.

No budget for the month means **no auto-approval** — never "a budget of zero"
(JSAP's `ISNULL(..., 0)` stopped every auto-approval from March 2026). An item
that does not qualify logs nothing; one that does logs AUTO_APPROVED once.

---

## 6. Writing the decision to SAP

Per line, into `OMS_BUDGET_APPROVALS` (`(ObjType, DocEntry, LineNum)` key):

| event | `ApprovedStatus` | `VerifiedStatus` |
|---|---|---|
| a stage approves, more stages follow | `V` | — |
| the last stage approves (item APPROVED) | `A` | `V` |
| rejected | `R` | — |

plus `BUDGET`, `AMOUNT`, `OmsRequestId`, `DecidedBy`, `DecidedAt`. Upsert, then
**read back and compare** (JSAP's one good habit). A failed write leaves the
item decided in OMS with `sap_write_status = FAILED`, shown on the item and
retried from it (production's Retry SAP) — never silently.

---

## 7. Switching a company over

1. Build and test against `TEST_JIVO_*` (already on option B).
2. Pick the company (Beverages first, ~130 drafts a month).
3. Stop JSAP's budget jobs for it (`Budget Sync Data`, its write-back, `48 hrs`).
4. `budget_gate --schema <live> --apply --allow-live` — imports JSAP's rows, so
   everything JSAP approved stays postable.
5. Run `sync_budget_drafts` once: pending drafts are rebuilt **from SAP**, not
   copied from JSAP.
6. Approvers use OMS. Then Oil.

---

## 8. Permissions and pages

`Budget_Approval` (the queue and deciding), `Budget_Settings` (auto-approval and
exempt users). Pages: the approver's queue, a draft's detail (lines, items, the
trail per item, SAP write state), settings.

---

## 8b. Build status — 2026-10-06

App `budget` (Postgres schema `budget`), API `/api/budget/`:

| piece | where |
|---|---|
| models + migration `0001_initial` (applied on the test DB) | `budget/models.py` |
| intake (port of `DRAFT_APPROVAL`), budget / spend, gate write with read-back | `budget/services/sap.py` |
| route per line | `budget/services/routing.py` |
| flow: open, approve, reject, retire | `budget/services/flow.py` |
| sync: new / changed / gone | `budget/services/sync.py` |
| auto-approval | `budget/services/auto.py` |
| hierarchy → 58 workflows | `budget/hierarchy.py`, `seed_budget_workflows` |
| scheduled jobs | `sync_budget_drafts` (5 min), `budget_auto_approve` (10 min) |
| permission keys | `Budget_Approval`, `Budget_Settings` |

To run it against the TEST companies:

1. `python manage.py migrate budget`
2. register module `BUDGET` on the Workflows page
3. `BUDGET_SAP_SCHEMAS=OIL=TEST_JIVO_OIL_HANADB,BEVERAGES=TEST_JIVO_BEVERAGES_HANADB` in `.env`
4. `python manage.py seed_budget_workflows --apply`
5. `python manage.py sync_budget_drafts --dry-run`, then without `--dry-run`

The intake has been run read-only against both TEST companies (Oil: 205 drafts /
693 lines across all five document types; Beverages: 89 / 236).

**Frontend** (`OMS-Frontend`):

| page | file |
|---|---|
| `/Budget_Approval` — "Waiting on you" / "Decided by you", company filter, the draft (lines, each budget head's stages and trail), Approve / Reject (reason required) / Retry SAP | `src/pages/Budget_Approval.tsx` |
| `/Budget_Settings` — SAP feed health (stale after 30 min), auto-approve switch and hours, exempt users | `src/pages/Budget_Settings.tsx` |
| API client | `src/services/budgetService.ts` |

Both pages are grantable per user (`adminPages.ts`) and in the sidebar's
"Budget" section. A decision sends the item's `version`: a stale screen is
refused. No budget figure is ever shown to an approver.

**State on the test DB (2026-10-06):** module `BUDGET` registered, 58 workflows
seeded, `sync_budget_drafts` run on both TEST companies — 261 drafts, 318 items
(317 pending, 1 approved). Not yet scheduled (§4); auto-approval off.

**Round trip, item 427** (Oil A/P invoice draft 51588, route TRANSPRT, 3 lines):
approved in OMS → the 3 lines are `A` / `V` in
`TEST_JIVO_OIL_HANADB.OMS_BUDGET_APPROVALS` and read `A` through
`tbl_Draft_Approvals`, the view SAP's gate reads. Still to do: add that draft in
TEST SAP (expect it to post), and try adding a pending one (e.g. 54447) and a
rejected one (expect SAP's budget refusal).

**Added after the JSAP source review (2026-10-06):**

| piece | where |
|---|---|
| Draft attachments for approvers — ATC1 via `AtcEntry` (ODRF / OPDF), a journal voucher's `OBTF.U_ATTACH_LINK`; files served through the attachment file service on .118, never SAP's Service Layer | `sap.draft_attachments`, `drafts/<id>/attachments/[<line>/]` |
| Notifications (web + OMS app, `notifications` framework): next stage's user on advance, one summary per approver per sync, earlier actors on the outcome, daily reminder | `services/notify.py`, `budget_pending_reminder` (daily) |
| Bulk approve — each item decided on its own (version, SAP write) | `items/approve-bulk/` |
| Reports — every item by document month / company / status / search, per-approver tally, Excel export (Approvers, Items, Lines); key `Budget_Reports` | `services/report.py`, `report/`, `report/export/`, `/Budget_Reports` |
| Deep link: a notification opens its item (`?itemId=`), with Approve / Reject in the draft dialog | `items/<id>/`, `useDeepLinkedItem.ts` |

Scheduled jobs are now three: `sync_budget_drafts` (5 min), `budget_auto_approve`
(10 min), `budget_pending_reminder` (once a day).

Fixed on the way: approving returned 500 — `select_for_update(of=('self',))`
rendered `FOR UPDATE OF "budget"."budget_item"`, which Postgres refuses for a
schema-qualified name. The lock now takes the item row alone, with no join
(`flow._locked`).

---

## 9. Open questions

1. Mart: when will the SAP team add its gate?

Answered by porting `DRAFT_APPROVAL` (2026-10-06): the per-type tables and line
keys (§1), how Beverages gets in, and that salary is never in scope.

Settled 2026-10-06: rejection is final per draft version, a changed draft comes
back (§5); lines with no budget code are not taken in (§3).
