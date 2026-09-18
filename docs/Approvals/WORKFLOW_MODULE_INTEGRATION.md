# Workflow Module Integration Contract

**Audience:** anyone — human or Claude Code — adding a Workflow-enabled OMS module.
**Status:** binding contract. The engine architecture is settled; see
`WORKFLOW_ENGINE_IMPLEMENTATION_PLAN.md` for why each rule exists.

> **When Claude Code creates a new Workflow-enabled OMS module,
> that module must register exactly one row in `workflow.workflow_modules`
> containing its `code` and `name` — and nothing else.**

Not a suggestion, and not a step to leave for a developer to remember: a module
with no registry row cannot start a workflow at all. The module registers
*itself*, from its own `AppConfig`, so nobody has to remember.

---

## 0. Start here — the whole integration, in order

Everything below expands on these nine steps. Follow them in order and a new
module is Workflow-enabled with nothing left to remember. `backdate` (BKDT) is
the worked reference throughout: when a rule is easier to read as code, open the
equivalent file in `backdate/` — and `docs/Approvals/BKDT.md` describes that
module end to end.

| # | Step | Where | Section |
|---|---|---|---|
| 1 | Create the Django app and its own PostgreSQL schema | `<module>/models.py`, `0001_initial` | §0.1 |
| 2 | Create **three** tables of your own: the document, its flow, its action log | `<module>/models.py` | §0.1 |
| 3 | Register the module — `code` + `name`, nothing else | `<module>/apps.py` | §3 |
| 4 | Add permission keys and gate every endpoint | `core/permission_registry.py` | §8 |
| 5 | Call `select_for_module()` when a document is submitted | `<module>/services/flow.py` | §6 |
| 6 | Own approve / reject: advance the stage, write the log | `<module>/services/flow.py` | §9 |
| 7 | Resolve the current approver through `get_stage_assignment()` — never store it | `<module>/permissions.py` | §8, §10 |
| 8 | Configure workflows, queries and stages in the Workflows UI | runtime, not code | §5, §12 |
| 9 | Work the integration checklist | tests | §13 |

### 0.1 The three tables your module owns

The engine owns configuration. **Your module owns its runtime**, and that is
always the same three tables. BKDT's are named in brackets.

| Table | Holds | Rows |
|---|---|---|
| **the document** (`backdate.backdate`) | what was asked for — the business fields, the company, who raised it | one per request |
| **the flow** (`backdate.backdate_flow`) | where it is now — status, current stage, which workflow was chosen, any external-system outcome | **exactly one per document** (UNIQUE FK) |
| **the action log** (`backdate.backdate_action_logs`) | what has happened — CREATE / UPDATE / APPROVE / REJECT, who, when, why, and what changed | many per document, append-only |

Rules that hold for every module:

* The document and its flow **commit together**. A document that cannot be
  routed must not exist — see §6.
* The flow stores **`current_stage` as the engine's stage id**, never a copy of
  the stage's name, sequence or user. That id is the whole integration: an
  administrator reassigning a stage re-routes every request already waiting
  there, with nothing in your module updated.
* The action log is **append-only**. Nothing updates or deletes a row in it.
* Give your own schema a `db_table = '<schema>"."<table>'` and a
  `RunSQL('CREATE SCHEMA IF NOT EXISTS <schema>;')` in `0001_initial`, the way
  `workflow` and `backdate` both do.

### 0.2 What you must NOT build

No task table, no per-module copy of the engine's tables, no stored approver,
no quorum columns. §16 is the full list and the reasons.

---

## 1. Why every Workflow-enabled module needs a registry row

`workflow.workflow_modules` stores only module identity: **`code` + `name`**.

The business module remains the source of truth for its own document table,
flow table, model, lifecycle, **approval runtime** and history.

The Workflow Engine is a CONFIGURATION AND SELECTION engine. It owns five
tables — `workflow_modules`, `workflows`, `workflow_queries`,
`workflow_stages`, `workflow_user_replacements` — and nothing else. It has no
`workflow_task` and no `workflow_action`: one universal task shape cannot fit
every module's runtime, and a module's approval history is part of its own
audit story, not the engine's.

