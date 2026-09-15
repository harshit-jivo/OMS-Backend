"""Fail-loud workflow selection — plan §6.

The whole algorithm, and every rule about what it must NOT do:

    Execute ALL configured queries for the module
                    |
    Collect the DISTINCT workflows that matched
                    |
         0 -> WorkflowNotConfigured
         1 -> select it
        >1 -> AmbiguousWorkflowSelection  (caller rolls back)

There is no tiebreak of any kind. Ambiguity is never resolved by workflow id,
creation order, query id, query order, database row order, a default
workflow, stage `sequence`, or a hidden priority — those columns do not exist
and no implicit ordering is consulted. More than one matching workflow is a
configuration fault and is raised as one.

Two implementation details that exist specifically to keep that promise:

* **Every query is evaluated.** The loop does not stop at the first match.
  Stopping early would make the outcome depend on iteration order, which is
  exactly the implicit ordering this design forbids — and it would hide the
  ambiguity rather than report it.
* **Re-invocation re-selects from scratch.** When a module resubmits an entry
  and calls the engine again, selection runs again in full against the CURRENT
  configuration. The previously used workflow gets no preference; if config
  has since become ambiguous, the re-invocation correctly fails.
"""
import logging
from dataclasses import dataclass

from workflow.exceptions import (
    AmbiguousWorkflowSelection,
    InvalidWorkflowConfiguration,
    WorkflowNotConfigured,
)
from django.db.models import Q

from workflow.models import (
    COMPANY_ALL, WorkflowModule, WorkflowQuery, WorkflowStage,
)
from workflow.services import conditions, replacements

logger = logging.getLogger(__name__)


def company_q(company, *, prefix=''):
    """Rows whose `company` applies to `company`.

    One column, two accepted values: the literal `ALL`, or the document's own
    company code.

        query.company = ALL   + document OIL  -> applies
        query.company = OIL   + document OIL  -> applies
        query.company = OIL   + document MART -> does not apply

    There is deliberately NO ordering and no preference between them — `OIL`
    does not outrank `ALL`, so when both apply the result is two matches and
    therefore ambiguity (§6.1). `approvals.resolve_workflow` does the opposite
    (`order_by('-company')`); that engine is untouched.

    A document with no company matches only `ALL` rows: a company-specific row
    cannot be said to apply to an unknown company.

    `company` MAY be several companies — a list, or the comma-separated string
    a module stores for a document that covers more than one. BackDate is the
    first: one request can ask for rights in OIL and BEVERAGES together, and it
    is still one document with one approval chain. A company-scoped row then
    applies if it covers ANY of them, which is the same rule as the single
    case and not a new one.

    Note what this does NOT do: it does not pick a winner. A two-company
    document that matches one OIL workflow and one BEVERAGES workflow is
    ambiguous, loudly, because which chain approves it is a business decision
    the engine cannot invent. Configuring an `ALL`-scoped workflow is how an
    administrator answers that.
    """
    field = f'{prefix}company'
    applies_to_all = Q(**{field: COMPANY_ALL})
    codes = company_codes(company)
    if not codes:
        return applies_to_all
    return applies_to_all | Q(**{f'{field}__in': codes})


