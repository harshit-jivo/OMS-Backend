from django.apps import AppConfig


class HaisConfig(AppConfig):
    # BigAutoField to match the newest apps in the project (einvoice/devices);
    # the log table grows one row per device movement, so leave 64-bit headroom.
    default_auto_field = "django.db.models.BigAutoField"
    name = "HAIS"
    verbose_name = "HAIS — Hardware Asset Identification Software"
