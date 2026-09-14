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

from workflow.exceptions import (
    AmbiguousWorkflowSelection,
    InvalidWorkflowConfiguration,
    WorkflowNotConfigured,
)
from django.db.models import Q

from workflow.models import CompanyScope, WorkflowQuery
from workflow.services import conditions

logger = logging.getLogger(__name__)


def company_scope_q(company, *, prefix=''):
    """Rows whose company scope applies to `company`.

    `ALL` applies to every company; `SPECIFIC` only to its own. There is
    deliberately NO ordering and no preference between the two — a `SPECIFIC`
    row does not outrank an `ALL` row, so when both apply the result is two
    matches and therefore ambiguity (§6.1). `approvals.resolve_workflow` does
    the opposite (`order_by('-company')`); that engine is untouched.

    A document with no company matches only `ALL` rows: a `SPECIFIC` row
    cannot be said to apply to an unknown company.
    """
    field = f'{prefix}company_scope'
    code = f'{prefix}company'
    applies_to_all = Q(**{field: CompanyScope.ALL})
    if not company:
        return applies_to_all
    return applies_to_all | Q(**{field: CompanyScope.SPECIFIC, code: company})


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
        .filter(company_scope_q(company))                       # query level
        .filter(company_scope_q(company, prefix='workflow__'))  # workflow level
        .select_related('workflow', 'workflow__module')
        # Stable listing for reproducible logs ONLY. Never a tiebreak: every
        # candidate is evaluated and ambiguity is reported, not ordered away.
        .order_by('workflow_id', 'id')
    )


def evaluate(module, document_key, company=''):
    """Every matching query for the document. Evaluates all candidates.

    Returns the list of matched `WorkflowQuery` objects. Propagates
    `ConditionExecutionError` — a condition that cannot be evaluated fails the
    whole start rather than counting as a non-match.
    """
    matched = []
    for query in candidate_queries(module, company):
        if conditions.matches(query, document_key):
            matched.append(query)
    return matched


def select_workflow(module, document_key, company=''):
    """The one workflow for this document, or an explicit failure.

    Returns `(workflow, matched_query)`.
    """
    matched = evaluate(module, document_key, company)

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
    if not workflow.stages.exists():
        raise InvalidWorkflowConfiguration(
            f'Workflow "{workflow.code}" has no stages configured.',
            context={'workflow': workflow.code},
        )

    return workflow, queries[0]
