# Workflow Module Integration Contract

**Audience:** anyone — human or Claude Code — adding a Workflow-enabled OMS module.
**Status:** binding contract. The engine architecture is settled; see
`WORKFLOW_ENGINE_IMPLEMENTATION_PLAN.md` for why each rule exists.

> **When Claude Code creates a new Workflow-enabled OMS module,
> it must register exactly one row in `workflow.workflow_modules`
> using the module's actual database/model definitions.**

Not a suggestion, and not a step to leave for a developer to remember: a module
with no registry row cannot start a workflow at all.

---

## 1. Why every Workflow-enabled module needs a registry row

The engine is generic. It never imports a business module, so at runtime it
knows nothing about your tables until the registry tells it:

| The engine needs to know | Registry field | What breaks without it |
|---|---|---|
| Which relation a configured query may read | `business_table` | every query fails the allow-list |
| Which column identifies one document | `business_key_column` | the `%s` bind in the condition wrapper has no target |
| Which concrete flow model holds runtime state | `flow_model` | `flow_model_for()` raises; approve/reject cannot resolve the flow |
| Where those flow rows live | `flow_table` | operators cannot trace runtime state |

This is what keeps the engine generic: the module supplies facts about itself
once, and the engine works against any module without a line of module-specific
code.

## 2. The exact `workflow.workflow_modules` fields

| Column | Type | Required | Notes |
|---|---|---|---|
| `code` | `varchar(30)` | yes | **UNIQUE**, UPPERCASE (DB CHECK `workflow_module_code_upper`) |
| `name` | `varchar(100)` | yes | human label |
| `business_table` | `varchar(120)` | yes | schema-qualified, **raw-SQL form** — see the warning below |
| `business_key_column` | `varchar(60)` | yes | usually `id` |
| `flow_table` | `varchar(120)` | yes | where the module's flow rows live |
| `flow_model` | `varchar(120)` | yes | `app_label.ModelName`, must subclass `WorkflowFlowBase` |

> ⚠️ **Write table names as raw SQL, not as Django's `db_table`.**
> Django writes `db_table = 'budget"."budget_request'` so it can quote it,
> emitting `"budget"."budget_request"`. A configured query is **raw SQL**, where
> `budget"."budget_request` parses as the identifier `budget` followed by a
> quoted `.` — not a table reference. Register
> **`budget.budget_request`**. `register_module()` rejects the quoting form,
> because this bug reads as a broken query rather than a bad registration.

## 3. How Claude Code registers a new module

Read the real definitions first — never assume names.

```
Create the module
      ↓
Inspect its ACTUAL models/tables
      ↓
Identify the business document table      (models.py -> Meta.db_table)
      ↓
Identify the business key column          (usually the pk, 'id')
      ↓
Identify the flow model + table           (the WorkflowFlowBase subclass)
      ↓
Register in workflow.workflow_modules     (register_module / management command)
      ↓
Verify: exactly ONE row
      ↓
Create the workflow configuration         (workflow -> queries -> stages)
      ↓
Run the module → workflow integration test
```

Two supported ways, both idempotent:

```python
# Data migration, AppConfig.ready(), or bootstrap script
from workflow.registry import register_module

register_module(
    code='BUDGET',
    name='Budget Approval',
    business_table='budget.budget_request',
    business_key_column='id',
    flow_table='budget.budget_flow',
    flow_model='budget.BudgetFlow',
)
```

```bash
python manage.py register_workflow_module \
    --code BUDGET --name "Budget Approval" \
    --business-table budget.budget_request --business-key-column id \
    --flow-table budget.budget_flow --flow-model budget.BudgetFlow

python manage.py register_workflow_module --list   # verify
```

`register_module()` validates before writing: `flow_model` must resolve through
the app registry and subclass `WorkflowFlowBase`, and both table names are
rejected if they use the quoting form.

## 4. Idempotent registration

`register_module()` uses `update_or_create(code=...)`. `code` is UNIQUE in the
database, so a second registration **updates** the existing row — it never
creates a duplicate. Safe to call from a migration that reruns, from
`AppConfig.ready()` on every boot, or from a deploy script.

