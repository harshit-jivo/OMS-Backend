"""Engine errors, each mapping to one explicit API response.

These are plain exceptions rather than DRF `APIException` subclasses so the
service layer stays usable outside a request (management commands, the
scheduler, tests). `workflow.views` translates them; `core.exception_handler`
never sees a raw SQL error, because `ConditionExecutionError` carries a
sanitised message and the original is logged instead.
"""


class WorkflowError(Exception):
    """Base for every engine error. Carries an API-safe message."""

    #: Overridden per subclass; used as the API `message`.
    default_message = 'Workflow error.'

    def __init__(self, message='', **context):
        self.message = message or self.default_message
        self.context = context
        super().__init__(self.message)


class WorkflowNotConfigured(WorkflowError):
    """No configured query matched the document — nothing to run."""

    default_message = (
        'No workflow is configured for this document. '
        'Nothing was submitted.'
    )


class AmbiguousWorkflowSelection(WorkflowError):
    """More than one workflow matched.

    A configuration fault, deliberately fatal: the engine has no tiebreak —
    no priority, no id ordering, no creation order — so selecting one would
    mean inventing an implicit order (plan §6.1).
    """

    default_message = (
        'More than one workflow matches this document. '
        'Fix the workflow query configuration before submitting.'
    )


# `WorkflowAlreadyRunning`, `InvalidWorkflowAction` and
# `UnauthorizedWorkflowAction` used to live here. They described RUNTIME the
# engine no longer owns — "a flow is already open", "you may not act on this
# task" — and a module raises its own equivalents over its own task model.
# Keeping them would advertise an engine capability that does not exist.


class InvalidWorkflowConfiguration(WorkflowError):
    """Configuration is unusable — no stages, unvalidated query, inactive user."""

    default_message = 'The workflow configuration is invalid.'


# `StageUserUnavailable` is gone with them: whether a stage's user can act is
# decided when the MODULE opens that stage, against the module's own rules.
# The engine reports the configured and effective user and stops there.


class ConditionExecutionError(WorkflowError):
    """A configured query failed to execute (SQL error or timeout).

    The start is failed and rolled back rather than skipping the query:
    silently skipping a broken condition would route the document down the
    wrong workflow, which is worse than refusing to submit (plan §5.3).

    The message is deliberately generic. The underlying database error is
    logged, never returned — an exception's text routinely carries the query
    itself.
    """

    default_message = (
        'A configured workflow condition could not be evaluated. '
        'The incident has been logged.'
    )


class QueryValidationError(WorkflowError):
    """A configured query failed validation and was not saved as usable."""

    default_message = 'The configured SQL query is not valid for this engine.'
