from django.apps import AppConfig


class SalesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cpanel.sales'
    label = 'cp_sales'
    verbose_name = 'Control Panel — sales'