```sql
SELECT id, code, name, business_table, business_key_column, flow_table, flow_model
FROM workflow.workflow_modules ORDER BY id;

SELECT * FROM workflow.workflow_modules WHERE code = 'BUDGET';   -- expect exactly 1
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
                                 "company_scope": "ALL"}
POST /api/workflow/queries/     {"workflow": 1, "name": "all", "company_scope": "ALL",
                                 "query_text": "SELECT * FROM budget.budget_request"}
POST /api/workflow/stages/      {"workflow": 1, "name": "Manager", "sequence": 1, "user": 42}
```

A workflow with **no stages** is refused at submit with
`InvalidWorkflowConfiguration` rather than creating a flow that deadlocks.

## 6. How the module invokes Workflow

**There is no generic `/workflow/start/` endpoint, and there will not be one.**
The engine does not own business submission. The module calls it from its own
submit operation, inside the module's transaction:

```python
from django.db import transaction
from workflow.services import engine
from workflow.models import WorkflowModule

with transaction.atomic():
    document = BudgetRequest.objects.create(...)          # business row
    flow, _ = BudgetFlow.objects.get_or_create(           # module owns this row
        document=document, defaults={'company': document.company})
    flow, task = engine.start(
        module=WorkflowModule.objects.get(code='BUDGET'),
        flow=flow,
        document_key=document.pk,
        company=document.company,
        user=request.user,
        context={'branch': document.branch},              # routing inputs only
    )
```

The module creates or reuses its flow row and hands it in. **The engine never
creates a flow row.** `context` is for routing inputs and the selection
explanation — never a copy of the business document.

Errors to handle: `WorkflowNotConfigured`, `AmbiguousWorkflowSelection`,
`WorkflowAlreadyRunning`, `InvalidWorkflowConfiguration`, `StageUserUnavailable`,
`ConditionExecutionError`. All subclass `workflow.exceptions.WorkflowError`.

## 7. Company scope rules

Companies are `OIL`, `BEVERAGES`, `MART` (`core.companies`). Scope lives on
**both** `workflows` and `workflow_queries`:

```
company_scope = 'ALL'       -> company IS NULL   (one row serves every company)
company_scope = 'SPECIFIC'  -> company = 'OIL' | 'BEVERAGES' | 'MART'
```

A row can never mean both — CHECK `*_company_scope_consistent`. **`ALL` is one
row; never copy a configuration per company.**

**There is no precedence.** A `SPECIFIC` match does not beat an `ALL` match — if
both apply and both match, that is `AmbiguousWorkflowSelection`. (This
deliberately differs from `approvals.resolve_workflow`, which prefers the
specific row. That engine is untouched.)

A query may only **narrow** its workflow's scope: under a workflow scoped
`SPECIFIC(OIL)`, a query must be `ALL` or `SPECIFIC(OIL)`.

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
priority. At most one *open* task per stage, enforced by
`workflow_task_one_open_per_stage_uq`.

## 9. Approval and rejection

```
Stage → assigned (effective) user
   ├── APPROVE → stage complete → next stage, or flow APPROVED
   └── REJECT  → flow REJECTED (this execution ends)
```

One approve completes a stage. One reject ends the workflow. Rejection is
terminal **for that execution**, not for the document — what happens next is the
module's decision (§11).

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
windows are fine. **The user's account is never modified.** Audit records
`acted_by` and `on_behalf_of`.

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

The engine re-resolves selection against the **current** configuration — it
never reuses the previously selected workflow, so a config that has since become
ambiguous correctly fails. Earlier `workflow_action` rows are retained and the
sequence continues; `created_at` on the flow row does not change.

Your module owns its own history table (the `flow_logs` role). **No such table
exists in OMS-Backend** — `workflow_test_document_log` is the harness's version.
Name yours per your module's own architecture. `workflow_action` records engine
actions only and has no `RESUBMITTED` value.

## 12. SQL query rules

One workflow may have many queries (OR semantics). A query is a **set
selector**: it answers "which documents belong in this workflow?" The engine
adds the bound document predicate:

```sql
SELECT 1 FROM ( <your query> ) AS wf_q WHERE wf_q."<key>" = %s LIMIT 1
```

Rules, all enforced:

- **SELECT / WITH only.** DB CHECK `workflow_query_select_only`
  (`^\s*(select|with)\y` — note `\y`: in PostgreSQL `\b` is *backspace*).
