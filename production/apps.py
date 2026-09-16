"""App configuration for the PRDO (Production Order) module.

The module registers ITSELF with the Workflow Engine at deploy time, so nobody
has to remember a manual step:

    register_module(code='PRDO', name='Production Order')

That is the whole registration. The engine's `workflow_modules` table holds
`code` + `name` and nothing else — this module's tables, keys and flow model
are its own business.
"""
from django.apps import AppConfig
from django.db import DatabaseError
from django.db.models.signals import post_migrate


def register_own_modules(sender, **kwargs):
    """Register PRDO with the Workflow Engine. Runs after every `migrate`.

    `post_migrate`, not `ready()`: `ready()` runs before the registry table
    necessarily exists (the first `migrate`, or `collectstatic` against an
    un-migrated database), so registering there either crashes or has to
    swallow errors that would hide real ones.

    Idempotent — `register_module` is `update_or_create` on the unique `code`.
    """
    from workflow.registry import register_module

    using = kwargs.get('using')
    if using is not None and using != 'default':
        # Only the default database carries the workflow schema.
        return

    try:
        register_module(code='PRDO', name='Production Order', verbose=True)
    except DatabaseError:
        # A partial/faked migrate that has not yet created the registry table
        # must not fail the whole command over a bootstrap row.
        pass


class ProductionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'production'
    verbose_name = 'Production Orders (PRDO)'

    def ready(self):
        post_migrate.connect(register_own_modules, sender=self)
