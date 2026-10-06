# Budget approval — going live

The order of work to move budget approval of SAP drafts from JSAP to OMS on the live servers, one
company at a time (**Beverages first**, then Oil). Read it all before starting. Most of it is safe
and reversible; **step 5 (the SAP gate) is the moment of change**.

What the module is and why it is built this way: `BUDGET_DESIGN.md`. How JSAP did it:
`BUDGET_JSAP_SAP.md`. The app developer's API: `BUDGET_APP_API.md`.

| Server | What it is |
|---|---|
| `.118` | OMS backend (`C:\LiveProjects\OMS\Backend`, port 8000), live Postgres (`order_management`), scheduled tasks |
| `.222` | SAP HANA / Service Layer — the live company schemas `JIVO_BEVERAGES_HANADB`, `JIVO_OIL_HANADB` |
| JSAP | SQL Server + the JSAP app — today's budget approval, to be retired per company |

---

## How the switch works, in one picture

```
Before                                        After (per company)
SAP draft ──gate reads──> tbl_Draft_Approvals SAP draft ──gate reads──> tbl_Draft_Approvals (VIEW)
                          (JSAP's table,                                        │
                           JSAP writes it)                                      ▼
                                                                    OMS_BUDGET_APPROVALS (OMS writes it)
                                              JSAP's table kept as tbl_Draft_Approvals_JSAP (history)
```

At the switch, every row JSAP's table holds is copied into OMS's table (`DecidedBy = 'JSAP'`), so a
draft JSAP already approved stays postable. Drafts still waiting are then taken into OMS **from SAP**
by the first sync and start at their first OMS stage.

**What approvers notice:** anything they had half-approved in JSAP (an intermediate stage) starts
again at the first stage in OMS. Anything JSAP had fully approved needs nothing.

---

## 0. Before the day (no risk — do these first)

### 0.1 The test that proves SAP respects OMS (on TEST)

Not yet done — do it first. In the **SAP desktop client**, company `TEST_JIVO_OIL_HANADB`:

| Draft | In OMS | Add it — expect |
|---|---|---|
| A/P invoice draft **51588** (Hindustan Petroleum, ₹27,143) | approved (item 427) | **posts** |
| A/P invoice draft **54447** (Facebook, ₹2,322) | pending | **refused** with the budget message |
| any draft you reject in OMS first | rejected | **refused** |

Do not go live until all three behave. (The Service Layer cannot run this test while `.222`'s
attachment mounts are down; the desktop client can.)

### 0.2 Code on the live branches

Budget is on `Mukesh2` only. Bring it to `production` (backend) and `live` (frontend) the way the
Payments module was brought over — the module's folders plus its registrations, nothing else of
Mukesh2:

**Backend (`production`)**
- `budget/` (whole app: models, migration `0001_initial`, services, commands, views, tests)
- `hana/management/commands/budget_gate.py`
- `OMS/settings.py`: `'budget'` in `INSTALLED_APPS`, `_company_schemas()`, `BUDGET_SAP_SCHEMAS`
- `OMS/urls.py`: `path('budget/', include('budget.urls'))`
- `core/permission_registry.py`: `Budget_Approval`, `Budget_Settings`, `Budget_Reports`
- `docs/Approvals/BUDGET_*.md`

**Frontend (`live`)**
- `src/pages/Budget_Approval.tsx`, `Budget_Settings.tsx`, `Budget_Reports.tsx` (+ their tests),
  `src/pages/budget/useDeepLinkedItem.ts`, `src/services/budgetService.ts`
- registrations: `App.tsx` (three routes), `auth/routeAccess.ts`, `components/layout/navigation.ts`
  (the "Budget" section), `config/adminPages.ts`, `utils/notificationRouting.ts` (`budgetitem`),
  `pages/routes.smoke.test.tsx`

