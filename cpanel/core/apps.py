"""C_Panel's shared layer, inside OMS: the SAP connector, the page shell and
theme, the xlsx writer and the permission bridge (context_processors.py).

C_Panel seeded default login accounts here after every migration (including a
superuser with a known password). That is deliberately gone: inside OMS the
only users are OMS users, and access comes from OMS permission keys.
"""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cpanel.core'
    label = 'cp_core'
    verbose_name = 'Control Panel — core'

    def ready(self):
        from django.core.checks import register

        from .companies import check_companies
        register(check_companies)
