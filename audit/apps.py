from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = 'django.db.models.AutoField'
    name = 'audit'
    verbose_name = 'Audit Log'

    def ready(self):
        from . import signals
        signals.connect()