The registry row answers exactly one question — *does this module exist, and
may workflows be configured against it?* It is the bridge between the two
ownership domains, and it is deliberately the narrowest possible bridge:

| Owned by the **business module** | Owned by the **Workflow Engine** |
|---|---|
| Document model and table | Module registration (`code` + `name`) |
| Flow model and table | Workflow definitions |
| Business lifecycle | Workflow selection |
| Submission / resubmission rules | Condition queries |
| Business history / log | Workflow stages |
| Business-specific rules | Assigned approver, user replacement |
| | Task, approve, reject, workflow state |
| | Action audit |

### What the registry used to hold, and where those answers live now

Earlier revisions stored `business_table`, `business_key_column`, `flow_table`
and `flow_model` on the registry row. That was a second, hand-maintained copy
of facts the module's own migrations already stated, and the copy could drift
from them with nothing to detect it — renaming `BudgetFlow` left the registry
pointing at a class that no longer existed until an approval failed on a real
document.

| Old registry column | Where the answer comes from now |
|---|---|
| `business_table` | the module's own models; the engine does not need it |
| `business_key_column` | RUNTIME context the module passes — `select_for_module(..., key_column=...)`, defaulting to `id` |
| `flow_table` | the flow model's own `Meta.db_table` |
| `flow_model` | the flow class's own `workflow_module_code` — see §2.1 |
| `is_active` | removed; stop routing by deactivating the module's **workflows** |

None of it needs to be typed anywhere, so none of it can be typed wrongly.

## 2. The exact `workflow.workflow_modules` fields

| Column | Type | Required | Notes |
|---|---|---|---|
| `id` | `bigint` | auto | |
| `created_at` | `timestamptz` | auto | |
| `updated_at` | `timestamptz` | auto | |
| `code` | `varchar(30)` | yes | **UNIQUE**, UPPERCASE (DB CHECK `workflow_module_code_upper`) |
| `name` | `varchar(100)` | yes | human label |

There is nothing else, and new technical module metadata must not be added
here. If the engine appears to need a fact about a module's implementation,
that is a sign the fact belongs at the call site or on the module's own model.

### 2.1 What the engine does NOT hold

No `workflow_task`, no `workflow_action`, no `WorkflowFlowBase`. Your module
defines its own:

```
budget/models.py
├── BudgetRequest         the business document
├── BudgetFlow            its workflow execution state
├── BudgetApprovalTask    who currently has to act
└── BudgetApprovalAction  what was approved/rejected, by whom, when
```

Another module may shape these entirely differently, and that is the point.
The engine never sees them.

## 3. How a new module registers itself

```
Create Business Module
        ↓
Business Module registers:  code + name
        ↓
workflow.workflow_modules
        ↓
Admin opens Workflows page
        ↓
Creates workflow for that module
        ↓
Defines SQL conditions
        ↓
Defines approval stages
```

Nothing in that path requires anyone to look up a table name.

### 3.1 The supported pattern — the module registers itself

Put this in the module's own app, so deploying the module registers it:

```python
# budget/apps.py
from django.apps import AppConfig
from django.db.models.signals import post_migrate


def register_own_modules(sender, **kwargs):
    from workflow.registry import register_module
    register_module(code='BUDGET', name='Budget Approval')


class BudgetConfig(AppConfig):
    name = 'budget'

    def ready(self):
        post_migrate.connect(register_own_modules, sender=self)
```

`post_migrate`, not `ready()` directly: `ready()` runs before the table
necessarily exists (during the first `migrate`, or `collectstatic` on an
un-migrated database), so registering there either crashes or has to swallow
errors that would hide real ones.

`workflow/apps.py` documents exactly this pattern — copy it. The engine app
registers no modules of its own.

### 3.2 The manual escape hatch

```bash
python manage.py register_workflow_module --code BUDGET --name "Budget Approval"
python manage.py register_workflow_module --list          # verify
```

There are deliberately no `--business-table`, `--business-key-column`,
`--flow-table` or `--flow-model` options. Passing the equivalent keyword to
`register_module()` raises `TypeError` rather than being ignored, so a stale
deploy script breaks loudly instead of leaving its author believing the engine
still holds a value it no longer has.

