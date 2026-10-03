from django.apps import AppConfig


class HomeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cpanel.home'
    label = 'cp_home'
    verbose_name = 'Control Panel — home'