Run the backend tests (`manage.py test budget --settings=OMS.test_settings`), the frontend
type-check, lint and tests, then deploy both as usual. **Deploying the code changes nothing yet:**
with `BUDGET_SAP_SCHEMAS` empty the module is switched off — it reads no SAP and writes none.

### 0.3 The live database (Postgres on `.118`)

On `.118`, in `C:\LiveProjects\OMS\Backend`:

```bat
python manage.py migrate budget
python manage.py register_workflow_module --code BUDGET --name "Budget Approval"
```

`migrate budget` creates the `budget` schema and its tables — new tables only, nothing existing is
touched. The module can also be registered on the Workflows page instead.

### 0.4 The approvers' logins on live

The hierarchy is written with the TEST server's usernames. On live (checked 2026-10-06):

| Hierarchy name | On live | Do |
|---|---|---|
| `Gurpreet Vg` (Director) | `gurpreet` (id 13) | `--user "Gurpreet Vg=gurpreet"` |
| `avtar` | `Avtarsingh` (id 56) | `--user avtar=Avtarsingh` |
| `paramdeep` | `USER01` (id 107, Paramdeep Singh)? | **confirm**, then `--user paramdeep=USER01` |
| `nirmal` (BackOff) | **no login** | **create** one (or name someone else) |
| `Raju Vg`, `gagan`, `bhupinder`, `arvinder`, `karanpreet` | exist | none — but confirm `gagan` (id 45) is Gagandeep Singh |

Then write the workflows — dry run first, read the plan, then apply:

```bat
python manage.py seed_budget_workflows --user "Gurpreet Vg=gurpreet" --user avtar=Avtarsingh --user paramdeep=USER01
python manage.py seed_budget_workflows --user "Gurpreet Vg=gurpreet" --user avtar=Avtarsingh --user paramdeep=USER01 --apply
```

