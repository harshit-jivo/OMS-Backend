from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cpanel.inventory'
    label = 'cp_inventory'
    verbose_name = 'Control Panel — inventory'
