"""Credit Limit (CREDIT_LIMIT). Registers itself with the Workflow Engine after
every `migrate`, exactly as `backdate.apps` does."""
from django.apps import AppConfig
from django.db import DatabaseError
from django.db.models.signals import post_migrate


def register_own_module(sender, **kwargs):
    from workflow.registry import register_module

    using = kwargs.get('using')
    if using is not None and using != 'default':
        return
    try:
        register_module(code='CREDIT_LIMIT', name='Credit Limit', verbose=True)
    except DatabaseError:
        pass


class CreditLimitConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'credit_limit'
    verbose_name = 'Credit Limit'

    def ready(self):
        post_migrate.connect(register_own_module, sender=self)
