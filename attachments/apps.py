from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'attachments'
    verbose_name = 'Attachments'

    def ready(self):
        # Registers the storage-path system check. Importing it here rather
        # than at module scope is the documented way: the check must be
        # registered once the app registry is populated, and `ready` is the
        # only hook that guarantees it.
        from . import checks  # noqa: F401