## 4. Idempotent registration

`register_module()` uses `update_or_create(code=...)`. `code` is UNIQUE in the
database, so a second registration **updates** the existing row — it never
creates a duplicate. Safe to call from a migration that reruns, from
`post_migrate` on every deploy, or from a deploy script.

```
first  →  Registered module BUDGET (id=20)
second →  Updated module BUDGET (id=20)
count  →  1
```

Verify the exact schema, not just the rows — `SELECT *` would not show that a
column was left behind:

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'workflow' AND table_name = 'workflow_modules'
ORDER BY ordinal_position;
-- expect exactly: id, created_at, updated_at, code, name

SELECT id, created_at, updated_at, code, name
FROM workflow.workflow_modules ORDER BY id;
```

## 5. Creating a workflow for the module

Configuration is data, created through the normal API (or admin). The engine
needs all three levels before a document can be submitted:

```
workflow_modules   (the registry row)
      └── workflows          ← company applicability
             ├── workflow_queries   ← which documents enter (1..N)
             └── workflow_stages    ← who approves, in order (1..N)
```

```http
POST /api/workflow/workflows/   {"module": 1, "code": "WF_STD", "name": "Standard",
                                 "company": "ALL"}
POST /api/workflow/queries/     {"workflow": 1, "name": "all", "company": "ALL",
                                 "query_text": "SELECT * FROM budget.budget_request"}
POST /api/workflow/stages/      {"workflow": 1, "name": "Manager", "sequence": 1, "user": 42}
```

A workflow with **no stages** is refused at submit with
`InvalidWorkflowConfiguration` rather than creating a flow that deadlocks.

## 6. How the module invokes Workflow

**There is no generic `/workflow/start/` endpoint, no `/workflow/tasks/` and no
`/workflow/tasks/<id>/approve/`, and there will not be.** The engine does not
own business submission or approval. It answers ONE question, in process:

```python
from django.db import transaction
from workflow.services import selection

with transaction.atomic():
    document = BudgetRequest.objects.create(...)          # business row

    result = selection.select_for_module(
        module_code='BUDGET',
        document_id=document.pk,
        company=document.company,
        # key_column='DocEntry',     # only if your queries key on something
    )                                # other than `id`

    # The module builds its OWN runtime from the answer.
    flow = BudgetFlow.objects.create(
        document=document, workflow_code=result.workflow.code)
    first = result.stages[0]
    BudgetApprovalTask.objects.create(
        flow=flow, sequence=first.sequence, name=first.name,
        configured_user_id=first.user_id,
        assigned_to_id=first.effective_user_id)
```

Everything is passed IN as runtime context. The engine looks nothing up about
your tables — it has no `business_table`, no `business_key_column`, no
`flow_model`, and no way to discover them.

What comes back (`result.as_dict()`):

```json
{
  "workflow":      {"id": 12, "code": "BUDGET_STANDARD",
                    "name": "Standard Budget Approval",
                    "company": "ALL", "module_code": "BUDGET"},
  "matched_query": {"id": 21, "name": "high-value", "company": "ALL"},
  "stages": [
    {"id": 5, "sequence": 1, "name": "Manager Approval",
     "user_id": 100, "effective_user_id": 100},
    {"id": 6, "sequence": 2, "name": "Finance Approval",
     "user_id": 101, "effective_user_id": 108}
  ]
}
```

`user_id` is the CONFIGURED user; `effective_user_id` is who may act today
after replacements. Both are given because they answer different questions —
route work to the effective one, show and hold accountable the configured one.

`selection.stages_for(workflow)` returns the same stage list when you already
know the workflow, for a flow that is mid-approval and must NOT be re-selected.

Errors to handle: `WorkflowNotConfigured`, `AmbiguousWorkflowSelection`,
`InvalidWorkflowConfiguration`, `ConditionExecutionError`. All subclass
`workflow.exceptions.WorkflowError`.

## 7. Company rules

Companies are `OIL`, `BEVERAGES`, `MART` (`core.companies`). Applicability
lives on **both** `workflows` and `workflow_queries`, in ONE column each:

```
company = 'ALL'         -> one row serves every company
company = 'OIL'         -> that company only
company = 'BEVERAGES'
company = 'MART'
```

`ALL` is a stored value, not a NULL. There was previously a `company_scope`
column beside a nullable `company`; it is gone, on both tables. The pair let
the same fact be written two ways, needed three CHECKs to forbid the
contradictory combinations, and made every reader join two columns back
together before it could answer "which company?". One column makes the
contradictions unrepresentable rather than forbidden, and leaves a single
CHECK: `*_company_valid`.

**`ALL` is one row; never copy a configuration per company.**

Matching is exactly:

```
query.company = ALL   + document OIL   -> applies
query.company = OIL   + document OIL   -> applies
query.company = OIL   + document MART  -> does not apply
```

**There is no precedence.** `OIL` does not beat `ALL` — if both apply and both
match, that is `AmbiguousWorkflowSelection`. (This deliberately differs from
`approvals.resolve_workflow`, which prefers the specific row. That engine is
untouched.)

A query may only **narrow** its workflow's applicability: under a workflow set
to `OIL`, a query must be `ALL` or `OIL`.

## 8. Stage / user rules

```
workflow
   ├── Stage 1 (sequence=1) → exactly ONE user
   ├── Stage 2 (sequence=2) → exactly ONE user
   └── Stage 3 (sequence=3) → exactly ONE user