def company_codes(company):
    """`company` as a list of codes, however the caller expressed it.

    Accepts `''`, `'OIL'`, `'OIL,BEVERAGES'`, or any iterable of codes, so a
    module storing one company and a module storing several both call the same
    engine function with the value they actually hold.
    """
    if not company:
        return []
    if isinstance(company, str):
        parts = company.split(',')
    else:
        parts = list(company)
    seen, codes = set(), []
    for part in parts:
        code = str(part).strip().upper()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def candidate_queries(module, company=''):
    """Validated queries for `module` whose company scope applies.

    Scope is filtered at BOTH levels (plan §4.0.2):

    * the WORKFLOW's scope decides whether the workflow applies at all;
    * the QUERY's scope decides which of that workflow's conditions are
      evaluated for this company.

    `select_related` reaches the workflow and module in one query so the loop
    in `select_workflow` does not issue one per candidate.
    """
    return list(
        WorkflowQuery.objects
        .filter(workflow__module=module, validated_at__isnull=False)
        # Deactivated rows are EXCLUDED outright, at both levels. This is a
        # hard gate, exactly like `validated_at`: it decides whether a row
        # takes part at all, and is never consulted to rank or prefer one
        # candidate over another. Ambiguity rules are unchanged.
        .filter(is_active=True, workflow__is_active=True)
        .filter(company_q(company))                       # query level
        .filter(company_q(company, prefix='workflow__'))  # workflow level
        .select_related('workflow', 'workflow__module')
        # Stable listing for reproducible logs ONLY. Never a tiebreak: every
        # candidate is evaluated and ambiguity is reported, not ordered away.
        .order_by('workflow_id', 'id')
    )


def evaluate(module, document_key, company='', *, key_column=None):
    """Every matching query for the document. Evaluates all candidates.

    `key_column` is runtime context from the business module and is passed
    straight through to the executor — see `conditions.matches`.

    Returns the list of matched `WorkflowQuery` objects. Propagates
    `ConditionExecutionError` — a condition that cannot be evaluated fails the
    whole start rather than counting as a non-match.
    """
    matched = []
    for query in candidate_queries(module, company):
        if conditions.matches(query, document_key, key_column=key_column):
            matched.append(query)
    return matched


def select_workflow(module, document_key, company='', *, key_column=None):
    """The one workflow for this document, or an explicit failure.

    Returns `(workflow, matched_query)`.
    """
    matched = evaluate(module, document_key, company, key_column=key_column)

    # Distinct workflows, not distinct queries: one workflow may match through
    # several of its own queries (OR semantics), which is not ambiguity.
    by_workflow = {}
    for query in matched:
        by_workflow.setdefault(query.workflow_id, []).append(query)

    if not by_workflow:
        raise WorkflowNotConfigured(
            context={'module': module.code, 'document_key': document_key},
        )

    if len(by_workflow) > 1:
        names = sorted(q[0].workflow.code for q in by_workflow.values())
        logger.warning(
            'workflow selection ambiguous for module=%s key=%s: %s',
            module.code, document_key, names,
        )
        raise AmbiguousWorkflowSelection(
            f'More than one workflow matches this document '
            f'({", ".join(names)}). Fix the workflow query configuration '
            f'before submitting.',
            context={'module': module.code, 'workflows': names},
        )

    (queries,) = by_workflow.values()
    workflow = queries[0].workflow

    # A workflow with no stages can never progress; refusing here rather than
    # creating a flow that deadlocks immediately.
    if not workflow.stages.filter(is_active=True).exists():
        raise InvalidWorkflowConfiguration(
            f'Workflow "{workflow.code}" has no stages configured.',
            context={'workflow': workflow.code},
        )

    return workflow, queries[0]


# ---------------------------------------------------------------------------
# The module-facing entry point
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageConfig:
    """One configured stage, as the engine hands it to a module.

    `user_id` is the CONFIGURED user — what an administrator set on the
    Workflows page. `effective_user_id` is who may actually act today, after
    replacements. Both are given because they answer different questions: the
    module shows the configured user in its configuration screens and routes
    work to the effective one, and conflating them is how "my stand-in
    approved it" stops being answerable later.
    """

    id: int
    sequence: int
    name: str
    user_id: int
    effective_user_id: int

    def as_dict(self):
        return {
            'id': self.id,
            'sequence': self.sequence,
            'name': self.name,
            'user_id': self.user_id,
            'effective_user_id': self.effective_user_id,
        }


