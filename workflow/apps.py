"""App configuration for the Workflow Engine.

This app registers NO modules of its own, and that is the point: it holds the
configuration a business module is configured *with*, and owns no business
module itself. It used to auto-register a `TESTFLOW` harness here; that harness
and its tables are gone.

HOW A REAL MODULE REGISTERS ITSELF
----------------------------------
The module does it from its own AppConfig, so deploying the module registers
it and nobody has to remember a manual step:

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

`post_migrate`, not `ready()` directly: `ready()` runs before the table
necessarily exists (during the first `migrate`, or `collectstatic` against an
un-migrated database), so registering there either crashes or has to swallow
errors that would hide real ones.

`register_module` is idempotent — `update_or_create` on the unique `code` — so
deploying twice leaves one row. It takes `code` and `name` and nothing else;
the module's tables, keys and flow models are the module's own business.
"""
from django.apps import AppConfig


class WorkflowConfig(AppConfig):
    # BigAutoField to match the newest apps in the project (HAIS/payments).
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'workflow'
    verbose_name = 'Workflow Engine'