```

`workflow_stages.user_id` is `NOT NULL`. There is **no** approver collection,
**no** role expansion, **no** quorum, **no** `approval_required` /
`rejection_required`. `sequence` is execution order only — never selection
priority. Whether a stage may have more than one open task is a question
about YOUR task table — enforce it there.

### 8.1 Managing who works a stage

Three operations, all in the Stages and Replacements tabs of the Workflows
page. None creates a table, duplicates a workflow, or touches a business
module's data.

| Operation | What it writes | Where |
|---|---|---|
| Change one stage's user | `workflow_stages.user_id` on that ONE row | Stages → Edit (either view) |
| Temporary replacement | a `workflow_user_replacements` row | Replacements → Add Replacement |
| View a user's assignments | nothing — it is a read | Stages → By User |

```
PATCH /api/workflow/stages/<id>/   {"user": 413}
GET   /api/workflow/stages/?user=88
POST  /api/workflow/replacements/  {...}
```

**There is no bulk "replace all assignments", in the UI or the API.** One
existed briefly and was removed: a single call that rewrote every stage a
person owns, across every module, is an organisation-wide change with no
natural review step and no undo. Moving somebody's work is done one stage at a
time, so the edit is the same size as the row being looked at. Both the By
Workflow and By User views offer the same single-stage **Edit**.

**Changing a stage's configured user does NOT require reassigning existing
business entries.**

```
The stage remains the same.
Existing entries remain at the stage.
Current responsibility is resolved from the current/effective stage user.
```

Why that works, and what your module must do to keep it working:

```
Stage 2  Mukesh -> Ravi

   same workflow      same stage id      same entries waiting there
                                         new responsible user
```

`workflow_stages` is the source of assignment. A module's task row holds
`stage_id` and asks the engine who is responsible each time it needs to know:

```python
from workflow.services.assignments import get_stage_assignment

row = get_stage_assignment(task.stage_id)
row.configured_user_id     # what an administrator set
row.effective_user_id      # who may act today, after replacements
row.has_active_replacement # why those two differ
```

So the module's inbox filters on `stage_id` plus the CURRENT effective user —
never on a user copied onto the task when it was created. Copying it is what
makes a "reassign pending entries" migration necessary, and there is no such
operation in this engine because nothing needs one.

**Permanent versus temporary.** They are different tools and are not
interchangeable:

* a *reassignment* writes `workflow_stages.user_id` — Mukesh no longer works
  that stage at all;
* a *replacement* writes `workflow_user_replacements` — the stage keeps Mukesh,
  and only the effective actor changes, for a date window, expiring by itself.

**History is never rewritten.** A past approval records who actually approved;
changing today's configuration cannot alter it. The engine keeps no action
history at all (§9), so a module's own records are the only history and nothing
here writes to them.

## 9. Approval and rejection — the MODULE's job

The engine tells you the stages. It does not run them.

```
Workflow Engine                Business Module
---------------                ---------------
stages 1..N, with              creates its own task per stage
configured + effective   -->   approve / reject / advance / complete
users                          writes its own action history
                               sends its own notifications
