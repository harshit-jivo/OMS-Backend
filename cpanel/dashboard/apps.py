from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cpanel.dashboard'
    label = 'cp_dashboard'
    verbose_name = 'Control Panel — dashboard'