@dataclass(frozen=True)
class WorkflowSelection:
    """What the engine returns: CONFIGURATION, never runtime.

    Deliberately not a model instance and deliberately not persisted. The
    engine used to answer a submission by creating a `workflow_task` row,
    which made it the owner of every module's approval runtime. It now answers
    with the configuration it holds and stops there; the module turns this into
    its own task, flow and history.
    """

    workflow: object
    matched_query: object
    stages: tuple

    def as_dict(self):
        """The plain-data form, for an API response or a log line."""
        return {
            'workflow': {
                'id': self.workflow.pk,
                'code': self.workflow.code,
                'name': self.workflow.name,
                'company': self.workflow.company,
                'module_code': self.workflow.module.code,
            },
            'matched_query': {
                'id': self.matched_query.pk,
                'name': self.matched_query.name,
                'company': self.matched_query.company,
            },
            'stages': [stage.as_dict() for stage in self.stages],
        }


def select_for_module(*, module_code, document_id, company='',
                      key_column=None, on_date=None):
    """Which workflow applies to this business entry, and who approves it.

    THE one call a business module makes. Everything it needs to build its own
    runtime comes back in the result; nothing is written to the database.

        selection = select_for_module(
            module_code='BUDGET', document_id=request.pk, company=request.company)

        for stage in selection.stages:
            BudgetApprovalTask.objects.create(
                request=request, sequence=stage.sequence,
                assigned_to_id=stage.effective_user_id)

    Arguments are RUNTIME CONTEXT supplied by the module — the engine looks
    nothing up about the module's tables:

    * `module_code`  the registered code, e.g. 'BUDGET'
    * `document_id`  the key of the entry being submitted
    * `company`      the document's company, matched against ALL/OIL/... rows
    * `key_column`   the column the module's queries expose as identity
                     (defaults to `id`)
    * `on_date`      resolve replacements as at this date; defaults to today

    Raises `WorkflowNotConfigured` (0 matches), `AmbiguousWorkflowSelection`
    (>1), `InvalidWorkflowConfiguration` (no active stages) or
    `ConditionExecutionError`. There is no tie-breaking of any kind.
    """
    code = (module_code or '').strip().upper()
    try:
        module = WorkflowModule.objects.get(code=code)
    except WorkflowModule.DoesNotExist:
        raise WorkflowNotConfigured(
            f'Module "{code}" is not registered with the workflow engine.',
            context={'module': code},
        )

    workflow, matched_query = select_workflow(
        module, document_id, company, key_column=key_column)

    on_date = on_date or replacements.today()
    rows = list(
        WorkflowStage.objects
        .filter(workflow=workflow, is_active=True)
        .select_related('user')
        .order_by('sequence', 'id')
    )
    # One query for every stage's replacement rather than one per stage.
    effective = replacements.effective_user_ids(
        [row.user_id for row in rows], on_date)

    stages = tuple(
        StageConfig(
            id=row.pk,
            sequence=row.sequence,
            name=row.name,
            user_id=row.user_id,
            effective_user_id=effective.get(row.user_id, row.user_id),
        )
        for row in rows
    )
    return WorkflowSelection(workflow=workflow, matched_query=matched_query,
                             stages=stages)


def stages_for(workflow, *, on_date=None):
    """The ordered active stages of a workflow, with effective users.

    The part of `select_for_module` a module needs when it already knows its
    workflow — re-reading configuration for a flow that is mid-approval, for
    instance, where re-running selection would be wrong.
    """
    on_date = on_date or replacements.today()
    rows = list(
        WorkflowStage.objects
        .filter(workflow=workflow, is_active=True)
        .select_related('user')
        .order_by('sequence', 'id')
    )
    effective = replacements.effective_user_ids(
        [row.user_id for row in rows], on_date)
    return tuple(
        StageConfig(id=row.pk, sequence=row.sequence, name=row.name,
                    user_id=row.user_id,
                    effective_user_id=effective.get(row.user_id, row.user_id))
        for row in rows
    )