58 workflows (one per budget head per company, plus each head's electricity route). People can be
changed later on the Workflows page; re-running the command puts the hierarchy back.

### 0.5 Permissions

On Role Permissions / Page Permissions:

| Key | To |
|---|---|
| `Budget_Approval` | every approver in the hierarchy (it opens the desk; what they may decide is their workflow stage) |
| `Budget_Reports` | whoever oversees budgets (read-only, all approvers, Excel export) |
| `Budget_Settings` | whoever may switch auto-approval on/off and choose exempt users |

### 0.6 Scheduled tasks on `.118` (create now, leave **disabled**)

As SYSTEM, highest privileges (like the tracker tasks — not Interactive):

| Task | Command | When |
|---|---|---|
| `Budget Sync` | `python manage.py sync_budget_drafts` | every 5 minutes |
| `Budget Auto-Approve` | `python manage.py budget_auto_approve` | every 10 minutes |
| `Budget Reminder` | `python manage.py budget_pending_reminder` | daily, e.g. 10:00 |

Each from `C:\LiveProjects\OMS\Backend` with the project's Python. Auto-approval does nothing until
someone switches it on in Budget Settings (it ships **off**), so its task is harmless when enabled.

---

## 1–8. The switch, for ONE company

Below uses Beverages. For Oil, replace `BEVERAGES` / `JIVO_BEVERAGES_HANADB` with `OIL` /
`JIVO_OIL_HANADB`. Pick a quiet time; allow 30 minutes.

### 1. Tell the approvers
From now on, Beverages budget approvals are in OMS (web: **Budget → Budget Approval**; the app once
its screens ship). JSAP must not be used for Beverages any more — once the gate switches, a JSAP
approval of a Beverages draft fails to save.

### 2. Look at the gate before touching it (read-only)
```bat
python manage.py budget_gate --schema JIVO_BEVERAGES_HANADB --allow-live
```
Without `--apply` it only reports: the gate should be JSAP's **table**, and OMS's table absent.

### 3. JSAP's jobs
Stop the JSAP SQL Agent jobs for budget **only when the LAST company switches** (they serve both):
*Budget Sync Data* (5 min), *48 hrs* (auto-approve), *Sync attchments* (2 min). Until then they keep
serving Oil; for Beverages they become harmless (a Beverages write-back to the new view simply fails).

### 4. Point OMS at the live company
In `.118`'s `.env`:
```
BUDGET_SAP_SCHEMAS=BEVERAGES=JIVO_BEVERAGES_HANADB
```
(Later, when Oil follows: `BEVERAGES=JIVO_BEVERAGES_HANADB,OIL=JIVO_OIL_HANADB`.) Restart the OMS
backend service so it reads the new value.

### 5. Switch SAP's gate to OMS — the moment of change
```bat
python manage.py budget_gate --schema JIVO_BEVERAGES_HANADB --apply --allow-live
```
It creates `OMS_BUDGET_APPROVALS`, copies JSAP's rows into it, renames JSAP's table to
`tbl_Draft_Approvals_JSAP`, creates the view `tbl_Draft_Approvals` over OMS's table, grants it to
SAP's procedure owners, and recompiles every procedure that reads it — **all must come back
valid**. If any step after the rename fails, it puts JSAP's table back by itself. Read the status it
prints at the end: gate = **VIEW**, rows imported, procedures valid.

### 6. Take the waiting drafts in
```bat
python manage.py sync_budget_drafts --company BEVERAGES --dry-run
python manage.py sync_budget_drafts --company BEVERAGES
```
The dry run must show `unroutable: []` and `errors: []` — an unroutable draft means a budget head
with no workflow, or a missing user; fix that and re-run. `already_approved` counts drafts JSAP had
fully approved (they are not taken in again). The real run sends each approver one "N budget
approvals waiting for you" notification.

### 7. Enable the scheduled tasks
`Budget Sync` and `Budget Reminder` now (and `Budget Auto-Approve` if auto-approval is wanted).
Check **Budget Settings → SAP feed**: Beverages' last sync is minutes old.

### 8. Prove it on one real draft
- An approver approves one waiting item on **Budget Approval**.
- Its message says SAP accepted it; Retry SAP does not appear.
- The draft's creator **adds it in SAP** — it posts.
- Optionally reject a test draft and confirm SAP refuses it.

Beverages is live. Repeat 1–8 for Oil when ready.

---

## What to watch in the first week

| Where | Look for |
|---|---|
| Budget Settings → SAP feed | last sync never older than ~10 minutes; a stale feed means the task stopped |
| Budget Reports, status Pending | items waiting too long on one person — reassign on the Workflows page, or set a stand-in |
| Budget Approval rows marked *SAP write failed* | press **Retry SAP**; repeated failures mean `.222` is unreachable |
| `.118` `logs\oms.log` | lines starting `BUDGET:` |
| JSAP | nobody still approving that company there |

---

## Rolling back one company

```bat
python manage.py budget_gate --schema JIVO_BEVERAGES_HANADB --rollback --allow-live
```
This drops the view and renames JSAP's table back as the gate; OMS's table is kept. Then remove the
company from `BUDGET_SAP_SCHEMAS`, restart the backend, and tell approvers to use JSAP again.

**Know before you roll back:** decisions made in OMS since the switch are **not** in JSAP's table.
A draft OMS approved but nobody has posted yet will be refused again until JSAP approves it. List
them first (on `.222`):

```sql
SELECT "ObjType", "DocEntry", "LineNum", "ApprovedStatus", "DecidedBy", "DecidedAt"
FROM "JIVO_BEVERAGES_HANADB"."OMS_BUDGET_APPROVALS"
WHERE "DecidedBy" <> 'JSAP' AND "ApprovedStatus" = 'A';
```

---

## Not part of this release

- **Mart** — SAP has no budget gate for Mart yet; nothing to switch until the SAP team adds one.
- **The OMS mobile app's Budget screens** — the app developer has `BUDGET_APP_API.md`.
  Notifications already reach the app; until its screens ship, approvers decide on the web.
