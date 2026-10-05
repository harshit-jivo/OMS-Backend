from django.apps import AppConfig
from django.db import DatabaseError
from django.db.models.signals import post_migrate

#: The Workflow Engine module code payments routes through. ONE module for both
#: document types: receipts and deposits are told apart by their document
#: NUMBER (`RCP-…` / `DEP-…`), which is what workflow selection keys on — see
#: payments/workflow_flow.py. Registered as `code` + `name` and nothing else,
#: per docs/Approvals/WORKFLOW_MODULE_INTEGRATION.md §2.
MODULE_CODE = 'PAYMENTS'
MODULE_NAME = 'Payments'


def register_own_modules(sender, **kwargs):
    """Register PAYMENTS with the Workflow Engine. Runs after every migrate."""
    from workflow.registry import register_module

    using = kwargs.get('using')
    if using is not None and using != 'default':
        # Only the default database carries the workflow schema.
        return

    try:
        register_module(code=MODULE_CODE, name=MODULE_NAME, verbose=True)
    except DatabaseError:
        # A partial/faked migrate that has not yet created the registry table
        # must not fail the whole command over a bootstrap row.
        pass


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'
    verbose_name = 'Payments & Deposits'

    def ready(self):
        # Register with the Workflow Engine after migrate, never at import
        # time: `ready()` runs before the registry table necessarily exists.
        post_migrate.connect(register_own_modules, sender=self)

        # Offline configuration checks (no SAP connection) — see checks.py.
        from . import checks  # noqa: F401