```

The conventions the stage configuration assumes: one approve completes a
stage, one reject ends that execution, and there is exactly ONE user per stage
— no quorum, no approver list, no `approval_required`/`rejection_required`
counts. Rejection is terminal **for that execution**, not for the document;
what happens next is the module's decision (§11).

Implement these in your module's service layer, over your own task model.

## 10. User replacement

Date-based and dynamic. The stage keeps its configured user; only the effective
actor changes:

```
workflow_stages.user_id = 5          ← never modified
replacement: old=5 new=8, 15–22 Sep

14 Sep → 5      18 Sep → 8      23 Sep → 5 (resumes automatically)
```

Resolution is one hop (not transitive). Overlapping windows for one user are
barred by the GiST constraint `workflow_replacement_no_overlap`; adjacent
windows are fine. **The user's account is never modified.**

`select_for_module()` gives you both `user_id` and `effective_user_id` per
stage, and takes `on_date=` so a module reconstructing a past decision gets the
answer that was true then. Recording who acted on whose behalf is the module's
audit responsibility — the engine keeps no action history.

## 11. Resubmission ownership

**Resubmission is a MODULE concept. The engine has no resubmission API, state,
policy, attempt counter, or round number.**

```
Module decides resubmission is allowed
        ↓
Same module flow row reused          ← engine NEVER creates a second
        ↓
Module logs RESUBMITTED in its own history
        ↓
Module invokes the engine again
        ↓
Normal workflow selection runs from scratch
```

Calling `select_for_module()` again re-resolves against the **current**
configuration — it never reuses the previously selected workflow, so a config
that has since become ambiguous correctly fails.

Everything else about resubmission is yours: whether it is allowed, how many
attempts, what the history says, and whether the old tasks are closed or
superseded. The engine keeps no attempt counter, no round number and no action
history to reconcile.

## 12. SQL query rules

### What a Workflow Query stores

```
company          ALL | OIL | BEVERAGES | MART
name
query_text
workflow association
validation state   (validated_at, validation_error)
```

That is the whole configurable surface. It deliberately does NOT store
`company_scope`, `type` or `key_column`:

- `type` was JSAP parity only and nothing in this engine ever read it.
- `key_column` asked an administrator which column identifies a document.
  That is **runtime context owned by the business module**, which passes it as
  `engine.start(..., key_column=...)`; it defaults to `id`. Putting it in the
  query configuration made the engine hold a second copy of a fact it could
  not verify.

### How the condition is evaluated

One workflow may have many queries (OR semantics). A query is a **set
selector**: it answers "which documents belong in this workflow?" The engine
adds the bound document predicate, using the RUNTIME key column:

```sql
SELECT 1 FROM ( <your query> ) AS wf_q WHERE wf_q."<runtime key>" = %s LIMIT 1
```

The identifier comes from the caller, never from the query row; the document
key itself is always a bound parameter.

Rules, all enforced:

- **SELECT / WITH only.** DB CHECK `workflow_query_select_only`
  (`^\s*(select|with)\y` — note `\y`: in PostgreSQL `\b` is *backspace*).
- INSERT/UPDATE/DELETE/DROP/ALTER, multi-statement, `pg_sleep` and friends,
  and forbidden schemas (`pg_catalog`, `information_schema`) are **rejected
  with HTTP 400** and no row is stored.
- An unknown `company` is **rejected with HTTP 400** before the SQL is even
  planned, and no row is stored.
- The document key is always a **bound parameter**, never interpolated.
- Execution is isolated with a statement timeout; a broken query fails the
  start rather than counting as "no match".
- **Use the one central executor** (`workflow.services.conditions`). Do not add
  a module-specific SQL executor — that was JSAP's failure mode, one
  `jsExecute*Queries` per module each with its own injection surface.

## 13. Integration testing checklist

**Registration**
- [ ] module registered — **exactly one** row for its `code`
- [ ] the registry row holds `code` + `name` only
- [ ] re-running registration does not duplicate the row

**Your own tables (§0.1)**
- [ ] three tables: the document, its flow, its action log
- [ ] exactly one flow row per document (UNIQUE FK)
- [ ] the flow stores the engine's `current_stage` **id** — no copied name,
      sequence or user
- [ ] the action log is append-only; nothing updates or deletes a row

**Selection**
- [ ] workflow + query + stage created through the Workflows UI
- [ ] query validated (`validated_at` is set)
- [ ] submitting a document selects exactly one workflow and opens its first
      stage
- [ ] zero matches → `WorkflowNotConfigured`, and the DOCUMENT rolls back with
      it — nothing is left unroutable
- [ ] two matches → `AmbiguousWorkflowSelection`, full rollback

**Acting**
- [ ] the request appears for the stage's effective user and nobody else
- [ ] approving requires BOTH the permission key AND being that effective user;
      each alone is refused
- [ ] approve advances to the next stage; the last approve completes the flow
- [ ] reject ends the flow and opens no further stage
- [ ] a replacement's window re-routes the queue with nothing reassigned
- [ ] changing a stage's user re-routes requests already waiting there

**Nothing extra**
- [ ] **no module-specific workflow tables were created** (§16)

## 14. Example registration

The whole registration, for a module that really creates
`budget.budget_request`, `budget.budget_flow` and `budget.BudgetFlow`:

```json
{
  "code": "BUDGET",
  "name": "Budget Approval"
}
```

The three names above appear **nowhere** in it. They are declared by the Budget
module: the tables by its own `Meta.db_table`, and the link to this
registration by `BudgetFlow.workflow_module_code = 'BUDGET'`.

**This is an example, not a template to paste.** Use the code your module
actually registers.

## 15. Example SQL verification

```sql
-- the registry: identity only
SELECT id, created_at, updated_at, code, name
FROM workflow.workflow_modules ORDER BY id;

