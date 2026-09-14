"""Module registration — the one supported way to put a module on the engine.

Every Workflow-enabled OMS module needs exactly ONE row in
`workflow.workflow_modules`. Without it the engine cannot resolve which
business table a configured query may read, which column to bind as the
document key, or which concrete flow model belongs to the module — so
`WorkflowService.start()` has nothing to work with.

Leaving that row as a manual step is how it gets forgotten. `register_module`
is idempotent (`update_or_create` on the unique `code`), so it is safe to call
from a data migration, an `AppConfig.ready()`, a management command, or a
deploy script, and safe to call repeatedly.

See `docs/architecture/WORKFLOW_MODULE_INTEGRATION.md` for the full contract.
"""
import logging

from django.apps import apps
from django.core.exceptions import ValidationError

from workflow.models import WorkflowModule

logger = logging.getLogger(__name__)


def _check_flow_model(flow_model):
    """`app_label.ModelName` must resolve and must be a concrete flow model.

    Checked at registration rather than at submit time: a typo here would
    otherwise surface as a failed approval on a real document, long after the
    module shipped.
    """
    try:
        app_label, model_name = flow_model.split('.', 1)
    except ValueError:
        raise ValidationError(
            f'flow_model must be "app_label.ModelName", got {flow_model!r}')
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise ValidationError(f'flow_model {flow_model!r} does not resolve: {exc}')

    from workflow.models import WorkflowFlowBase
    if not issubclass(model, WorkflowFlowBase):
        raise ValidationError(
            f'flow_model {flow_model!r} must subclass WorkflowFlowBase — '
            f'the engine relies on its columns (status, current_stage, ...).')
    return model


def _check_business_table(business_table):
    """Reject Django's `schema"."table` quoting form.

    A configured query is RAW SQL. Django writes `db_table` as
    `schema"."table` so it can quote it, but that same text in raw SQL parses
    as the identifier `schema` followed by a quoted `.`, which is not a table
    reference. Registration is the right place to catch it — otherwise every
    query written against the module fails validation for a reason that looks
    like it is about the query.
    """
    if '"."' in business_table:
        plain = business_table.replace('"."', '.')
        raise ValidationError(
            f'business_table must be written as it appears in raw SQL: use '
            f'{plain!r}, not the db_table quoting form {business_table!r}.')


def register_module(*, code, name, business_table, business_key_column,
                    flow_table, flow_model, verbose=False):
    """Register (or update) one module. Idempotent on `code`.

    Returns `(WorkflowModule, created: bool)`.

    Every value must come from the module's ACTUAL models/tables — read them,
    do not assume. `code` is uppercased because `workflow_module_code_upper`
    is a database CHECK.
    """
    code = (code or '').strip().upper()
    if not code:
        raise ValidationError('code is required')

    _check_business_table(business_table)
    _check_business_table(flow_table)
    _check_flow_model(flow_model)

    module, created = WorkflowModule.objects.update_or_create(
        code=code,
        defaults={
            'name': name,
            'business_table': business_table,
            'business_key_column': business_key_column,
            'flow_table': flow_table,
            'flow_model': flow_model,
        },
    )
    if verbose:
        logger.info('workflow module %s %s (id=%s)', code,
                    'registered' if created else 'updated', module.pk)
    return module, created


def is_registered(code):
    return WorkflowModule.objects.filter(code=(code or '').upper()).exists()


def registration_for(code):
    return WorkflowModule.objects.filter(code=(code or '').upper()).first()
