from django.apps import AppConfig


class DevicesConfig(AppConfig):
    # BigAutoField to match the newest app in the project (einvoice) and to
    # leave headroom for a table that grows one row per user per device.
    default_auto_field = "django.db.models.BigAutoField"
    name = "devices"
    verbose_name = "Device & Version Management"
