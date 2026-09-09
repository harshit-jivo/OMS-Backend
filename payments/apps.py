from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'
    verbose_name = 'Payments & Deposits'

    def ready(self):
        # Register the approval hooks. Done here (not at import time) so the
        # approval engine never imports `payments`, which would be circular.
        from . import hooks  # noqa: F401
        hooks.register()
