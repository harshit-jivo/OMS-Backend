"""Module registration — the one supported way to put a module on the engine.

Every Workflow-enabled OMS module needs exactly ONE row in
`workflow.workflow_modules`, and that row carries IDENTITY ONLY:

    register_module(code='BUDGET', name='Budget Approval')

It states that BUDGET exists and may have workflows configured against it.
That is the whole contract.

WHAT THIS FUNCTION DELIBERATELY NO LONGER TAKES
-----------------------------------------------
`business_table`, `business_key_column`, `flow_table` and `flow_model` used to
be required arguments, validated here and written to the registry row. They
are gone, and are not replaced by differently-named equivalents, because they
described the MODULE's implementation while living in the ENGINE's table — a
hand-maintained second copy of facts the module's own migrations already
state, free to drift from them with nothing to detect it.

Where each answer comes from now:

    business_table       the module's own models; no longer used by the engine
    business_key_column  the query that projects it (models.DEFAULT_KEY_COLUMN)
    flow_table           the flow model's own Meta.db_table
    flow_model           the flow class's `workflow_module_code` (workflow.flows)

None of that needs to be typed anywhere, so none of it can be typed wrongly.

IDEMPOTENT BY CONSTRUCTION
--------------------------
`update_or_create` on the unique `code`, so it is safe to call from a data
migration, a `post_migrate` handler, `AppConfig.ready()`, a management command
or a deploy script, and safe to call repeatedly. Calling it twice updates one
row; it never creates a second.

See `docs/Approvals/WORKFLOW_MODULE_INTEGRATION.md` for the full contract.
"""
import logging

from django.core.exceptions import ValidationError

from workflow.models import WorkflowModule

logger = logging.getLogger(__name__)


def register_module(*, code, name, verbose=False):
    """Register (or update) one module. Idempotent on `code`.

    Returns `(WorkflowModule, created: bool)`.

    `code` is uppercased because `workflow_module_code_upper` is a database
    CHECK — accepting `'budget'` and letting the constraint reject it would be
    a worse error message for no benefit.
    """
    code = (code or '').strip().upper()
    if not code:
        raise ValidationError('code is required')

    name = (name or '').strip()
    if not name:
        raise ValidationError('name is required')

    module, created = WorkflowModule.objects.update_or_create(
        code=code, defaults={'name': name},
    )
    if verbose:
        logger.info('workflow module %s %s (id=%s)', code,
                    'registered' if created else 'updated', module.pk)
    return module, created


def is_registered(code):
    return WorkflowModule.objects.filter(code=(code or '').upper()).exists()


def registration_for(code):
    return WorkflowModule.objects.filter(code=(code or '').upper()).first()
