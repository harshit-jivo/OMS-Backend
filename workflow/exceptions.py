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


class WorkflowAlreadyRunning(WorkflowError):
    """A workflow execution is already in progress for this document."""

    default_message = 'A workflow is already running for this document.'


class InvalidWorkflowAction(WorkflowError):
    """The requested action is not valid for the current state."""

    default_message = 'That action is not valid for this workflow right now.'


class UnauthorizedWorkflowAction(WorkflowError):
    """The caller is not the effective actor for this task."""

    default_message = 'This task is not assigned to you.'


class InvalidWorkflowConfiguration(WorkflowError):
    """Configuration is unusable — no stages, unvalidated query, inactive user."""

    default_message = 'The workflow configuration is invalid.'


class StageUserUnavailable(InvalidWorkflowConfiguration):
    """The stage's effective user cannot act (inactive account).

    Surfaced at the moment the stage would open, rather than letting the
    document deadlock there days later — the same choice
    `approvals.services` already makes for an unstaffed rung.
    """

    default_message = (
        'The user configured for this stage is not active, and no '
        'replacement covers today.'
    )


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