- INSERT/UPDATE/DELETE/DROP/ALTER, multi-statement, `pg_sleep` and friends, and
  relations outside the module's allow-list are **rejected with HTTP 400** and
  no row is stored.
- The document key is always a **bound parameter**, never interpolated.
- Execution is isolated with a statement timeout; a broken query fails the
  start rather than counting as "no match".
- **Use the one central executor** (`workflow.services.conditions`). Do not add
  a module-specific SQL executor — that was JSAP's failure mode, one
  `jsExecute*Queries` per module each with its own injection surface.

## 13. Integration testing checklist

- [ ] module registered — **exactly one** row for its `code`
- [ ] re-running registration does not duplicate the row
- [ ] `flow_model` resolves and subclasses `WorkflowFlowBase`
- [ ] `business_table` in raw-SQL form (no `"."`)
- [ ] workflow + query + stage created through the normal APIs
- [ ] query validated (`validated_at` is set)
- [ ] submitting a document selects the workflow and creates **one** task
- [ ] the task appears in the right user's inbox and nobody else's
- [ ] an unauthorised user gets `UnauthorizedWorkflowAction`
- [ ] approve advances to the next stage; the last approve completes the flow
- [ ] reject ends the execution and opens no further stage
- [ ] zero matches → `WorkflowNotConfigured`, nothing created
- [ ] two matches → `AmbiguousWorkflowSelection`, full rollback
- [ ] resubmission reuses the same flow row and keeps history
- [ ] **no module-specific workflow tables were created** (§8 of the task brief)

## 14. Example registration

For a module that really creates `budget.budget_request`, `budget.budget_flow`
and `budget.BudgetFlow`:

```json
{
  "code": "BUDGET",
  "name": "Budget Approval",
  "business_table": "budget.budget_request",
  "business_key_column": "id",
  "flow_table": "budget.budget_flow",
  "flow_model": "budget.BudgetFlow"
}
```

**This is an example, not a template to paste.** Use the names your module
actually defines.

## 15. Example SQL verification

```sql
-- the registry
SELECT id, code, name, business_table, business_key_column, flow_table, flow_model
FROM workflow.workflow_modules ORDER BY id;

-- exactly one row for a module
SELECT count(*) FROM workflow.workflow_modules WHERE code = 'BUDGET';   -- 1

-- its configuration
SELECT w.id, w.code, w.company_scope, w.company
FROM workflow.workflows w
JOIN workflow.workflow_modules m ON m.id = w.module_id
WHERE m.code = 'BUDGET';

SELECT q.id, q.name, q.company_scope, q.company,
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

-- runtime for one module
SELECT t.id, t.flow_id, t.stage_id, t.stage_user_id, t.status
FROM workflow.workflow_task t
JOIN workflow.workflow_modules m ON m.id = t.module_id
WHERE m.code = 'BUDGET' ORDER BY t.created_at DESC;

SELECT a.flow_id, a.sequence, a.action, a.stage_name,
       a.acted_by_username, a.on_behalf_of_id, a.acted_at
FROM workflow.workflow_action a
JOIN workflow.workflow_modules m ON m.id = a.module_id
WHERE m.code = 'BUDGET' ORDER BY a.flow_id, a.sequence;

-- latent ambiguity: workflows in one module that could both apply
SELECT a.code AS wf_a, a.company_scope, a.company,
       b.code AS wf_b, b.company_scope, b.company
FROM workflow.workflows a
JOIN workflow.workflows b ON a.module_id = b.module_id AND a.id < b.id
 AND (a.company_scope='ALL' OR b.company_scope='ALL' OR a.company = b.company);
```

## 16. What the module must NOT create

The generic engine already owns these — a module that duplicates any of them
has forked the engine:

```
workflow.workflow_modules      workflow.workflow_stages     workflow.workflow_task
workflow.workflows             workflow.workflow_queries    workflow.workflow_action
workflow.workflow_user_replacements
```

So **do not create** `budget_workflow`, `budget_workflow_stage`,
`budget_workflow_query`, `budget_approver`, or any module-specific approver,
stage, query or SQL-executor implementation.

A business module owns exactly three things: its **business document**, its
**flow state** (one `WorkflowFlowBase` subclass), and its **own history/log**.