-- and the exact schema, which SELECT * would not reveal
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'workflow' AND table_name = 'workflow_modules'
ORDER BY ordinal_position;

-- exactly one row for a module
SELECT count(*) FROM workflow.workflow_modules WHERE code = 'BUDGET';   -- 1

-- its configuration
SELECT w.id, w.code, w.company
FROM workflow.workflows w
JOIN workflow.workflow_modules m ON m.id = w.module_id
WHERE m.code = 'BUDGET';

SELECT q.id, q.name, q.company,
       q.validated_at IS NOT NULL AS usable, q.validation_error
FROM workflow.workflow_queries q
JOIN workflow.workflows w ON w.id = q.workflow_id
JOIN workflow.workflow_modules m ON m.id = w.module_id
WHERE m.code = 'BUDGET';

SELECT s.sequence, s.name, s.user_id
FROM workflow.workflow_stages s
JOIN workflow.workflows w ON w.id = s.workflow_id
JOIN workflow.workflow_modules m ON m.id = w.module_id
WHERE m.code = 'BUDGET' ORDER BY s.sequence;

-- runtime lives in the MODULE's own tables; query those, not `workflow.*`

-- latent ambiguity: workflows in one module that could both apply
SELECT a.code AS wf_a, a.company,
       b.code AS wf_b, b.company
FROM workflow.workflows a
JOIN workflow.workflows b ON a.module_id = b.module_id AND a.id < b.id
 AND (a.company='ALL' OR b.company='ALL' OR a.company = b.company);
```

## 16. What the module must NOT create

The generic engine already owns these — a module that duplicates any of them
has forked the engine:

```
workflow.workflow_modules      workflow.workflow_stages
workflow.workflows             workflow.workflow_queries
workflow.workflow_user_replacements
```

Those five are the whole engine.

So **do not create** `budget_workflow`, `budget_workflow_stage`,
`budget_workflow_query`, `budget_approver`, or any module-specific approver,
stage, query or SQL-executor implementation.

A business module owns exactly three things: its **business document**, its
**flow state** (one flow row per document, a plain model of its own — see
§0.1), and its **own history/log**.
